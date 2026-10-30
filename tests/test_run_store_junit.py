#!/usr/bin/env python3
"""Exact cross-run diagnostic discovery; no gateway wildcard assumptions."""
import importlib.util
import json
import os
import runpy
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / '.github/actions/run-store/get-junit.py'
spec = importlib.util.spec_from_file_location('store_junit', SCRIPT)
store = importlib.util.module_from_spec(spec)
spec.loader.exec_module(store)


class JunitDiscoveryTest(unittest.TestCase):
    def test_matrix_names_deduplicate_and_skip_unexecuted_producers(self):
        steps = [{'name': 'run-store put junit-rust-test-701', 'conclusion': 'success'},
                 {'name': 'run-store put junit-rust-test-1', 'conclusion': 'failure'},
                 {'name': 'run-store put junit-unused', 'conclusion': 'skipped'},
                 {'name': 'upload artifact junit-old', 'conclusion': 'success'}]
        rows = [{'run_id': 44, 'steps': steps}, {'run_id': 44, 'steps': steps}]
        self.assertEqual(store.entries(rows, 44), ['junit-rust-test-1', 'junit-rust-test-701'])

    def test_invalid_identity_or_name_is_rejected(self):
        with self.assertRaises(ValueError):
            store.entries([{'run_id': 45, 'steps': []}], 44)
        with self.assertRaises(ValueError):
            store.entries([{'run_id': 44, 'steps': [{'name': 'run-store put junit-../../escape'}]}], 44)

    def exercise_fetch(self, miss=False, cli=False):
        calls, gets = [], []
        first = [{'run_id': 44, 'name': 'rust-test (1)', 'conclusion': 'skipped', 'steps': []} for _ in range(100)]
        first[0]['steps'] = [{'name': 'run-store put junit-a'}]
        second = [{'run_id': 44, 'name': 'rust-test (701)', 'steps': [{'name': 'run-store put junit-b'}]}]
        def api(path):
            calls.append(path)
            if path == 'repos/tenant/project':
                return {'id': 991}
            return {'jobs': first if path.endswith('page=1') else second}
        def run(args, env, **kwargs):
            gets.append(env)
            Path(env['STORE_PATH']).mkdir(parents=True)
            (Path(env['STORE_PATH']) / 'junit.xml').write_text('<testsuite/>')
            Path(env['GITHUB_OUTPUT']).write_text('found=false\n' if miss and env['NAME'] == 'junit-b' else 'found=true\n')
        with tempfile.TemporaryDirectory() as tmp, patch.object(store, 'api', api), patch.object(store.subprocess, 'run', run), patch.dict(os.environ, {'GITHUB_OUTPUT': str(Path(tmp) / 'out'), 'REPO_ID': 'wrong-source-id'}):
            dest = Path(tmp) / 'reports'
            if cli:
                # Exercise the same entrypoint red-main invokes, with transport
                # fixtures only; importing fetch alone misses CLI-only defects.
                with patch.object(sys, 'argv', [str(SCRIPT), 'tenant/project', '44', str(dest)]), patch.object(store.subprocess, 'check_output', lambda args, **kwargs: json.dumps(api(args[-1]))):
                    with self.assertRaises(SystemExit) as exit_result:
                        runpy.run_path(str(SCRIPT), run_name='__main__')
                    self.assertEqual(exit_result.exception.code, 1 if miss else 0)
            else:
                self.assertEqual(store.fetch('tenant/project', '44', str(dest)), not miss)
            self.assertEqual(dest.exists(), not miss)
            if not miss:
                self.assertTrue((dest / 'junit-b/junit.xml').exists())
                self.assertEqual(json.loads((dest / '.run-store-lanes.json').read_text()),
                                 {'junit-a': 'rust-test (1)', 'junit-b': 'rust-test (701)'})
        self.assertEqual(len(calls), 3)
        self.assertEqual([env['NAME'] for env in gets], ['junit-a', 'junit-b'])
        self.assertTrue(all(env['REPO_ID'] == '991' and env['STORE_RUN_ID'] == '44' and env['MODE'] == 'get' for env in gets))

    def test_pagination_target_repository_and_exact_keys(self):
        self.exercise_fetch()

    def test_consumer_entrypoint_fetches_exact_cross_run_shards(self):
        self.exercise_fetch(cli=True)

    def test_consumer_entrypoint_reports_unavailable_partial_shards(self):
        self.exercise_fetch(miss=True, cli=True)

    def test_partial_shard_fetch_is_not_passing_evidence(self):
        self.exercise_fetch(miss=True)

    def test_empty_api_steps_use_exact_completed_job_log_receipts_and_keep_matrix_lanes(self):
        rows = [{'id': shard, 'run_id': 44, 'status': 'completed', 'name': f'JUnit fixture ({shard})',
                 'steps': []} for shard in (1, 701)]
        def log(repo, job_id):
            self.assertEqual(repo, 'tenant/project')
            return (f'2026-10-30T18:48:28.0842415Z run-store put junit-consumer-{job_id}: '
                    f'291 bytes as run-store.v1.991.44.junit-consumer-{job_id}\n')
        calls = []
        def api(path):
            return {'id': 991} if path == 'repos/tenant/project' else {'jobs': rows}
        def get(args, env, **kwargs):
            calls.append(env['NAME'])
            Path(env['STORE_PATH']).mkdir(parents=True)
            (Path(env['STORE_PATH']) / 'junit.xml').write_text('<testsuite/>')
            Path(env['GITHUB_OUTPUT']).write_text('found=true\n')
        with tempfile.TemporaryDirectory() as tmp, patch.object(store, 'api', api), \
                patch.object(store, 'job_log', log), patch.object(store.subprocess, 'run', get), \
                patch.dict(os.environ, {'GITHUB_OUTPUT': str(Path(tmp) / 'out')}):
            destination = Path(tmp) / 'reports'
            self.assertTrue(store.fetch('tenant/project', 44, destination))
            self.assertEqual(json.loads((destination / '.run-store-lanes.json').read_text()),
                             {f'junit-consumer-{shard}': f'JUnit fixture ({shard})' for shard in (1, 701)})
        self.assertEqual(calls, ['junit-consumer-1', 'junit-consumer-701'])

    def test_log_discovery_refuses_commands_packed_bytes_and_wrong_store_identity(self):
        lines = [
            '##[start-action display=run-store put junit-a;id=__self.run]',
            'run-store put junit-a: packed_bytes=291',
            'run-store put junit-a: 291 bytes as run-store.v1.992.44.junit-a',
            'run-store put junit-a: 291 bytes as run-store.v1.991.45.junit-a',
            'run-store put junit-a: 291 bytes as run-store.v1.991.44.junit-b',
            'echo run-store put junit-a: 291 bytes as run-store.v1.991.44.junit-a',
        ]
        for line in lines[:-1]:
            with self.subTest(line=line), self.assertRaisesRegex(ValueError, 'completed store receipt'):
                store.log_entries('2026-10-30T18:48:28.0842415Z ' + line, 991, 44)
        self.assertEqual(store.log_entries('2026-10-30T18:48:28.0842415Z ' + lines[-1], 991, 44), [])
        retained = '2026-10-30T18:48:28.0842415Z run-store put junit-kept: an entry already exists; it is kept\n'
        self.assertEqual(store.log_entries(retained, 991, 44), ['junit-kept'])

    def test_one_good_and_one_failed_omitted_step_producer_refuses_before_any_get(self):
        rows = [{'id': shard, 'run_id': 44, 'status': 'completed', 'name': f'JUnit fixture ({shard})',
                 'steps': []} for shard in (1, 701)]
        def api(path):
            return {'id': 991} if path == 'repos/tenant/project' else {'jobs': rows}
        def log(repo, job_id):
            start = f'2026-10-30T18:48:26.0635479Z ##[start-action display=run-store put junit-consumer-{job_id};id=__self.run]\n'
            if job_id == 1:
                return start + ('2026-10-30T18:48:28.0842415Z run-store put junit-consumer-1: '
                                '291 bytes as run-store.v1.991.44.junit-consumer-1\n')
            return start + ('2026-10-30T18:48:26.9483174Z run-store put junit-consumer-701: packed_bytes=293\n'
                            '2026-10-30T18:48:28.0842415Z ::warning::run-store put junit-consumer-701: could not store the bundle\n')
        with tempfile.TemporaryDirectory() as tmp, patch.object(store, 'api', api), \
                patch.object(store, 'job_log', log), patch.object(store.subprocess, 'run') as get:
            destination = Path(tmp) / 'reports'
            with self.assertRaisesRegex(ValueError, 'refusing partial shard discovery'):
                store.fetch('tenant/project', 44, destination)
            get.assert_not_called()
            self.assertFalse(destination.exists())

    def test_unavailable_or_empty_discovery_never_exposes_a_partial_shard_set(self):
        rows = [{'id': shard, 'run_id': 44, 'status': 'completed', 'name': str(shard), 'steps': []}
                for shard in (1, 701)]
        for log, error in [(lambda *args: '', 'no JUnit producer keys'),
                           (lambda *args: (_ for _ in ()).throw(ValueError('log unavailable')), 'log unavailable')]:
            with self.subTest(error=error), patch.object(store, 'job_log', log):
                with self.assertRaisesRegex(ValueError, error):
                    store.discover(rows, 'tenant/project', 991, 44)
        with patch.object(store, 'job_log') as log:
            rows[-1]['run_id'] = 45
            with self.assertRaisesRegex(ValueError, 'another run'):
                store.discover(rows, 'tenant/project', 991, 44)
            log.assert_not_called()

    def test_authenticated_log_read_bounds_bytes_and_does_not_echo_transport_output(self):
        popen = subprocess.Popen
        for program, limit, expected in [
                ('print("x" * 64)', 32, 'byte limit'),
                ('import sys; sys.stderr.write("private transport detail"); sys.exit(1)', 1024, 'unavailable')]:
            with self.subTest(expected=expected), patch.object(store, 'LOG_LIMIT', limit), \
                    patch.object(store.subprocess, 'Popen',
                                 side_effect=lambda *args, **kwargs: popen([sys.executable, '-c', program], **kwargs)):
                with self.assertRaisesRegex(ValueError, expected) as caught:
                    store.job_log('tenant/project', 1)
                self.assertNotIn('private transport detail', str(caught.exception))

    def test_sharded_nested_reports_keep_exact_test_and_job_identity(self):
        junit_py = (ROOT / '.github/red_main/junit.py').read_text()
        window_py = (ROOT / '.github/red_main/window.py').read_text()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / 'junit-rust-test-701/nested'
            report.mkdir(parents=True)
            (report / 'junit.xml').write_text('<testsuite><testcase classname="suite" name="broken"><failure/></testcase><testcase classname="suite" name="passing"/></testsuite>')
            (root / '.run-store-lanes.json').write_text(json.dumps({'junit-rust-test-701': 'rust-test (701)'}))
            output = root / 'units.tsv'
            with patch.object(sys, 'argv', ['junit.py', str(root), str(output), 'rust-test (701)']):
                exec(compile(junit_py, 'junit.py', 'exec'), {})
            self.assertEqual(output.read_text(), 'test\tsuite::broken\trust-test (701)\n')
            namespace = {'__name__': 'fixture'}
            exec(compile(window_py, 'window.py', 'exec'), namespace)
            failed, passed, covered = namespace['read_junit'](str(root))
            self.assertEqual(failed, {('suite::broken', 'rust-test (701)')})
            self.assertEqual(passed, {('suite::passing', 'rust-test (701)')})
            self.assertEqual(covered, {'rust-test (701)'})

    def test_all_red_main_reads_use_trusted_run_store_helper(self):
        text = (ROOT / '.github/workflows/red-main.yml').read_text()
        self.assertNotIn('"run", "download"', text)
        self.assertNotIn('gha run download', text)
        self.assertIn('ref: ${{ job.workflow_sha }}', text)
        self.assertEqual(text.count('python3 "$RUN_STORE_JUNIT"'), 3)
        self.assertIn('env["RUN_STORE_JUNIT"]', (ROOT / '.github/red_main/window.py').read_text())
        producer = (ROOT / 'workflow-templates/optimistic-verify.yml').read_text()
        self.assertNotIn('actions/upload-artifact', producer)
        self.assertIn('name: run-store put junit-integration', producer)
        self.assertIn('id-token: write', producer)


if __name__ == '__main__':
    unittest.main()
