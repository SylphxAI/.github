"""The repair job resolves its own incident only after clean handling."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

from tests.red_main_programs import ROOT, program

JOBS = yaml.safe_load((ROOT / '.github/workflows/red-main.yml').read_text())['jobs']
STEPS = {step.get('name'): step for step in JOBS['red-main']['steps']}
CLOSE = STEPS['Close red-main handler alerts on recovery']
ALERT = STEPS['Alert that the red-main handler itself failed']
VERDICT = STEPS['Decide the verdict of the failed run']
REPO = 'example/project'
RUN_URL = f'https://github.com/{REPO}/actions/runs/293'
ISSUES = f'repos/{REPO}/issues'
LIST = f'{ISSUES}?state=open&labels=red-main-broken&per_page=10'
FILTER = '.[] | select(has("pull_request") | not) | .number'

# The double serves each JSON page and runs the script's actual jq filter.
# It logs requests before rejecting them and allows one API call to fail.
GH = '''#!/usr/bin/env python3
import json
import os
import subprocess
import sys

args = sys.argv[1:]
assert args.pop(0) == 'api', args
method = 'GET'
if args[0] == '--method':
    args.pop(0)
    method = args.pop(0)
paginate = args[0] == '--paginate'
if paginate:
    args.pop(0)
path = args.pop(0)
with open(os.environ['GH_LOG'], 'a') as log:
    print(json.dumps([method, path, *args]), file=log)
if os.environ['FAIL_CALL'] == f'{method} {path}':
    print('gh: Resource not accessible by integration (HTTP 403)', file=sys.stderr)
    sys.exit(1)
if method == 'GET' and '/issues?state=open' in path:
    pages = json.loads(os.environ['PAGES'])
    if pages:
        jq_filter = args[args.index('--jq') + 1]
        for page in pages if paginate else pages[:1]:
            subprocess.run(['jq', '-r', jq_filter], input=json.dumps(page), text=True, check=True)
'''


class RecoveryContractTest(unittest.TestCase):
    def test_workflow_conditions_keep_failure_and_recovery_disjoint(self):
        self.assertEqual(JOBS['red-main']['if'], "inputs.mode == 'repair'")
        self.assertNotIn(CLOSE, JOBS['lifecycle']['steps'])
        self.assertEqual(ALERT['if'], 'failure()')
        self.assertEqual(CLOSE['if'], (
            "success() && steps.gate.outputs.proceed == 'true'"
            " && steps.probe.outcome == 'success'"
            " && (steps.verdict.outputs.verdict == 'real' || steps.verdict.outputs.verdict == 'infra')"
            " && (steps.key.outputs.write == 'yes' || steps.gate.outputs.mode == 'notify')"
            " && (steps.key.outputs.path != 'app' || steps.app-token-act.outputs.token != '')"
            " && (steps.key.outputs.path != 'broker' || steps.broker-token-act.outputs.token != '')"
        ))
        self.assertEqual(CLOSE['env']['GH_TOKEN'], '${{ github.token }}')
        self.assertIn('github.run_id', CLOSE['env']['RUN_URL'])


@unittest.skipUnless(shutil.which('jq'), 'jq is needed to simulate gh API filtering')
class RecoveryShellTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        self.log = self.temp / 'gh-log'
        gh = self.temp / 'gh'
        gh.write_text(GH)
        gh.chmod(0o755)
        self.work = self.temp / 'red-main'
        self.state = self.work / 'state'
        self.state.mkdir(parents=True)
        (self.work / 'lib.sh').write_text(program('lib.sh'))

    def run_step(self, step=CLOSE, pages=(), fail='', **env):
        self.log.write_text('')
        proc = subprocess.run(['bash', '-e', '-c', step['run']], text=True,
                              capture_output=True, timeout=10, env={
            **os.environ, 'PATH': f'{self.temp}:{os.environ["PATH"]}',
            'GH_LOG': str(self.log), 'RUNNER_TEMP': str(self.temp),
            'REPO': REPO, 'RUN_URL': RUN_URL, 'TRUNK': 'main',
            'TRIGGER_URL': '', 'OWNER_LABEL': 'owner:ops',
            'PAGES': json.dumps(pages), 'FAIL_CALL': fail, **env,
        })
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc.stdout, [json.loads(line) for line in self.log.read_text().splitlines()]

    def assert_closed(self, calls, *numbers):
        self.assertEqual(calls, [['GET', LIST, '--jq', FILTER]] + [
            call for number in numbers for call in (
                ['POST', f'{ISSUES}/{number}/comments', '-f',
                 f'body=Resolved: the red-main handler completed cleanly on `{REPO}`: {RUN_URL}.'],
                ['PATCH', f'{ISSUES}/{number}', '-f', 'state=closed', '-f', 'state_reason=completed'],
            )
        ])

    def test_comments_then_closes_one_issue(self):
        _, calls = self.run_step(pages=[[{'number': 21}]])
        self.assert_closed(calls, 21)

    def test_paginates_ignoring_labeled_pull_requests(self):
        _, calls = self.run_step(pages=[
            [{'number': 21}, {'number': 22, 'pull_request': {}}], [{'number': 31}],
        ])
        self.assert_closed(calls, 21, 31)

    def test_no_open_issues_is_read_only(self):
        out, calls = self.run_step(pages=[[]])
        self.assertIn('no open red-main handler alerts', out)
        self.assert_closed(calls)

    def test_successful_early_exit_or_soft_block_does_not_even_list_issues(self):
        for marker in ('quiet', 'blocked', 'grant-failure', 'incomplete'):
            with self.subTest(marker=marker):
                path = self.state / marker
                path.touch()
                out, calls = self.run_step(pages=[[{'number': 21}]])
                self.assertIn(f'leaving handler alerts open: {marker}', out)
                self.assertEqual(calls, [])
                path.unlink()

    def test_real_infra_escalation_failure_leaves_existing_handler_alert_open(self):
        self.run_step(VERDICT, fail=f'POST {ISSUES}', INITIAL_PROOF='failure',
                      INFRA='yes', INFRA_SIGNATURE='runner-lost', HEAD_SHA='a' * 40,
                      VERIFY_WORKFLOW='verify.yml', FAILED_LANES='build', OPS_ISSUE='',
                      GITHUB_OUTPUT=str(self.temp / 'output'))
        self.assertIn('verdict=infra', (self.temp / 'output').read_text())
        self.assertEqual((self.state / 'incomplete').read_text(), 'infrastructure escalation failed')
        _, calls = self.run_step(pages=[[{'number': 21}]])
        self.assertEqual(calls, [])

    def test_api_list_failure_warns_without_writes(self):
        out, calls = self.run_step(fail=f'GET {LIST}')
        self.assertIn('::warning::', out)
        self.assert_closed(calls)

    def test_comment_or_close_failure_warns_and_does_not_stop_later_issues(self):
        for method, suffix in (('POST', '/comments'), ('PATCH', '')):
            with self.subTest(method=method):
                out, calls = self.run_step(pages=[[{'number': 21}, {'number': 31}]],
                                           fail=f'{method} {ISSUES}/21{suffix}')
                self.assertIn('::warning::', out)
                self.assertIn('closed red-main handler alert #31', out)
                self.assertNotIn('closed red-main handler alert #21', out)
                expected = ['GET', 'POST', 'POST', 'PATCH']
                if method == 'PATCH':
                    expected.insert(2, 'PATCH')
                self.assertEqual([call[0] for call in calls], expected)

    def test_failure_alert_creates_or_comments_but_never_closes(self):
        for pages, endpoint in (([[]], ISSUES), ([[{'number': 21}]], f'{ISSUES}/21/comments')):
            with self.subTest(endpoint=endpoint):
                _, calls = self.run_step(ALERT, pages=pages)
                self.assertEqual(calls[-1][:2], ['POST', endpoint])
                self.assertIn(RUN_URL, ' '.join(calls[-1]))
                self.assertIn('red-main-broken', str(calls))
                self.assertNotIn('PATCH', [call[0] for call in calls])
