"""The stack action's shared-pin format guard runs without GitHub access."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / '.github/actions/stack-conformance'


class CallerGate(unittest.TestCase):
    def check(self, ref, *, repository='example/private', token=''):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow = root / '.github/workflows/ci.yml'
            workflow.parent.mkdir(parents=True)
            workflow.write_text('jobs:\n  gate:\n    steps:\n'
                                f'      - uses: {ref}\n')
            def git(*args):
                return subprocess.check_output(['git', *args], cwd=root, text=True).strip()
            git('init', '-qb', 'main')
            git('add', '.')
            git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'Fixture')
            gh = root / 'gh'
            gh.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$RUNNER_TEMP/gh-calls"\nexit 1\n')
            gh.chmod(0o755)
            action = yaml.safe_load((ACTION / 'action.yml').read_text())
            self.assertEqual(set(action['inputs']),
                             {'base', 'head', 'baseline', 'model-aliases', 'auth-surface', 'auth-baseline', 'token'})
            script = action['runs']['steps'][0]['run']
            result = subprocess.run(['bash', '-e', '-c', script], cwd=root, capture_output=True, text=True,
                                    env={**os.environ, 'PATH': directory + os.pathsep + os.environ['PATH'],
                                         'GH_TOKEN': token, 'GITHUB_REPOSITORY': repository,
                                         'GITHUB_ACTION_PATH': str(ACTION), 'GITHUB_OUTPUT': str(root / 'output'),
                                         'RUNNER_TEMP': directory, 'BASE': git('rev-parse', 'HEAD'), 'HEAD_REV': 'HEAD',
                                         'BASELINE': '.github/stack-departures.txt', 'AUTH_SURFACE': 'full',
                                         'AUTH_BASELINE': '', 'MODEL_ALIASES': ''})
            log = root / 'gh-calls'
            report = json.loads((root / 'stack-conformance-report.json').read_text())
            return result, report, log.read_text().splitlines() if log.exists() else []

    def test_accepts_full_sha_without_comment_target_lookup_or_ancestry(self):
        sha = 'a' * 40
        for ref in (f'SylphxAI/.github/.github/actions/absent@{sha}',
                    f'"sylphxai/.github/.github/workflows/absent.yml@{sha}" # descriptive comment'):
            with self.subTest(ref=ref):
                result, report, calls = self.check(ref)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(report['shared_pin_errors'], [])
                self.assertEqual(calls, [])

    def test_rejects_mutable_short_and_missing_pins(self):
        for suffix in ('@main', '@v1', '@abcdef01', ''):
            with self.subTest(suffix=suffix):
                result, report, calls = self.check('SylphxAI/.github/.github/actions/example' + suffix)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn('.github/workflows/ci.yml:4:', report['shared_pin_errors'][0])
                self.assertIn('must use a full commit SHA', result.stdout)
                self.assertEqual(calls, [])

    def test_action_property_lookup_failure_does_not_block_self_reference(self):
        result, report, calls = self.check('SylphxAI/.github/.github/workflows/release.yml@main',
                                           repository='SylphxAI/.github', token='fixture')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(report['passed'])
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].startswith('api repos/SylphxAI/.github/properties/values '))
