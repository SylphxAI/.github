"""Real sparse Git input, large negative inputs, and polling syntax witnesses."""
import importlib.util
import json
import os
import pathlib
import subprocess
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
ACTION = ROOT / '.github/actions/stack-conformance'
SPEC = importlib.util.spec_from_file_location('scanner_regressions', ACTION / 'stack_conformance.py')
sc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sc)


class PollingSyntaxTests(unittest.TestCase):
    def polling(self, suffix, source):
        return [row for row in sc.architecture.scan_file('src/input' + suffix, source, 'example/app', set(), [])
                if row['rule'] in {'polling-loop', 'polling-timer'}]

    def test_unbounded_reads_fail_in_each_supported_language(self):
        cases = {
            '.rs': 'fn run() { loop { query(); sleep(1); } }',
            '.go': 'func run() { for { query(); sleep(1); } }',
            '.ts': 'async function run() { while (true) { await fetchStatus(); } }',
            '.js': 'function run() { for (;;) { query(); } }',
            '.py': 'while True:\n query()\n',
            '.sh': 'while true; do query; sleep 1; done\n',
        }
        for suffix, source in cases.items():
            with self.subTest(suffix=suffix):
                self.assertTrue(self.polling(suffix, source))

    def test_collection_and_finite_iteration_pass_in_each_language(self):
        cases = {
            '.rs': 'fn run(items: Items) { for item in items { query(item); } }',
            '.go': 'func run() { for _, item := range items { query(item) } }',
            '.ts': 'async function run() { for (const item of items) { await query(item); } }',
            '.js': 'function run() { for (let i = 0; i < 12; i++) { query(i); } }',
            '.py': 'for item in items:\n query(item)\n',
            '.sh': 'for item in a b c; do query; sleep 1; done\n',
        }
        for suffix, source in cases.items():
            with self.subTest(suffix=suffix):
                self.assertEqual(self.polling(suffix, source), [])

    def test_trait_poll_methods_and_unbraced_fixture_inserts_are_not_loops(self):
        for implementation in ('BoxedIap', 'CatalogIap'):
            with self.subTest(implementation=implementation):
                self.assertEqual(self.polling('.rs', 'impl IapPort for ' + implementation +
                    ' { fn poll(&mut self) -> Poll { self.inner.poll() } }'), [])
        source = '''async function run() {
  for (let uid = 1; uid <= 12; uid++)
    await db.query(`INSERT INTO users VALUES($1)`, [uid]);
  for (const [uid, time, device] of rows)
    await db.query(`INSERT INTO registrations VALUES($1,$2,$3)`, [uid, time, device]);
  const query = "SELECT * FROM report()";
  const result = (await db.query(query)).rows;
}'''
        self.assertEqual(self.polling('.mjs', source), [])

    def test_finite_cleanup_collections_do_not_poll(self):
        source = '''const clients = [admin, application, ddl];
const databases = [emptyName, partialName, occupiedName];
try {
  await verifyRelease();
} finally {
  for (const client of clients.filter((client) => client !== admin)) await client.end();
  if (admin) {
    for (const name of databases) await admin.query(`DROP DATABASE ${name}`);
    await admin.end();
  }
}'''
        self.assertEqual(self.polling('.mjs', source), [])

    def test_literal_sql_negative_controls_do_not_poll(self):
        source = '''for (const query of [
  "SELECT id FROM example.mail",
  "INSERT INTO example.mail (id, uid, payload) VALUES (4,48,'blocked')",
  "UPDATE example.mail SET payload = 'blocked' WHERE id = 2",
]) {
  await assert.rejects(application.query(query), (error) => error.code === "57014");
}'''
        self.assertEqual(self.polling('.mjs', source), [])

    def test_const_finite_array_iteration_does_not_poll(self):
        source = '''const queries = ["SELECT 1", "SELECT 2", "SELECT 3"];
for (const query of queries) { await application.query(query); }'''
        for suffix in ('.js', '.mjs', '.ts'):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.polling(suffix, source), [])

    def test_finite_outer_loop_does_not_hide_unbounded_transport_poll(self):
        source = '''const clients = [application, ddl];
for (const client of clients) {
  while (true) { await client.query("SELECT 1"); }
}'''
        for suffix in ('.js', '.mjs', '.ts'):
            with self.subTest(suffix=suffix):
                findings = self.polling(suffix, source)
                self.assertEqual(len(findings), 1)
                self.assertEqual(findings[0]['rule'], 'polling-loop')
                self.assertEqual(findings[0]['line'], 3)

    def test_exit_and_event_conditions_are_not_proven_unbounded(self):
        for suffix, source in (
                ('.rs', 'fn run() { loop { query(); if done { break; } } }'),
                ('.go', 'func run() { for { query(); if done { return } } }'),
                ('.ts', 'while (active) { query(); }'),
                ('.py', 'while True:\n query()\n if done:\n  break\n'),
                ('.sh', 'while true; do query; break; done')):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.polling(suffix, source), [])

    def test_timer_rearm_without_exit_fails_but_guarded_rearm_passes(self):
        source = 'async function refresh() { await fetchStatus(); setTimeout(refresh, 1000); }'
        self.assertTrue(self.polling('.ts', source))
        self.assertEqual(self.polling('.ts', source.replace('await fetchStatus();',
            'if (stopped) return; await fetchStatus();')), [])
        self.assertEqual(self.polling('.ts', 'function tick() { animate(); setTimeout(tick, 16); }'), [])


class ScannerWalkTests(unittest.TestCase):
    def test_large_candidate_free_data_does_not_enter_model_regex(self):
        tree = sc.DictTree({'assets/animation.json': '{"frames":[' + '123456789,' * 100000 + '0]}'})
        with mock.patch.object(sc.model_pins, 'TOKEN') as token:
            token.search.side_effect = AssertionError('full model regex should not scan candidate-free data')
            token.finditer.side_effect = AssertionError('full model regex should not scan candidate-free data')
            report = sc.model_pins.scan(tree, 'example/app', None, sc.architecture.source_views)
        self.assertEqual(report['findings'], [])

    def test_model_prefilter_preserves_every_family_and_reasoning_id(self):
        models = [name + '-9' for name in sc.model_pins.FAMILIES] + ['o1', 'o3', 'o4']
        source = '\n'.join('const model = "' + model + '";' for model in models)
        report = sc.model_pins.scan(sc.DictTree({'src/models.ts': source}), 'example/app', None,
                                    sc.architecture.source_views)
        self.assertEqual({row['model'] for row in report['findings']}, set(models))

    def test_late_model_in_large_authored_data_is_still_checked(self):
        source = '{"frames":[' + '123456789,' * 100000 + '0],"model":"gpt-9"}'
        report = sc.model_pins.scan(sc.DictTree({'assets/config.json': source}), 'example/app', None,
                                    sc.architecture.source_views)
        self.assertEqual([row['model'] for row in report['findings']], ['gpt-9'])

    def test_compiler_output_is_exposed_but_authored_schema_and_generated_filename_are_checked(self):
        header = '// Generated by the protocol buffer compiler. DO NOT EDIT!\n'
        tree = sc.DictTree({'src/Messages.java': header + 'class Messages {}\n',
                           'schema/messages.proto': 'option model = "gpt-9";\n',
                           'src/policy.generated.ts': 'const model = "gpt-9";\n'})
        report = sc.model_pins.scan(tree, 'example/app', None, sc.architecture.source_views)
        self.assertEqual({row['path'] for row in report['findings']},
                         {'schema/messages.proto', 'src/policy.generated.ts'})
        self.assertEqual([row['path'] for row in report['exemptions']], ['src/Messages.java'])
        self.assertFalse(sc.model_pins.source_scope.compiler_generated(
            'const note = "Generated by the protocol buffer compiler. DO NOT EDIT!";\n'))

    def test_vendor_case_variants_do_not_exempt_adjacent_authored_source(self):
        for directory in ('ThirdParty', 'THIRD_PARTY', 'Vendor', 'EXTERNAL'):
            with self.subTest(directory=directory):
                path = 'src/' + directory + '/dependency.ts'
                self.assertTrue(sc.architecture.excluded(path))
                self.assertTrue(sc.model_pins.excluded(path))
        self.assertFalse(sc.architecture.excluded('src/authored.ts'))
        self.assertFalse(sc.model_pins.excluded('src/authored.ts'))

    def test_cache_parser_skips_unrelated_function_bodies_without_changing_reads(self):
        source = 'class Example {\n public int ordinary() { return 1; }\n public int purchase() { return cache.get(); }\n}'
        clean, code, _ = sc.architecture.source_views(source, '.java')
        self.assertEqual(len(list(sc.architecture.economy_cache_reads(clean, code, '.java'))), 1)
        with mock.patch.object(sc.architecture, 'matching_end', side_effect=AssertionError('unrelated body walked')):
            self.assertEqual(list(sc.architecture.named_bodies(clean, code, '.java', lambda name: False)), [])

    def test_sparse_blobless_clone_bulk_fetches_candidates_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            origin = self.blobless_origin(root)
            for layout in ('ordinary', 'renamed', 'linked'):
                with self.subTest(layout=layout):
                    clone = root / layout
                    self.clone_blobless(origin, clone, sparse=True)
                    if layout != 'ordinary':
                        self.git(clone, 'remote', 'rename', 'origin', 'upstream')
                    repository = clone
                    if layout == 'linked':
                        repository = root / 'worktree'
                        self.git(clone, 'worktree', 'add', '--no-checkout', '-b', 'linked', str(repository))
                    result, fetches = self.run_blobless_check(root, repository)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(len(fetches), 1)
                    self.assertIn('--stdin', fetches[0]['argv'])
                    self.assertIn('bulk-fetching', result.stderr)
                    with sc.Tree(str(repository), 'HEAD') as tree:
                        self.assertEqual(tree.read('src/input-0.ts'), 'export const value = 2; // input 0\n')
                        self.assertIsNone(tree.read('absent.ts'))
                    missing = self.git(repository, 'rev-list', '--objects', '--missing=print', '--no-walk', 'HEAD')
                    self.assertIn('?', missing)  # Unscanned binary assets stay absent.

    def test_checkout_blob_none_depth_one_compares_unmaterialized_base(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            origin = self.blobless_origin(root)
            base = self.git(origin, 'rev-parse', 'HEAD~1').strip()
            clone = root / 'clone'
            self.clone_blobless(origin, clone, sparse=False)
            # The shared workflow materializes HEAD, then acquires the base
            # using blob:none. Its historical source/baseline are still absent.
            self.git(clone, 'fetch', '--depth=1', '--filter=blob:none', 'origin', base)
            head_missing = self.git(clone, 'rev-list', '--objects', '--missing=print', '--no-walk', 'HEAD')
            base_missing = self.git(clone, 'rev-list', '--objects', '--missing=print', '--no-walk', base)
            self.assertNotIn('?', head_missing)
            self.assertGreaterEqual(base_missing.count('?'), 33)
            result, fetches = self.run_blobless_check(root, clone, base)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(len(fetches), 1)
            self.assertIn('--stdin', fetches[0]['argv'])
            self.assertIn('reading the base Git tree', result.stderr)
            with sc.Tree(str(clone), base) as tree:
                self.assertEqual(tree.read('.github/stack-departures.txt'), '# base baseline\n')
                for index in range(32):
                    self.assertEqual(tree.read(f'src/input-{index}.ts'), f'export const value = 1; // input {index}\n')

    def test_unavailable_promisor_fails_after_one_explicit_fetch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            origin = self.blobless_origin(root)
            clone = root / 'clone'
            self.clone_blobless(origin, clone, sparse=True)
            self.git(clone, 'remote', 'set-url', 'origin', str(root / 'unavailable'))
            result, fetches = self.run_blobless_check(root, clone)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('bulk-fetch of required Git blobs failed', result.stderr)
            self.assertIn('source was not reviewed', result.stderr)
            self.assertEqual(len(fetches), 1)
            self.assertIn('--stdin', fetches[0]['argv'])

    @staticmethod
    def git(path, *args):
        return subprocess.check_output(['git', '-C', str(path), *args], text=True, stderr=subprocess.PIPE)

    def blobless_origin(self, root):
        origin = root / 'origin'
        origin.mkdir()
        self.git(origin, 'init', '-q', '-b', 'main')
        self.git(origin, 'config', 'user.name', 'Test')
        self.git(origin, 'config', 'user.email', 'test@example.invalid')
        self.git(origin, 'config', 'uploadpack.allowFilter', 'true')
        for revision, value in (('base', 1), ('head', 2)):
            files = {'.github/workflows/check.yml': 'name: Check\n',
                     '.github/stack-departures.txt': f'# {revision} baseline\n',
                     'assets/image.png': f'binary asset {revision}'}
            files.update({f'src/input-{i}.ts': f'export const value = {value}; // input {i}\n' for i in range(32)})
            for name, content in files.items():
                target = origin / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
            self.git(origin, 'add', '.')
            self.git(origin, 'commit', '-qm', revision)
        return origin

    def clone_blobless(self, origin, clone, *, sparse):
        self.git(origin, 'clone', '--filter=blob:none', '--depth=1', '--no-checkout',
                 origin.as_uri(), str(clone))
        if sparse:
            self.git(clone, 'sparse-checkout', 'set', '--cone', '.github')
        else:
            self.git(clone, 'sparse-checkout', 'disable')
        self.git(clone, 'checkout', '--force', 'main')

    def run_blobless_check(self, root, clone, base=''):
        trace = root / (clone.name + '-trace.jsonl')
        started = time.monotonic()
        result = subprocess.run(['python3', str(ACTION / 'stack_conformance.py'), 'check', '--repo', str(clone),
            '--base', base, '--head', 'HEAD', '--repository', 'example/app', '--shared-source', str(ROOT),
            '--report', str(root / 'report.json')], capture_output=True, text=True, timeout=5,
            env={**os.environ, 'GIT_TRACE2_EVENT': str(trace)})
        self.assertLess(time.monotonic() - started, 5)
        events = [json.loads(line) for line in trace.read_text().splitlines()]
        self.assertFalse([event for event in events if event.get('event') == 'child_start'
                          and 'fetch' in event.get('argv', [])], 'unexpected per-blob lazy fetch')
        fetches = [event for event in events if event.get('event') == 'start'
                   and 'fetch' in event.get('argv', [])]
        return result, fetches


if __name__ == '__main__':
    unittest.main()
