"""Exercise red-main's copied and sparse-checkout run-store distributions."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

from tests.test_run_store import Fake, OIDC_REQUEST_TOKEN

ROOT = Path(__file__).resolve().parents[1]


class RedMainStoreStagingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name)

    def snapshot(self):
        action = yaml.safe_load((ROOT / '.github/actions/red-main/action.yml').read_text())
        step = next(step for step in action['runs']['steps'] if step.get('id') == 'programs')
        result = subprocess.run(
            ['bash', '--noprofile', '--norc', '-e', '-o', 'pipefail', '-c', step['run']],
            cwd=self.workspace,
            env={'PATH': os.environ['PATH'], 'GITHUB_ACTION_PATH': str(ROOT / '.github/actions/red-main')},
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.workspace / '.red-main-store'

    def checkout(self, omit_shared=False):
        workflow = yaml.safe_load((ROOT / '.github/workflows/red-main.yml').read_text())
        step = next(step for step in workflow['jobs']['red-main']['steps']
                    if step.get('name') == 'Read canonical run-store consumer')
        patterns = step['with']['sparse-checkout'].splitlines()
        if omit_shared:
            patterns = [pattern for pattern in patterns if pattern != '.github/actions/_shared']
        stage = self.workspace / step['with']['path']
        # Use Git's real cone-mode materialization, as actions/checkout does,
        # rather than treating a path's presence in YAML as dependency closure.
        env = {'PATH': os.environ['PATH'], 'GIT_CONFIG_NOSYSTEM': '1',
               'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_TEMPLATE_DIR': ''}
        for command, data in (
            (['git', 'clone', '--quiet', '--no-checkout', '--shared', str(ROOT), str(stage)], None),
            (['git', '-C', str(stage), 'sparse-checkout', 'set', '--cone', '--stdin'], '\n'.join(patterns) + '\n'),
            (['git', '-C', str(stage), 'checkout', '--quiet', 'HEAD'], None),
        ):
            result = subprocess.run(command, input=data, env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
        return stage

    def upload(self, stage):
        fake = Fake()
        self.addCleanup(fake.close)
        source = self.workspace / 'payload.txt'
        source.write_text('staged consumer payload')
        output = self.workspace / 'outputs'
        output.write_text('')
        env = {
            'PATH': os.environ['PATH'],
            'LD_LIBRARY_PATH': os.environ.get('LD_LIBRARY_PATH', ''),
            'MODE': 'put', 'NAME': 'staged', 'STORE_PATH': str(source),
            'STORE_RUN_ID': '4242', 'REPO_ID': '77', 'REQUIRED': 'true',
            'BUILD_CACHE_URL': f'http://127.0.0.1:{fake.port}',
            'BUILD_CACHE_NETWORK': 'public',
            'ACTIONS_ID_TOKEN_REQUEST_URL': f'http://127.0.0.1:{fake.port}/oidc?x=1',
            'ACTIONS_ID_TOKEN_REQUEST_TOKEN': OIDC_REQUEST_TOKEN,
            'GITHUB_OUTPUT': str(output), 'RUNNER_TEMP': str(self.workspace),
            'RUN_STORE_RETRY_DELAY': '0',
        }
        result = subprocess.run(
            ['bash', str(stage / '.github/actions/run-store/run-store.sh')],
            cwd=self.workspace, env=env, capture_output=True, text=True, timeout=15,
        )
        return result, fake

    def assert_upload_succeeds(self, stage):
        helper = stage / '.github/actions/_shared/curl_retry.py'
        self.assertTrue(helper.is_file(), f'missing staged dependency: {helper}')
        result, fake = self.upload(stage)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual([method for method, _, _ in fake.requests], ['GET', 'POST', 'PUT', 'HEAD'])
        self.assertIn('run-store.v1.77.4242.staged', fake.store)

    def assert_missing_helper_fails(self, stage):
        result, fake = self.upload(stage)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn('_shared/curl_retry.py', result.stderr)
        self.assertIn('No such file or directory', result.stderr)
        self.assertIn('OIDC token request: curl exit 2', result.stdout)
        self.assertEqual(fake.requests, [])

    def test_composite_snapshot_stages_shared_helper_and_replaces_stale_copy(self):
        stage = self.snapshot()
        shared = stage / '.github/actions/_shared'
        self.assertTrue((shared / 'curl_retry.py').is_file(), 'snapshot omitted the shared retry helper')
        (shared / 'stale.py').write_text('stale helper')
        self.snapshot()
        self.assertFalse((shared / 'stale.py').exists())
        self.assert_upload_succeeds(stage)

    def test_composite_without_shared_helper_cannot_run_store(self):
        stage = self.snapshot()
        shutil.rmtree(stage / '.github/actions/_shared', ignore_errors=True)
        self.assert_missing_helper_fails(stage)

    def test_workflow_sparse_checkout_stages_shared_helper(self):
        self.assert_upload_succeeds(self.checkout())

    def test_run_store_only_sparse_checkout_cannot_run_store(self):
        self.assert_missing_helper_fails(self.checkout(omit_shared=True))


if __name__ == '__main__':
    unittest.main()
