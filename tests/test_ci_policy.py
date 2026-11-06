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
        self.assertEqual(set(self.action['inputs']), {'token', 'contribution-scope'})
        self.assertEqual(self.action['inputs']['token']['default'], '${{ github.token }}')
        self.assertEqual(self.action['inputs']['contribution-scope']['default'], 'all')
        steps = self.action['runs']['steps']
        self.assertEqual(len(steps), 2)
        for step in steps:
            self.assertEqual(set(step), {'name', 'shell', 'env', 'run'})
            self.assertEqual(step['shell'], 'bash')
        self.assertEqual(steps[0]['env'], {
            'CONTRIBUTION_POLICY_TOKEN': '${{ inputs.token }}',
            'CONTRIBUTION_POLICY_SCOPE': '${{ inputs.contribution-scope }}',
        })
        self.assertEqual(steps[1]['env'], {'SECRET_SCAN_TOKEN': '${{ inputs.token }}'})

    def invoke(self, *, metadata_exit=0, scanner_exit=0, scope='all'):
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
                # Resolve the composite's inputs as Actions does, outside
                # the shell. Probe programs stand in for the separately tested
                # policy implementations; their invocation is the contract here.
                inputs = {'${{ inputs.token }}': TOKEN, '${{ inputs.contribution-scope }}': scope}
                step_env = {key: inputs[value] for key, value in step['env'].items()}
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
            {'program': 'contribution_policy.py', 'args': ['--scope', 'all'], 'token': TOKEN},
            {'program': 'secret_scan.py', 'args': ['--fetch', '--pre-merge-only'], 'token': TOKEN},
        ])

    def test_metadata_refusal_stops_before_secret_scanning(self):
        status, calls = self.invoke(metadata_exit=23)
        self.assertEqual(status, 23)
        self.assertEqual(calls, [{'program': 'contribution_policy.py', 'args': ['--scope', 'all'], 'token': TOKEN}])

    def test_commits_scope_uses_existing_validator_without_changing_secret_scan(self):
        status, calls = self.invoke(scope='commits')
        self.assertEqual(status, 0)
        self.assertEqual(calls[0]['args'], ['--scope', 'commits'])
        self.assertEqual(calls[1]['args'], ['--fetch', '--pre-merge-only'])

    def test_scope_is_a_single_literal_argument(self):
        value = 'commits; $(must-not-execute)'
        status, calls = self.invoke(scope=value)
        self.assertEqual(status, 0)
        self.assertEqual(calls[0]['args'], ['--scope', value])

    def test_code_consumer_keeps_metadata_on_its_edit_triggered_producer(self):
        code = yaml.safe_load((ROOT / '.github/workflows/project-control.yml').read_text())
        consumer = next(s for s in code['jobs']['tests']['steps']
                        if s.get('uses') == './.github/actions/ci-policy')
        self.assertEqual(consumer['with']['contribution-scope'], 'commits')
        gate = next(s for s in code['jobs']['ci-ok']['steps']
                    if s.get('uses') == './.github/actions/ci-ok')
        self.assertEqual(gate['with']['separate-pr-metadata'], 'true')
        metadata = yaml.safe_load((ROOT / '.github/workflows/pr-metadata.yml').read_text())
        self.assertIn('edited', metadata.get('on', metadata.get(True))['pull_request']['types'])
        self.assertTrue(any(s.get('uses') == './.github/actions/contribution-policy'
                            for s in metadata['jobs']['pr-metadata']['steps']))

    def test_real_validator_scope_separates_mutable_text_not_commit_identity(self):
        script = ROOT / '.github/actions/contribution-policy/contribution_policy.py'
        identity = {'name': 'Kyle Tse', 'email': 'hi@kylet.se'}
        snapshot = {
            'pull_request': {'number': 1, 'title': 'AI-generated change', 'body': 'Co-authored-by: AI',
                             'user': {'login': 'shtse8', 'id': 8020099, 'type': 'User'}},
            'commits': [{'sha': 'a' * 40, 'commit': {'author': identity, 'committer': identity,
                                                      'message': 'Improve parser'}}],
        }
        with tempfile.TemporaryDirectory(prefix='ci-policy-scope-') as directory:
            path = Path(directory) / 'snapshot.json'
            def check(scope):
                path.write_text(json.dumps(snapshot))
                return subprocess.run(['python3', str(script), '--input', str(path), '--scope', scope],
                                      capture_output=True, text=True).returncode
            self.assertEqual(check('all'), 1)
            self.assertEqual(check('commits'), 0)
            snapshot['commits'][0]['commit']['author'] = {'name': 'Other', 'email': 'other@example.invalid'}
            self.assertEqual(check('commits'), 1)
            self.assertEqual(check('unsupported'), 2)

    def test_secret_refusal_is_not_hidden(self):
        status, calls = self.invoke(scanner_exit=29)
        self.assertEqual(status, 29)
        self.assertEqual([call['program'] for call in calls], ['contribution_policy.py', 'secret_scan.py'])


if __name__ == '__main__':
    unittest.main()
