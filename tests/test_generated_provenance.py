"""Execute generated-policy decisions and immutable-source acquisition, not source greps."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / '.github/actions/ci-ok/generated.py'
SPEC = importlib.util.spec_from_file_location('generated_provenance', SCRIPT)
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)
from review_policy import pattern_regex


def policy(provenance=True):
    return ('version=1\n[tier0]\npaths=[]\n[review]\npaths=["generated/reviewed/**"]\n'
            '[generated]\npaths=["generated/**"]\n' +
            ('[[generated.provenance]]\npaths=["generated/**"]\njobs=["regenerate"]\n' if provenance else ''))


def results(**jobs):
    return {job: {'result': result} for job, result in jobs.items()}


class GeneratedDecisionTests(unittest.TestCase):
    def test_all_matching_proof_jobs_must_be_explicit_required_and_successful(self):
        parsed = gate.parse_policy(policy() + '[[generated.provenance]]\npaths=["**/*.json"]\njobs=["capture"]\n')
        needs = results(regenerate='success', capture='success', tests='success')
        self.assertEqual(gate.evaluate(parsed, ['generated/openapi.json'], needs, set(needs)), [])
        for job in ('regenerate', 'capture'):
            with self.subTest(job=job):
                self.assertTrue(gate.evaluate(parsed, ['generated/openapi.json'], needs, set(needs) - {job}))
                self.assertTrue(gate.evaluate(parsed, ['generated/openapi.json'], {k: v for k, v in needs.items() if k != job}, set(needs)))
                for state in ('skipped', 'failure', 'cancelled', 'in_progress', 'queued', 'neutral', '', None):
                    bad = dict(needs, **{job: {'result': state}})
                    self.assertTrue(gate.evaluate(parsed, ['generated/openapi.json'], bad, set(needs)), state)
                for value in ({}, None, 'success'):
                    self.assertTrue(gate.evaluate(parsed, ['generated/openapi.json'], dict(needs, **{job: value}), set(needs)))

    def test_changed_generated_without_proof_declaration_fails_even_when_review_also_applies(self):
        parsed = gate.parse_policy(policy(False))
        for path in ('generated/openapi.json', 'generated/reviewed/output.rs'):
            self.assertIn('no base-declared provenance job', gate.evaluate(parsed, [path], results(tests='success'), {'tests'})[0])
        self.assertEqual(gate.evaluate(parsed, ['src/manual.rs'], results(tests='success'), {'tests'}), [])
        partial = gate.parse_policy(policy().replace('paths=["generated/**"]\njobs=', 'paths=["generated/covered/**"]\njobs='))
        self.assertTrue(gate.evaluate(partial, ['generated/not-covered.json'], results(regenerate='success'), {'regenerate'}))

    def test_malformed_declarations_are_not_silent_absence(self):
        invalid = [policy().replace('version=1', 'version=true'), policy().replace('jobs=["regenerate"]', 'jobs=[]'),
                   policy().replace('jobs=["regenerate"]', 'jobs=["display / name"]'),
                   policy().replace('jobs=["regenerate"]', 'jobs="regenerate"'),
                   policy().replace('jobs=["regenerate"]', 'job=["regenerate"]'),
                   policy().replace('paths=["generated/**"]\njobs=', 'paths=[]\njobs='),
                   'invalid toml', policy() + 'x="' + 'x' * gate.MAX_POLICY_BYTES + '"']
        for text in invalid:
            with self.subTest(text=text[:90]), self.assertRaises(ValueError):
                gate.parse_policy(text)
        self.assertEqual(gate.provenance_jobs(gate.parse_policy(None), ['generated/new.json']), (set(), []))

    def test_globs_preserve_segment_boundaries_and_literal_regexp_characters(self):
        vectors = [('**/*.json', 'a.json', True), ('generated/*', 'generated/nested/a', False),
                   ('generated/**', 'generated/nested/a', True), ('**/a?.json', 'nested/a1.json', True),
                   ('a[1].json', 'a1.json', False), ('a[1].json', 'a[1].json', True),
                   ('a+b/{x}.json', 'a+b/{x}.json', True)]
        for pattern, path, expected in vectors:
            import re
            self.assertEqual(bool(re.search(pattern_regex(pattern), path)), expected)

    def test_event_ranges_bind_native_merge_group_and_code_dispatch(self):
        base, original, merge = 'a' * 40, 'b' * 40, 'c' * 40
        pr = {'base': {'sha': base}, 'head': {'sha': original}, 'merge_commit_sha': merge}
        self.assertEqual(gate.event_range('pull_request', {'pull_request': pr}, merge), (base, merge, [base, original]))
        self.assertEqual(gate.event_range('pull_request', {'pull_request': pr}, original), (base, original, None))
        self.assertEqual(gate.event_range('pull_request_target', {'pull_request': pr}, base), (base, merge, [base, original]))
        self.assertEqual(gate.event_range('merge_group', {'merge_group': {'base_sha': base, 'head_sha': merge}}, merge), (base, merge, None))
        with self.assertRaises(ValueError):
            gate.event_range('merge_group', {'merge_group': {'base_sha': base, 'head_sha': merge}}, original)
        self.assertIsNone(gate.event_range('push', {'before': '0' * 40}, merge))
        self.assertIsNone(gate.event_range('schedule', {}, merge))
        with self.assertRaises(ValueError):
            gate.event_range('push', {}, merge)
        event = {'inputs': {'code-merge': merge, 'code-base': base, 'code-head': original, 'code-pr': '1', 'code-base-ref': 'main'}}
        with mock.patch.object(gate.contribution, 'dispatch_target', return_value=({}, base, original)) as binding:
            self.assertEqual(gate.event_range('workflow_dispatch', event, original, object()), (base, merge, [base, original]))
            self.assertEqual(binding.call_count, 1)


class GeneratedSourceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='generated-proof-test-')
        self.addCleanup(self.directory.cleanup)
        self.repo = Path(self.directory.name) / 'repo'
        self.repo.mkdir()
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Kyle Tse')
        self.git('config', 'user.email', 'hi@kylet.se')

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), *args], check=True, capture_output=True, text=True).stdout.strip()

    def put(self, path, text):
        destination = self.repo / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text)

    def commit(self):
        self.git('add', '-A')
        self.git('commit', '-qm', 'Synthetic generated provenance fixture')
        return self.git('rev-parse', 'HEAD')

    def fixture(self, text=None):
        self.put('.github/review-policy.toml', policy() if text is None else text)
        for path in ('generated/deleted.json', 'generated/old.json', 'src/into-generated.js'):
            self.put(path, 'synthetic before\n')
        base = self.commit()
        (self.repo / 'generated/deleted.json').unlink()
        (self.repo / 'generated/old.json').rename(self.repo / 'src/from-generated.js')
        (self.repo / 'src/into-generated.js').rename(self.repo / 'generated/renamed.js')
        self.put('generated/new.json', 'synthetic after\n')
        self.put('src/manual.js', 'manual after\n')
        # Head cannot remove its own base obligation or supply a weaker policy.
        self.put('.github/review-policy.toml', policy(False).replace('paths=["generated/**"]', 'paths=[]'))
        return base, self.commit()

    def enforce(self, base, head, needs=None, required=None, **kwargs):
        return gate.enforce(base, head, None, results(regenerate='success') if needs is None else needs,
                            {'regenerate'} if required is None else required, repository='example/synthetic', token='', **kwargs)

    def test_full_path_population_includes_both_rename_identities_additions_and_deletions(self):
        base, head = self.fixture()
        snapshot = gate.Snapshot(self.repo, time.monotonic() + 10)
        paths = snapshot.changed(base, head)
        self.assertEqual(set(paths), {'.github/review-policy.toml', 'generated/deleted.json', 'generated/old.json',
            'generated/renamed.js', 'generated/new.json', 'src/from-generated.js', 'src/into-generated.js', 'src/manual.js'})
        self.assertEqual(self.enforce(base, head, workspace=self.repo), [])
        self.assertTrue(self.enforce(base, head, needs=results(tests='success'), required={'tests'}, workspace=self.repo))
        self.assertTrue(self.enforce(base, head, needs=results(regenerate='skipped'), workspace=self.repo))

    def test_checkout_independent_depth_one_source_and_shallow_checkout_use_the_same_base_policy(self):
        base, head = self.fixture()
        remote = self.repo.as_uri()
        self.assertEqual(self.enforce(base, head, remote=remote), [])
        self.assertTrue(self.enforce(base, head, needs=results(regenerate='skipped'), remote=remote))
        shallow = Path(self.directory.name) / 'shallow'
        subprocess.run(['git', 'clone', '-q', '--depth=1', remote, str(shallow)], check=True)
        self.assertTrue(self.enforce(base, head, needs=results(regenerate='failure'), workspace=shallow, remote=remote))
        # Acquisition never needs the commits between the two requested tree tips.
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            env = gate.scan.fetch(repo, remote, '', base, '', depth=1)
            snapshot = gate.Snapshot(repo, time.monotonic() + 10, env)
            snapshot.git('fetch', '--quiet', '--depth=1', '--filter=blob:none', 'origin', head)
            self.assertEqual(snapshot.git('rev-list', base, head, '--count').strip(), b'2')
            self.assertEqual(snapshot.policy(base)['generated']['provenance'][0]['jobs'], ['regenerate'])

    def test_population_beyond_rest_compare_file_cap_is_not_truncated(self):
        self.put('.github/review-policy.toml', policy())
        base = self.commit()
        for index in range(401):
            self.put(f'generated/{index:04d}.json', 'synthetic\n')
        head = self.commit()
        self.assertEqual(len(gate.Snapshot(self.repo, time.monotonic() + 10).changed(base, head)), 401)
        self.assertTrue(self.enforce(base, head, needs=results(tests='success'), required={'tests'}, workspace=self.repo))

    def test_missing_or_empty_base_policy_is_fast_and_new_policy_cannot_grant_an_ai_exemption(self):
        for text in (None, policy(False).replace('paths=["generated/**"]', 'paths=[]')):
            with self.subTest(text=text):
                self.put('src/control', 'control\n')
                if text is not None:
                    self.put('.github/review-policy.toml', text)
                base = self.commit()
                with (mock.patch.object(gate.Snapshot, 'changed', side_effect=AssertionError('must not inventory')),
                      mock.patch.object(gate.scan, 'fetch', side_effect=AssertionError('must not fetch'))):
                    self.assertEqual(self.enforce(base, 'a' * 40, workspace=self.repo), [])

    def test_policy_modes_size_inventory_limits_deadline_and_merge_parent_binding_fail_closed(self):
        base, head = self.fixture()
        snapshot = gate.Snapshot(self.repo, time.monotonic() + 10)
        with self.assertRaises(ValueError):
            snapshot.changed(base, head, [base, 'a' * 40])
        with self.assertRaises(ValueError):
            gate.Snapshot(self.repo, time.monotonic() - 1).policy(base)
        with mock.patch.object(gate, 'MAX_METADATA_BYTES', 1):
            # Explicit limit exercises the same runtime bound as path collection.
            with self.assertRaises((ValueError, gate.scan.ScanError)):
                snapshot.git('diff', '--name-only', base, head, limit=1)
        self.put('.github/review-policy.toml', 'x' * (gate.MAX_POLICY_BYTES + 1))
        huge = self.commit()
        with self.assertRaises(ValueError):
            snapshot.policy(huge)
        (self.repo / '.github/review-policy.toml').unlink()
        (self.repo / '.github/review-policy.toml').symlink_to('../src/manual.js')
        linked = self.commit()
        with self.assertRaises(ValueError):
            snapshot.policy(linked)

    def test_acquisition_caps_apply_before_output_is_read_and_preserve_auth_in_environment(self):
        with self.assertRaises(gate.scan.ScanError):
            gate.scan.command(['python3', '-c', 'print("x" * 10000)'], max_bytes=1024)
        with self.assertRaises(gate.scan.ScanError):
            gate.scan.command(['python3', '-c', 'print("x" * 10000)'], max_bytes=20000, output_limit=1024)
        with self.assertRaises(gate.scan.ScanError):
            gate.scan.command(['python3', '-c', 'import time;time.sleep(2)'], timeout=0.01, max_bytes=1024)
        with mock.patch.object(gate.scan, 'command', return_value=b'') as command:
            gate.scan.fetch(self.repo, 'https://github.com/example/synthetic.git', '', 'a' * 40,
                            'synthetic-token-sentinel', depth=1, timeout=10, max_bytes=gate.MAX_FETCH_BYTES)
        self.assertEqual([call.kwargs['timeout'] for call in command.call_args_list], [180, 180, 10])
        args, kwargs = command.call_args
        self.assertNotIn('synthetic-token-sentinel', str(args))
        self.assertIn('--depth=1', args[0])
        self.assertEqual(kwargs['max_bytes'], gate.MAX_FETCH_BYTES)
        self.assertEqual(kwargs['env']['GIT_CONFIG_KEY_0'], 'http.https://github.com/.extraheader')
        with mock.patch.object(gate.scan, 'command') as command:
            with self.assertRaisesRegex(gate.scan.ScanError, 'acquisition deadline expired'):
                gate.scan.fetch(self.repo, 'https://github.com/example/synthetic.git', '', 'a' * 40, '',
                                deadline=time.monotonic() - 1, max_bytes=gate.MAX_FETCH_BYTES)
            command.assert_not_called()

    def test_actual_git_setup_and_fetch_share_the_acquisition_deadline(self):
        tools = Path(self.directory.name) / 'tools'
        tools.mkdir()
        executable = tools / 'git'
        executable.write_text(f'#!{sys.executable}\n' + """
import os
from pathlib import Path
import sys
import time
stage = sys.argv[3]
with Path(os.environ['FAKE_GIT_LOG']).open('a') as log:
    log.write(stage + ':started\\n')
if os.environ['FAKE_GIT_SLOW_STAGE'] == 'each':
    time.sleep(0.25)
elif stage == os.environ['FAKE_GIT_SLOW_STAGE']:
    time.sleep(3)
with Path(os.environ['FAKE_GIT_LOG']).open('a') as log:
    log.write(stage + ':completed\\n')
""")
        executable.chmod(0o700)
        stages = ('init', 'remote', 'fetch')
        for index, stage in enumerate((*stages, 'each')):
            with self.subTest(stage=stage):
                log = tools / (stage + '.log')
                env = dict(os.environ, PATH=str(tools) + os.pathsep + os.environ['PATH'],
                           FAKE_GIT_LOG=str(log), FAKE_GIT_SLOW_STAGE=stage)
                started = time.monotonic()
                budget = 0.6 if stage == 'each' else 0.25
                with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(gate, 'ACQUISITION_SECONDS', budget):
                    refusal = 'bounded Git (command timed out|acquisition deadline expired)' if stage == 'each' else 'bounded Git command timed out'
                    with self.assertRaisesRegex(gate.scan.ScanError, refusal):
                        self.enforce('a' * 40, 'b' * 40, remote='https://github.com/example/synthetic.git')
                self.assertLess(time.monotonic() - started, 2)
                actual = log.read_text().splitlines()
                if stage == 'each':
                    # Individually short commands still share one total budget.
                    self.assertIn('init:completed', actual)
                    self.assertNotIn('fetch:completed', actual)
                else:
                    self.assertEqual(actual, [line for previous in stages[:index]
                                     for line in (previous + ':started', previous + ':completed')] + [stage + ':started'])

    def test_actual_cli_refuses_missing_failed_skipped_and_omitted_required_proof_jobs(self):
        base, head = self.fixture()
        event = Path(self.directory.name) / 'event.json'
        event.write_text(json.dumps({'repository': {'full_name': 'example/synthetic'}, 'before': base}))
        env = dict(os.environ, GITHUB_EVENT_NAME='push', GITHUB_EVENT_PATH=str(event), GITHUB_SHA=head,
                   GITHUB_REPOSITORY='example/synthetic', GITHUB_WORKSPACE=str(self.repo), GENERATED_POLICY_TOKEN='')
        for needs, required, expected in [(results(regenerate='success'), 'regenerate', 0),
                (results(regenerate='skipped'), 'regenerate', 1), (results(regenerate='failure'), 'regenerate', 1),
                (results(tests='success'), 'tests', 1), (results(regenerate='success'), 'tests', 1)]:
            with self.subTest(needs=needs, required=required):
                result = subprocess.run(['python3', str(SCRIPT)], env=dict(env, NEEDS=json.dumps(needs), REQUIRED=required),
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                self.assertIn('Generated provenance:', result.stdout)
        # Public diagnostics never include caller-controlled credentials or raw Git errors.
        env.update(GITHUB_WORKSPACE='', GITHUB_SERVER_URL='unsafe://secret-sentinel')
        result = subprocess.run(['python3', str(SCRIPT)], env=dict(env, NEEDS='{}', REQUIRED='tests'), capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('secret-sentinel', result.stdout + result.stderr)
        self.assertIn('could not be verified', result.stdout)


if __name__ == '__main__':
    unittest.main()
