#!/usr/bin/env python3
"""Exact cross-run diagnostic discovery; no gateway wildcard assumptions."""
import importlib.util
import json
import os
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

    def exercise_fetch(self, miss=False):
        calls, gets = [], []
        first = [{'run_id': 44, 'name': 'rust-test (1)', 'steps': []} for _ in range(100)]
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

    def test_partial_shard_fetch_is_not_passing_evidence(self):
        self.exercise_fetch(miss=True)

    def test_sharded_nested_reports_keep_exact_test_and_job_identity(self):
        workflow = yaml.safe_load((ROOT / '.github/workflows/red-main.yml').read_text())
        env = workflow['jobs']['red-main']['env']
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / 'junit-rust-test-701/nested'
            report.mkdir(parents=True)
            (report / 'junit.xml').write_text('<testsuite><testcase classname="suite" name="broken"><failure/></testcase><testcase classname="suite" name="passing"/></testsuite>')
            (root / '.run-store-lanes.json').write_text(json.dumps({'junit-rust-test-701': 'rust-test (701)'}))
            output = root / 'units.tsv'
            with patch.object(sys, 'argv', ['junit.py', str(root), str(output), 'rust-test (701)']):
                exec(compile(env['JUNIT_PY'], 'junit.py', 'exec'), {})
            self.assertEqual(output.read_text(), 'test\\tsuite::broken\\trust-test (701)\\n')
            namespace = {'__name__': 'fixture'}
            exec(compile(env['WINDOW_PY'], 'window.py', 'exec'), namespace)
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
        self.assertIn('env["RUN_STORE_JUNIT"]', text)
        producer = (ROOT / 'workflow-templates/optimistic-verify.yml').read_text()
        self.assertNotIn('actions/upload-artifact', producer)
        self.assertIn('name: run-store put junit-integration', producer)
        self.assertIn('id-token: write', producer)


if __name__ == '__main__':
    unittest.main()
