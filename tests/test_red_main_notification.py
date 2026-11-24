#!/usr/bin/env python3
"""Exercise the actual red-main act and audit shells against a local forge."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

from tests.red_main_programs import PROGRAMS, ROOT

REPO = 'fixture/project'
CANDIDATES = {f'{n:040x}': 100 + n for n in range(1, 26)}
BASE = 'a' * 40
HEAD = 'b' * 40
MARKER = '<!-- red-main-window:v1 -->'
WORKFLOW = yaml.safe_load((ROOT / '.github/workflows/red-main.yml').read_text())
STEPS = {s['name']: s['run'] for s in WORKFLOW['jobs']['red-main']['steps'] if 'run' in s}
ACT = STEPS['Revert the culprit or report it']
RECORD = STEPS['Record what the handler did']

FORGE = r'''
import json, os, pathlib, subprocess, sys
root = pathlib.Path(os.environ['RUNNER_TEMP'])
args = sys.argv[1:]
with (root / 'calls').open('a') as log:
    print(json.dumps(args), file=log)
repo = 'repos/' + os.environ['REPO']
fixture = json.loads((root / 'fixture.json').read_text())
if args[:2] == ['pr', 'merge']:
    sys.exit(0)
if args[:2] == ['pr', 'list']:
    print('[{"number":333}]')
    sys.exit(0)
if args[0] != 'api':
    sys.exit(99)
method = args[2] if args[1] == '--method' else 'GET'
route = args[3] if method != 'GET' else args[2] if args[1] == '--paginate' else args[1]
path = route.removeprefix(repo)
if method != 'GET':
    if path == '/pulls':
        (root / 'revert-payload.json').write_text(sys.stdin.read())
        print(334)
    elif path == '/issues' and any('labels[]=red-main-window' == a for a in args):
        body = next(a[5:] for a in args if a.startswith('body='))
        (root / 'tracking.json').write_text(json.dumps({'number': 700, 'body': body, 'user': {'login': 'builder[bot]'}}))
    elif path == '/issues/700' and method == 'PATCH':
        body = next(a[5:] for a in args if a.startswith('body='))
        (root / 'tracking.json').write_text(json.dumps({'number': 700, 'body': body, 'user': {'login': 'builder[bot]'}}))
    sys.exit(0)
if path.startswith('/commits/') and path.endswith('/pulls'):
    pr = fixture['candidates'][path.split('/')[2]]
    response = [{'number': pr, 'title': 'Example', 'labels': [{'name':'P0'}] if fixture['protected'] else []}]
elif path.startswith('/issues?state=all&labels=red-main-window'):
    # Include a spoof and a PR with the marker; neither may be patched.
    marker = '<!-- red-main-window:v1 -->\n\n'
    response = [{'number': 601, 'body': marker, 'user': {'login': 'contributor'}},
                {'number': 602, 'body': marker, 'user': {'login': 'builder[bot]'}, 'pull_request': {}}]
    if (root / 'tracking.json').exists():
        response.append(json.loads((root / 'tracking.json').read_text()))
elif path.startswith('/issues?state=open&labels=red-main-migration-hold'):
    response = [{'number':701, 'body': fixture['culprit'], 'user': {'login':'builder[bot]'}}] if fixture['existing_hold'] else []
elif path == '/issues/900':
    response = {'number': 900, **({'pull_request': {}} if fixture['ops_is_pr'] else {})}
elif path.startswith('/labels?'):
    response = []
else:
    sys.exit(f'unexpected forge read: {args!r}')
if '--jq' not in args:
    print(json.dumps(response))
    sys.exit(0)
sys.exit(subprocess.run(['jq', '-r', args[args.index('--jq') + 1]], input=json.dumps(response), text=True).returncode)
'''


class WindowNotificationTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        work = self.root / 'red-main'
        self.state = work / 'state'
        self.state.mkdir(parents=True)
        for name in ('lib.sh', 'hold.py', 'migration.py', 'revert_guard.py'):
            dest = 'revert-guard.py' if name == 'revert_guard.py' else name
            (work / dest).write_bytes((PROGRAMS / name).read_bytes())
        (work / 'proof.py').write_text(f'import sys\nprint("success" if sys.argv[5] == "{BASE}" else "failure")\n')
        (self.state / 'proof-verdict').write_text('failure')
        (self.state / 'base').write_text(BASE)
        (self.state / 'grant.tsv').write_text('contents\tgranted\npull-requests\tgranted\n')
        (self.state / 'run-id').write_text('42')
        self.summary = self.root / 'summary'
        self.executable('gh', f'#!{sys.executable}\n' + FORGE)
        self.executable('git', r'''#!/bin/bash
printf '%s\n' "$*" >> "$RUNNER_TEMP/git.calls"
if [[ " $* " == *" diff "* ]]; then printf '%s\0' "$CHANGED_PATH"; fi
''')

    def executable(self, name, script):
        path = self.root / name
        path.write_text(script)
        path.chmod(0o700)

    def exercise(self, *, ops='#900', mode='revert', revert_window='false', count=25,
                 proven=False, protected=False, migration=False, existing_hold=False, ops_is_pr=False):
        for file in ('calls', 'git.calls', 'summary'):
            (self.root / file).unlink(missing_ok=True)
        (self.root / 'red-main' / 'summary.md').unlink(missing_ok=True)
        (self.state / 'range-report').unlink(missing_ok=True)
        (self.state / 'report-written').unlink(missing_ok=True)
        candidates = dict(list(CANDIDATES.items())[:count])
        culprit = next(iter(candidates)) if proven else ''
        (self.state / 'culprit').write_text(culprit)
        (self.state / 'revert-all.json').write_text(json.dumps(list(candidates)))
        (self.root / 'fixture.json').write_text(json.dumps(dict(
            candidates=candidates, culprit=culprit, protected=protected,
            existing_hold=existing_hold, ops_is_pr=ops_is_pr)))
        env = dict(PATH=f'{self.root}:{os.environ["PATH"]}', RUNNER_TEMP=str(self.root), REPO=REPO,
                   TRUNK='main', VERIFY_WORKFLOW='verify.yml', VERIFY_CHECK_NAME='verified',
                   HEAD_SHA=HEAD, SHORT_SHA=HEAD[:9], GITHUB_RUN_ID='52', GITHUB_RUN_ATTEMPT='1',
                   MODE=mode, REVERT_WINDOW=revert_window, OPS_ISSUE=ops, RUN_URL='https://github.com/fixture/project/actions/runs/42',
                   APP_SLUG='builder', APP_ID='123', OWNER_LABEL='owner:ci', PROTECTED_LABELS='P0',
                   PROTECTED_PRS='', GITHUB_STEP_SUMMARY=str(self.summary),
                   CHANGED_PATH='db/migrations/@author-#997.sql' if migration else 'src/app.txt',
                   FAILED_LANES='Rust, Jobs @author #101 https://github.com/fixture/project/pull/102')
        for shell in (ACT, RECORD):
            result = subprocess.run(['bash', '-c', shell], cwd=self.root, env=env,
                                    text=True, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = [json.loads(row) for row in (self.root / 'calls').read_text().splitlines()]
        self.comments = {a[3]: [b for b in calls if b[:4] == a[:4]] for a in calls
                         if a[:3] == ['api', '--method', 'POST'] and a[3].endswith('/comments')}
        self.issue_writes = [a for a in calls if a[:3] in
                             (['api', '--method', 'POST'], ['api', '--method', 'PATCH'])
                             and a[3] in (f'repos/{REPO}/issues', f'repos/{REPO}/issues/700')]
        self.armed = any(a[:3] == ['pr', 'merge', '--auto'] for a in calls)
        git = self.root / 'git.calls'
        self.git = git.read_text() if git.exists() else ''
        self.report = self.summary.read_text()

    def assert_safe(self, body, count):
        self.assertIn('Failed lanes: '+chr(96)+'Rust, Jobs', body)
        self.assertNotIn('@', body)
        self.assertNotRegex(body, r'#\d+|/pulls?/\d+')
        for sha, pr in list(CANDIDATES.items())[:count]:
            self.assertIn(f'{chr(96)}PR {pr}{chr(96)}', body)
            self.assertIn(sha, body)

    def assert_ops_only(self, count):
        self.assertEqual(list(self.comments), [f'repos/{REPO}/issues/900/comments'])
        comments = next(iter(self.comments.values()))
        self.assertEqual(len(comments), 1)
        self.assert_safe(next(a[5:] for a in comments[0] if a.startswith('body=')), count)
        self.assertFalse(self.issue_writes)
        self.assert_safe(self.report.split("Red main at ", 1)[1].split("The consolidated report", 1)[0], count)

    def test_large_window_reports_once_to_ops_without_reverting(self):
        self.exercise()
        self.assert_ops_only(25)
        self.assertFalse(self.git)

    def test_unproven_singleton_is_not_a_culprit_in_notify_mode(self):
        self.exercise(count=1, mode='notify', revert_window='true', ops='900')
        self.assert_ops_only(1)
        self.assertFalse(self.git)

    def test_no_ops_upserts_one_tracking_issue_across_runs_without_comments(self):
        for method in ('POST', 'PATCH'):
            self.exercise(ops='', count=3)
            self.assertFalse(self.comments)
            self.assertEqual(len(self.issue_writes), 1)
            self.assertEqual(self.issue_writes[0][2], method)
            tracker = json.loads((self.root / 'tracking.json').read_text())
            self.assertEqual(tracker['number'], 700)
            self.assertTrue(tracker['body'].startswith(MARKER + '\n'))
            self.assert_safe(tracker['body'], 3)
            self.assert_safe(self.report, 3)

    def test_misconfigured_ops_number_cannot_comment_on_a_pr(self):
        self.exercise(ops_is_pr=True, count=2)
        self.assertFalse(self.comments)
        self.assertFalse(self.issue_writes)
        self.assert_safe(self.report, 2)

    def test_protected_range_and_migration_range_use_same_ops_route(self):
        for case in ('protected', 'migration'):
            with self.subTest(case=case):
                self.exercise(revert_window='true', count=3, **{case: True})
                self.assert_ops_only(3)
                self.assertNotIn(' revert --no-edit ', self.git)
                self.assertFalse(self.armed)

    def test_proven_culprit_still_gets_one_pr_comment_in_notify_mode(self):
        self.exercise(proven=True, mode='notify')
        pr_comments = {path: calls for path, calls in self.comments.items() if '/issues/' in path}
        self.assertEqual([(p, len(c)) for p, c in pr_comments.items()], [(f'repos/{REPO}/issues/101/comments', 1)])
        self.assertFalse(self.git)

    def test_proven_protected_pr_still_gets_a_single_comment(self):
        self.exercise(proven=True, protected=True)
        self.assertEqual(len(self.comments[f'repos/{REPO}/issues/101/comments']), 1)
        self.assertFalse(self.git)
        self.assertFalse(self.armed)

    def test_proven_migration_hold_still_files_issue_and_comments_once(self):
        self.exercise(proven=True, migration=True)
        self.assertEqual(len(self.comments[f'repos/{REPO}/issues/101/comments']), 1)
        self.assertEqual(len(self.comments[f'repos/{REPO}/issues/900/comments']), 1)
        self.assertEqual(len(self.issue_writes), 1)
        self.assertIn('labels[]=red-main-migration-hold', self.issue_writes[0])
        self.assertNotIn(' revert --no-edit ', self.git)
        self.exercise(proven=True, migration=True, existing_hold=True)
        self.assertNotIn(f'repos/{REPO}/issues/101/comments', self.comments)
        self.assertFalse(self.issue_writes)

    def test_proven_auto_revert_still_pushes_arms_and_comments_on_culprit(self):
        self.exercise(proven=True)
        self.assertTrue(self.armed)
        self.assertIn(' revert --no-edit ', self.git)
        self.assertIn(' push origin HEAD:refs/heads/auto-revert/', self.git)
        self.assertEqual(len(self.comments[f'repos/{REPO}/issues/101/comments']), 1)

    def test_opted_in_singleton_window_still_reverts_without_blaming_original_pr(self):
        self.exercise(revert_window='true', count=1)
        self.assertTrue(self.armed)
        self.assertIn(' revert --no-edit ', self.git)
        self.assert_ops_only(1)
        payload = json.loads((self.root / 'revert-payload.json').read_text())
        self.assertNotIn('#101', payload['body'])
