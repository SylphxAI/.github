"""Real ci-range entrypoint and Git checkout; only the forge transport is fake."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / '.github/actions/ci-range'

FORGE = r'''#!/usr/bin/env python3
import json, os, re, sys
from pathlib import Path
path = sys.argv[2]
scenario = os.environ['SCENARIO']
sha = os.environ['GITHUB_SHA']
log = Path(os.environ['FORGE_LOG'])
reads = log.read_text().splitlines() if log.exists() else []
log.write_text('\n'.join(reads + [path]) + '\n')
count = reads.count(path)
if scenario == 'unreadable':
    sys.exit(1)
identity = dict(id=7, full_name='o/r')
run = dict(id=40, workflow_id=6, check_suite_id=8, run_attempt=1,
           repository=identity.copy(), head_repository=identity.copy(),
           event='merge_group', head_branch='gh-readonly-queue/main/pr-3-abc',
           path='.github/workflows/gate.yml', head_sha=sha,
           status='completed', conclusion='success')
if scenario in ('workflow_id', 'check_suite_id', 'run_attempt'):
    run[scenario] = True
if scenario == 'wrong_path': run['path'] = '.github/workflows/other.yml'
if scenario == 'wrong_workflow': run['workflow_id'] = 99
if scenario == 'wrong_source': run['head_sha'] = 'a'*40
if scenario == 'wrong_event': run['event'] = 'push'
if scenario == 'wrong_target': run['head_branch'] = 'gh-readonly-queue/other/pr-3-abc'
if scenario == 'empty_target': run['head_branch'] = 'gh-readonly-queue/main/'
if scenario == 'wrong_repo': run['repository']['id'] = 44
if scenario == 'wrong_head_repo': run['head_repository']['full_name'] = 'attacker/r'
if scenario in ('pending', 'failure', 'cancelled'):
    run['status'] = 'in_progress' if scenario == 'pending' else 'completed'
    run['conclusion'] = None if scenario == 'pending' else scenario
jobs = []
for i, name in enumerate(('gate', 'database', 'database (other)'), 1):
    jobs.append(dict(id=100+i, run_id=40, run_attempt=1, head_sha=sha, name=name,
                     status='completed', conclusion='success',
                     check_run_url=f'https://api.github.com/repos/o/r/check-runs/{200+i}'))
if scenario == 'job_run': jobs[1]['run_id'] = 41
if scenario == 'job_attempt': jobs[1]['run_attempt'] = 2
if scenario == 'job_source': jobs[1]['head_sha'] = 'b'*40
if scenario == 'job_url': jobs[1]['check_run_url'] += '?fake=1'
if scenario in ('job_failure', 'job_cancelled', 'job_skipped'):
    jobs[1]['conclusion'] = scenario.split('_')[1]
if scenario == 'job_pending': jobs[1]['status'] = 'in_progress'
if scenario == 'aggregate_failure': jobs[0]['conclusion'] = 'failure'
if scenario == 'aggregate_pending': jobs[0]['status'] = 'queued'
if scenario == 'missing_job': jobs.pop(1)
if scenario == 'missing_aggregate': jobs.pop(0)
if scenario == 'duplicate_job': jobs.append(dict(jobs[1], id=199))
if scenario == 'duplicate_aggregate': jobs.append(dict(jobs[0], id=198))
if scenario == 'jobs_drift' and '/jobs?' in path and count: jobs[1]['started_at'] = 'changed'
if scenario == 'run_drift' and path.endswith('/runs/40') and count: run['run_attempt'] = 2
meta = dict(id=6, path='.github/workflows/gate.yml', state='active')
if scenario == 'meta_path': meta['path'] = '.github/workflows/other.yml'
if scenario == 'meta_inactive': meta['state'] = 'disabled_manually'
if scenario == 'meta_id': meta['id'] = False
if scenario == 'meta_drift' and path.endswith('/workflows/gate.yml') and count: meta['id'] = 66
if '/check-runs/' in path:
    check_id = int(path.rsplit('/',1)[1]); job = next(j for j in jobs if j['id'] == check_id-100)
    body = dict(id=check_id, app=dict(id=15368, slug='github-actions'), head_sha=sha,
                name=job['name'], check_suite=dict(id=8),
                details_url=f"https://github.com/o/r/actions/runs/40/job/{job['id']}",
                status=job['status'], conclusion=job['conclusion'])
    if scenario == 'check_app': body['app']['id'] = 3
    if scenario == 'check_slug': body['app']['slug'] = 'not-actions'
    if scenario == 'check_suite': body['check_suite']['id'] = 9
    if scenario == 'check_source': body['head_sha'] = 'b'*40
    if scenario == 'check_name': body['name'] = 'other'
    if scenario == 'check_url': body['details_url'] += '/other'
    if scenario == 'check_conclusion': body['conclusion'] = 'failure'
    if scenario == 'check_status': body['status'] = 'queued'
    if scenario == 'check_id': body['id'] = 99
    if scenario == 'check_malformed': body = None
elif '/jobs?' in path:
    body = dict(total_count=len(jobs), jobs=jobs)
    if scenario == 'jobs_incomplete': body['total_count'] += 1
elif path.endswith('/workflows/gate.yml'):
    body = meta
elif path.endswith('/runs/40'):
    body = run
elif path.endswith('/runs/41'):
    body = dict(run, id=41, status='completed', conclusion='failure')
    if scenario == 'newer_pending': body.update(status='queued', conclusion=None)
    if scenario == 'newer_cancelled': body['conclusion'] = 'cancelled'
elif '/workflows/6/runs?' in path:
    rows = [run]
    if scenario == 'absent': rows = []
    if scenario.startswith('newer_') or scenario == 'list_drift' and count:
        newer = dict(run, id=41, status='completed', conclusion='failure')
        if scenario == 'newer_pending': newer.update(status='queued', conclusion=None)
        if scenario == 'newer_cancelled': newer['conclusion'] = 'cancelled'
        rows += [newer]
    body = dict(total_count=len(rows), workflow_runs=rows)
    if scenario == 'runs_incomplete': body['total_count'] += 1
    if scenario == 'runs_duplicate': body = dict(total_count=2, workflow_runs=[run,run])
    if scenario == 'runs_malformed': body = dict(total_count=True, workflow_runs=rows)
else:
    # Existing authenticated ancestor selection remains separate: no verified
    # push exists here, so ordinary selection safely runs every lane.
    body = dict(total_count=0, workflow_runs=[])
print(json.dumps(body))
'''


class ReuseEntrypoint(unittest.TestCase):
    def invoke(self, scenario='success', event='push', only='', changes=None, settings=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args):
                return subprocess.check_output(['git', '-C', directory, *args], text=True).strip()
            git('init', '-q', '-b', 'main')
            git('config', 'user.name', 'test')
            git('config', 'user.email', 'test@example.com')
            (root/'source').write_text('one')
            git('add', 'source'); git('commit', '-qm', 'source')
            sha = git('rev-parse', 'HEAD')
            payload = dict(repository=dict(id=7, full_name='o/r', default_branch='main'),
                           ref='refs/heads/main', after=sha, deleted=False)
            if event == 'pull_request': payload['pull_request'] = {'base': {'sha': sha}}
            if event == 'merge_group': payload['merge_group'] = {'base_sha': sha}
            payload.update(changes or {})
            (root/'event.json').write_text(json.dumps(payload))
            (root/'gh').write_text(FORGE); (root/'gh').chmod(0o755)
            env = dict(os.environ, GITHUB_EVENT_NAME=event, GITHUB_EVENT_PATH=str(root/'event.json'),
                       GITHUB_OUTPUT=str(root/'outputs'), GITHUB_STEP_SUMMARY=str(root/'summary'),
                       GITHUB_REPOSITORY='o/r', GITHUB_SHA=sha, GITHUB_REF='refs/heads/main',
                       GITHUB_WORKFLOW_REF='o/r/.github/workflows/verify.yml@refs/heads/main',
                       PROOF_MODE='range', REUSE_WORKFLOW='gate.yml', REUSE_AGGREGATE='gate',
                       REUSE_JOBS='{"db":["database"]}', ONLY=only, SCENARIO=scenario,
                       LANES='db: *\nlint: *', FORGE_LOG=str(root/'reads'), PATH=f'{root}:{os.environ["PATH"]}')
            env.update(settings or {})
            result = subprocess.run(['python3', str(ACTION/'ci_range.py')], cwd=root, env=env,
                                    text=True, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            outputs = dict(line.split('=',1) for line in (root/'outputs').read_text().splitlines())
            outputs = {key: json.loads(value) for key,value in outputs.items()
                       if key in ('run','execute','reused','reuse-proof')}
            self.stdout = result.stdout
            self.reads = (root/'reads').read_text() if (root/'reads').exists() else ''
            return outputs

    def refused(self, *args, **kwargs):
        out = self.invoke(*args, **kwargs)
        self.assertEqual(out['execute'], out['run'])
        self.assertEqual(out['reused'], dict.fromkeys(out['run'], False))
        self.assertEqual(out['reuse-proof'], {})

    def test_success_is_explicit_separate_from_range(self):
        out = self.invoke()
        self.assertEqual(out['run'], dict(db=True, lint=True))
        self.assertEqual(out['execute'], dict(db=False, lint=True))
        self.assertEqual(out['reused'], dict(db=True, lint=False))
        receipt = out['reuse-proof']
        self.assertEqual((receipt['run'],receipt['attempt'],receipt['workflow_id'],receipt['repository_id']), (40,1,6,7))
        self.assertEqual(receipt['jobs'], {'database': {'job':102,'check':202}, 'gate':{'job':101,'check':201}})
        self.assertEqual(receipt['lanes'], {'db':['database']})
        reads = self.reads.splitlines()
        self.assertEqual(reads.count('repos/o/r/actions/runs/40'), 2)
        self.assertEqual(reads.count('repos/o/r/actions/workflows/gate.yml'), 2)

    def test_unreadable_and_invalid_evidence_runs_real_tests(self):
        cases = '''unreadable absent workflow_id check_suite_id run_attempt wrong_path wrong_workflow wrong_source
        wrong_event wrong_target empty_target wrong_repo wrong_head_repo pending failure cancelled job_run job_attempt
        job_source job_url job_failure job_cancelled job_skipped job_pending aggregate_failure aggregate_pending
        missing_job missing_aggregate duplicate_job duplicate_aggregate jobs_drift run_drift meta_path meta_inactive
        meta_id meta_drift check_app check_slug check_suite check_source check_name check_url check_conclusion
        check_status check_id check_malformed jobs_incomplete runs_incomplete runs_duplicate runs_malformed
        newer_failure newer_pending newer_cancelled list_drift'''.split()
        for scenario in cases:
            with self.subTest(scenario=scenario): self.refused(scenario)

    def test_schedule_manual_pr_and_queue_never_reuse(self):
        for event in ('schedule','workflow_dispatch','pull_request','merge_group'):
            with self.subTest(event=event):
                self.refused(event=event)
                self.assertNotIn('/workflows/6/', self.reads)

    def test_any_explicit_lane_request_never_reuses(self):
        for only in ('db', 'unknown', 'db,lint'):
            with self.subTest(only=only): self.refused(only=only)

    def test_native_default_branch_exact_checkout_required(self):
        for changes in ({'after':'a'*40}, {'ref':'refs/heads/other'}, {'deleted':True}, {'deleted':None},
                        {'repository':{'id':7,'full_name':'other/r','default_branch':'main'}},
                        {'repository':{'id':True,'full_name':'o/r','default_branch':'main'}}):
            with self.subTest(changes=changes): self.refused(changes=changes)
        self.refused(settings={'GITHUB_REF':'refs/heads/other'})
        self.refused(settings={'GITHUB_SHA':'a'*40})

    def test_malformed_configuration_fails_closed(self):
        for mapping in ('{}','[]','{"missing":["database"]}', '{"db":[]}', '{"db":[1]}',
                        '{"db":["gate"]}', '{"db":["database","database"]}', 'invalid'):
            with self.subTest(mapping=mapping): self.refused(settings={'REUSE_JOBS':mapping})
        self.refused(settings={'REUSE_WORKFLOW':'../gate.yml'})
        self.refused(settings={'REUSE_AGGREGATE':''})

    def test_disabled_is_identical_to_range(self):
        self.refused(settings={'REUSE_WORKFLOW':'','REUSE_AGGREGATE':'','REUSE_JOBS':''})

    def test_exact_matrix_and_reusable_names_are_not_normalized(self):
        self.refused(settings={'REUSE_JOBS':'{"db":["other / database"]}'})
        out = self.invoke(settings={'REUSE_JOBS':'{"db":["database","database (other)"]}'})
        self.assertTrue(out['reused']['db'])
        self.assertEqual(len(out['reuse-proof']['jobs']), 3)

    def test_reread_mutations_are_reached_and_refused(self):
        for scenario, reason in (
            ('jobs_drift', 'queue jobs changed during proof'),
            ('run_drift', 'queue producer changed during proof'),
            ('list_drift', 'queue producer changed during proof'),
            ('meta_drift', 'queue producer changed during proof'),
        ):
            with self.subTest(scenario=scenario):
                self.refused(scenario)
                self.assertIn(reason, self.stdout)

    def test_latest_red_or_pending_run_not_older_green(self):
        for scenario in ('newer_failure', 'newer_pending', 'newer_cancelled'):
            with self.subTest(scenario=scenario):
                self.refused(scenario)
                self.assertIn('latest queue attempt is not completed successfully', self.stdout)
                self.assertIn('repos/o/r/actions/runs/41', self.reads.splitlines())
                self.assertNotIn('repos/o/r/actions/runs/40', self.reads.splitlines())

    def test_unselected_lane_is_never_reused(self):
        # With a PR base identical to HEAD, this path-dependent lane is false.
        out = self.invoke(event='pull_request', settings={'LANES': 'db: db/*\nlint: *'})
        self.assertEqual(out['run'], {'db': False, 'lint': True})
        self.assertEqual(out['execute'], out['run'])
        self.assertEqual(out['reused'], {'db': False, 'lint': False})
