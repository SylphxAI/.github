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

    def test_sparse_blobless_clone_fails_promptly_without_any_lazy_fetch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            origin, clone = root / 'origin', root / 'clone'
            origin.mkdir()
            def git(path, *args):
                return subprocess.check_output(['git', '-C', str(path), *args], text=True, stderr=subprocess.PIPE)
            git(origin, 'init', '-q', '-b', 'main')
            git(origin, 'config', 'user.name', 'Test')
            git(origin, 'config', 'user.email', 'test@example.invalid')
            git(origin, 'config', 'uploadpack.allowFilter', 'true')
            for path, text in {'.github/workflows/check.yml': 'name: Check\n', 'src/product.ts': 'export const value = 1;\n'}.items():
                target = origin / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text)
            git(origin, 'add', '.')
            git(origin, 'commit', '-qm', 'fixture')
            subprocess.run(['git', 'clone', '--filter=blob:none', '--no-checkout', origin.as_uri(), str(clone)],
                           check=True, capture_output=True)
            git(clone, 'sparse-checkout', 'set', '--cone', '.github')
            git(clone, 'checkout', 'main')
            self.assertIn('?', git(clone, 'rev-list', '--objects', '--missing=print', '--no-walk', 'HEAD'))
            trace = root / 'trace.jsonl'
            started = time.monotonic()
            result = subprocess.run(['python3', str(ACTION / 'stack_conformance.py'), 'check', '--repo', str(clone),
                '--head', 'HEAD', '--repository', 'example/app', '--shared-source', str(ROOT),
                '--report', str(root / 'report.json')], capture_output=True, text=True, timeout=5,
                env={**os.environ, 'GIT_TRACE2_EVENT': str(trace)})
            self.assertLess(time.monotonic() - started, 5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('required Git blob is missing', result.stdout + result.stderr)
            self.assertIn('sparse-checkout', result.stdout + result.stderr)
            self.assertIn('stack conformance:', result.stderr)
            events = [json.loads(line) for line in trace.read_text().splitlines()]
            fetches = [event for event in events if event.get('event') == 'child_start'
                       and 'fetch' in event.get('argv', [])]
            self.assertEqual(fetches, [])


if __name__ == '__main__':
    unittest.main()
