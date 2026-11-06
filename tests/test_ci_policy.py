"""Execute the short composite's shell boundary; Project control is the live consumer."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / '.github/actions/ci-policy/action.yml'
TOKEN = 'synthetic value; $(must-not-execute)'
PROBE = '''import json, os, pathlib, sys
with open(os.environ['POLICY_PROBE_LOG'], 'a') as log:
    log.write(json.dumps({
        'program': pathlib.Path(__file__).name,
        'args': sys.argv[1:],
        'token': os.environ.get(PROBE_TOKEN_ENV),
    }) + '\\n')
sys.exit(int(os.environ[PROBE_EXIT_ENV]))
'''


class CiPolicyTest(unittest.TestCase):
    def setUp(self):
        self.action = yaml.safe_load(MANIFEST.read_text())

    def test_manifest_is_only_two_required_policy_steps(self):
        self.assertEqual(self.action['runs']['using'], 'composite')
        self.assertEqual(set(self.action['inputs']), {'token'})
        self.assertEqual(self.action['inputs']['token']['default'], '${{ github.token }}')
        steps = self.action['runs']['steps']
        self.assertEqual(len(steps), 2)
        for step in steps:
            self.assertEqual(set(step), {'name', 'shell', 'env', 'run'})
            self.assertEqual(step['shell'], 'bash')
        self.assertEqual(steps[0]['env'], {'CONTRIBUTION_POLICY_TOKEN': '${{ inputs.token }}'})
        self.assertEqual(steps[1]['env'], {'SECRET_SCAN_TOKEN': '${{ inputs.token }}'})

    def invoke(self, *, metadata_exit=0, scanner_exit=0):
        with tempfile.TemporaryDirectory(prefix='ci policy ') as directory:
            root = Path(directory)
            action = root / '.github/actions/ci-policy'
            action.mkdir(parents=True)
            for sibling, program, token_env, exit_env in (
                ('contribution-policy', 'contribution_policy.py', 'CONTRIBUTION_POLICY_TOKEN', 'METADATA_EXIT'),
                ('secret-scan', 'secret_scan.py', 'SECRET_SCAN_TOKEN', 'SCANNER_EXIT'),
            ):
                target = action.parent / sibling
                target.mkdir()
                (target / program).write_text(
                    f'PROBE_TOKEN_ENV = {token_env!r}\nPROBE_EXIT_ENV = {exit_env!r}\n' + PROBE)
            log = root / 'calls.jsonl'
            env = {
                **os.environ,
                'GITHUB_ACTION_PATH': str(action),
                'POLICY_PROBE_LOG': str(log),
                'METADATA_EXIT': str(metadata_exit),
                'SCANNER_EXIT': str(scanner_exit),
            }
            status = 0
            for step in self.action['runs']['steps']:
                # Resolve the composite's single input as Actions does, outside
                # the shell. Probe programs stand in for the separately tested
                # policy implementations; their invocation is the contract here.
                step_env = {key: TOKEN for key, value in step['env'].items()
                            if value == '${{ inputs.token }}'}
                result = subprocess.run(['bash', '--noprofile', '--norc', '-e', '-o', 'pipefail', '-c', step['run']],
                                        cwd=root, env={**env, **step_env}, capture_output=True, text=True)
                self.assertEqual(result.stdout + result.stderr, '')
                status = result.returncode
                # GitHub's default success() skips later steps after a failure.
                if status:
                    break
            calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
            return status, calls

    def test_invokes_existing_programs_in_order_with_exact_scan_flags(self):
        status, calls = self.invoke()
        self.assertEqual(status, 0)
        self.assertEqual(calls, [
            {'program': 'contribution_policy.py', 'args': [], 'token': TOKEN},
            {'program': 'secret_scan.py', 'args': ['--fetch', '--pre-merge-only'], 'token': TOKEN},
        ])

    def test_metadata_refusal_stops_before_secret_scanning(self):
        status, calls = self.invoke(metadata_exit=23)
        self.assertEqual(status, 23)
        self.assertEqual(calls, [{'program': 'contribution_policy.py', 'args': [], 'token': TOKEN}])

    def test_secret_refusal_is_not_hidden(self):
        status, calls = self.invoke(scanner_exit=29)
        self.assertEqual(status, 29)
        self.assertEqual([call['program'] for call in calls], ['contribution_policy.py', 'secret_scan.py'])


if __name__ == '__main__':
    unittest.main()
