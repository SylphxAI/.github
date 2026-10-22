"""Lifecycle-only alert tests; execute the same embedded code as the workflow."""
import copy
import json
import unittest
import tempfile
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def embedded():
    lines = (ROOT / '.github/workflows/red-main.yml').read_text().splitlines()
    start = lines.index('      LIFECYCLE_PY: |') + 1
    body = []
    for line in lines[start:]:
        if line.strip() and not line.startswith('        '):
            break
        body.append(line[8:])
    namespace = {'__name__': 'test'}
    with patch.object(sys, 'path', [str(ROOT / '.github/actions/ci-range'), *sys.path]):
        exec('\n'.join(body), namespace)
    return namespace


class API:
    def __init__(self):
        self.issues = []
        self.writes = []
        self.reads = []
        self.runs = {}
        self.fail = None
        self.hook = None

    def __call__(self, path, method='GET', data=None):
        self.reads.append(path)
        if self.hook:
            self.hook(path, method)
        if self.fail and self.fail in path:
            raise RuntimeError('API unavailable')
        if path == 'graphql':
            after = data['variables']['after']
            start = int(after) if after else 0
            rows = self.issues[start:start + 100]
            return {'data': {'repository': {'issues': {
                'totalCount': len(self.issues),
                'pageInfo': {'endCursor': str(start + len(rows)),
                             'hasNextPage': start + len(rows) < len(self.issues)},
                'nodes': [{'databaseId': row['number'], 'number': row['number'],
                           'body': row['body'], 'state': row['state'].upper(),
                           'author': {'databaseId': row['user']['id']}} for row in rows]}}}}
        if method != 'GET':
            self.writes.append((path, method, data))
            if path.endswith('/comments'):
                return {'id': 1}
            if method == 'POST':
                row = {**data, 'number': len(self.issues) + 1, 'user': {'id': 42}, 'state': 'open'}
                self.issues.append(row)
                return copy.deepcopy(row)
            number = int(path.rsplit('/', 1)[1])
            self.issues[number - 1].update(data)
            return copy.deepcopy(self.issues[number - 1])
        if '/actions/workflows/' in path:
            page = int(path.rsplit('page=', 1)[1])
            runs = list(self.runs.values())
            return {'total_count': len(runs),
                    'workflow_runs': copy.deepcopy(runs[(page - 1) * 100:page * 100])}
        if '/actions/runs/' in path:
            run_id = int(path.split('/actions/runs/')[1].split('/')[0])
            run = self.runs[run_id]
            if '/jobs?' in path:
                return {'total_count': 1, 'jobs': [{'id': 1, 'name': 'verified',
                                                   'conclusion': run['conclusion']}]}
            return copy.deepcopy(run)
        if '/issues?' in path:
            page = int(path.rsplit('page=', 1)[1])
            return copy.deepcopy(self.issues[(page - 1) * 100:page * 100])
        if '/issues/' in path:
            return copy.deepcopy(self.issues[int(path.rsplit('/', 1)[1]) - 1])
        raise AssertionError(path)


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.code = embedded()
        self.api = API()
        self.trunk('main')

    def trunk(self, name):
        patcher = patch.dict(self.code['os'].environ, {'TRUNK': name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def event(self, run_id, result='failure', attempt=1, branch='main'):
        run = {'id': run_id, 'workflow_id': 1, 'check_suite_id': 1, 'run_attempt': attempt, 'head_sha': 'a' * 40,
               'status': 'completed', 'conclusion': result, 'path': '.github/workflows/verify.yml',
               'repository': {'id': 1, 'full_name': 'SylphxAI/keel'},
               'event': 'push', 'head_branch': branch,
               'head_repository': {'id': 1, 'full_name': 'SylphxAI/keel'}}
        self.api.runs[run_id] = run
        return self.code['Event']('SylphxAI/keel', 'verify.yml', 'verified',
                                  'a' * 40, run_id, attempt, result)

    def apply(self, event):
        self.code['handle'](self.api, event, 42)

    def test_old_green_after_new_failure_keeps_open(self):
        self.apply(self.event(20))
        self.apply(self.event(10, 'success'))
        self.assertEqual(self.api.issues[0]['state'], 'open')
        self.assertEqual(len(self.api.writes), 1)

    def test_pending_newer_failure_blocks_older_recovery(self):
        self.apply(self.event(5))
        green = self.event(10, 'success')
        self.event(20)  # Completed red run whose pending handler never ran.
        with self.assertLogs(level='WARNING'):
            self.apply(green)
        self.assertEqual(self.api.issues[0]['state'], 'open')
        self.assertEqual(len(self.api.writes), 1)

    def test_event_binding_rejects_fork_main_success_and_non_main_failure(self):
        cases = [({'event': 'pull_request', 'head_repository': {'full_name': 'fork/keel'}},
                  'success'), ({'head_branch': 'feature'}, 'failure')]
        for changes, result in cases:
            with self.subTest(changes=changes), tempfile.NamedTemporaryFile(mode='w+') as stream:
                event = self.event(10, result)
                run = {**self.api.runs[10], **changes}
                json.dump({'action': 'completed', 'repository': {'full_name': event.repo},
                           'workflow_run': run}, stream)
                stream.flush()
                env = {'TRUNK': 'main', 'REPO': event.repo, 'VERIFY_WORKFLOW': event.workflow,
                       'VERIFY_CHECK_NAME': event.check, 'HEAD_SHA': event.sha,
                       'RUN_ID': str(event.run), 'RUN_ATTEMPT': str(event.attempt),
                       'RESULT': result, 'GITHUB_EVENT_PATH': stream.name,
                       'GITHUB_EVENT_NAME': 'workflow_run', 'GITHUB_REPOSITORY': event.repo}
                with patch.dict(self.code['os'].environ, env):
                    with self.assertRaisesRegex(RuntimeError, 'completed workflow_run'):
                        self.code['main']()
        self.assertEqual(self.api.writes, [])

    def test_api_provenance_mismatch_before_each_write(self):
        for changes in ({'event': 'pull_request'}, {'head_branch': 'feature'},
                        {'head_repository': {'full_name': 'fork/keel'}}):
            with self.subTest(changes=changes):
                self.api = API()
                self.apply(self.event(10))
                event = self.event(20, 'success')
                count = 0
                def hook(path, method):
                    nonlocal count
                    if path.endswith('/actions/runs/20'):
                        count += 1
                        if count == 2:
                            self.api.runs[20].update(changes)
                self.api.hook = hook
                with self.assertLogs(level='WARNING'):
                    self.apply(event)
                self.assertEqual(len(self.api.writes), 1)
                self.assertEqual(self.api.issues[0]['state'], 'open')

    def test_master_trunk_failure_and_recovery_are_classified(self):
        self.trunk('master')
        self.apply(self.event(10, branch='master'))
        self.assertEqual(len(self.api.issues), 1)
        self.apply(self.event(20, 'success', branch='master'))
        self.assertEqual(self.api.issues[0]['state'], 'closed')
        listing = [path for path in self.api.reads if '/actions/workflows/' in path]
        self.assertTrue(listing and all('branch=master&' in path for path in listing), listing)

    def test_a_run_on_main_is_not_the_trunk_of_a_master_repository(self):
        self.trunk('master')
        with self.assertLogs(level='WARNING'):
            self.apply(self.event(10, branch='main'))
        self.assertEqual(self.api.writes, [])

    def test_fork_main_success_and_non_main_failure_api_rejected(self):
        for changes, result in [({'event': 'pull_request',
                                 'head_repository': {'full_name': 'fork/keel'}}, 'success'),
                                ({'head_branch': 'feature'}, 'failure')]:
            with self.subTest(changes=changes):
                self.api = API()
                event = self.event(10, result)
                self.api.runs[10].update(changes)
                with self.assertLogs(level='WARNING'):
                    self.apply(event)
                self.assertEqual(self.api.writes, [])

    def test_recovery_listing_failure_leaves_open(self):
        self.apply(self.event(10))
        self.api.fail = '/actions/workflows/'
        with self.assertLogs(level='WARNING'):
            self.apply(self.event(20, 'success'))
        self.assertEqual(self.api.issues[0]['state'], 'open')
        self.assertEqual(len(self.api.writes), 1)

    def test_new_red_between_comment_and_close_leaves_open(self):
        self.apply(self.event(10))
        green = self.event(20, 'success')
        def hook(path, method):
            if path.endswith('/comments') and method == 'POST':
                self.event(30)
        self.api.hook = hook
        with self.assertLogs(level='WARNING'):
            self.apply(green)
        self.assertEqual(self.api.issues[0]['state'], 'open')
        self.assertEqual(len(self.api.writes), 2)

    def test_latest_run_listing_is_paginated_and_ignores_ineligible_runs(self):
        self.apply(self.event(10))
        green = self.event(20, 'success')
        for run_id in range(30, 135):
            self.event(run_id)
            self.api.runs[run_id]['head_branch'] = 'feature'
        self.apply(green)
        self.assertEqual(self.api.issues[0]['state'], 'closed')
        self.assertTrue(any('/actions/workflows/' in p and 'page=2' in p
                            for p in self.api.reads))

    def test_queue_preserves_up_to_100_pending_handlers(self):
        text = (ROOT / '.github/workflows/red-main.yml').read_text()
        self.assertIn('  queue: max\n  cancel-in-progress: false', text)

    def test_incomplete_counted_reads_never_comment_or_close(self):
        for source in ('workflow_runs', 'jobs', 'issues'):
            for fault in ('missing_count', 'invalid_count', 'truncated', 'duplicate'):
                with self.subTest(source=source, fault=fault):
                    self.api = API()
                    self.apply(self.event(5))
                    green = self.event(10, 'success')
                    self.event(20)
                    def reader(path, method='GET', data=None):
                        response = self.api(path, method, data)
                        if source == 'issues' and path == 'graphql':
                            envelope = response['data']['repository']['issues']
                            count_key, rows_key = 'totalCount', 'nodes'
                        elif source == 'workflow_runs' and '/actions/workflows/' in path:
                            envelope = response
                            count_key, rows_key = 'total_count', source
                        elif source == 'jobs' and '/jobs?' in path:
                            envelope = response
                            count_key, rows_key = 'total_count', source
                        else:
                            return response
                        if fault == 'missing_count':
                            envelope.pop(count_key)
                        elif fault == 'invalid_count':
                            envelope[count_key] = None
                        elif fault == 'truncated':
                            envelope[rows_key] = envelope[rows_key][:-1]
                        else:
                            rows = envelope[rows_key]
                            envelope[rows_key] = rows + [copy.deepcopy(rows[0])]
                            envelope[count_key] += 1
                        return response
                    with self.assertLogs(level='WARNING'):
                        self.code['handle'](reader, green, 42)
                    self.assertEqual(self.api.issues[0]['state'], 'open')
                    # No recovery comment and no close, only the initial alert.
                    self.assertEqual(len(self.api.writes), 1)

    def test_contradictory_issue_continuation_never_recovers(self):
        for fault in ('complete_with_continuation', 'empty_with_continuation',
                      'incomplete_without_continuation'):
            with self.subTest(fault=fault):
                self.api = API()
                self.apply(self.event(5))
                green = self.event(10, 'success')  # Valid latest recovery proof.
                def reader(path, method='GET', data=None):
                    response = self.api(path, method, data)
                    if path == 'graphql':
                        connection = response['data']['repository']['issues']
                        if fault == 'complete_with_continuation':
                            connection['pageInfo']['hasNextPage'] = True
                        elif fault == 'empty_with_continuation':
                            connection['nodes'] = []
                            connection['totalCount'] = 0
                            connection['pageInfo']['hasNextPage'] = True
                        else:
                            connection['totalCount'] = 2
                            connection['pageInfo']['hasNextPage'] = False
                    return response
                with self.assertLogs(level='WARNING'):
                    self.code['handle'](reader, green, 42)
                self.assertEqual(self.api.issues[0]['state'], 'open')
                self.assertEqual(len(self.api.writes), 1)  # No comment or close.

    def test_empty_issue_page_with_continuation_never_creates_duplicate(self):
        self.apply(self.event(5))
        failure = self.event(10)
        def reader(path, method='GET', data=None):
            response = self.api(path, method, data)
            if path == 'graphql':
                connection = response['data']['repository']['issues']
                connection['nodes'] = []
                connection['totalCount'] = 0
                connection['pageInfo']['hasNextPage'] = True
            return response
        with self.assertLogs(level='WARNING'):
            self.code['handle'](reader, failure, 42)
        self.assertEqual(len(self.api.issues), 1)
        self.assertEqual(len(self.api.writes), 1)
        self.assertEqual(self.api.issues[0]['state'], 'open')

    def test_invalid_newer_relevant_row_never_comments_or_closes(self):
        for field in ('status', 'conclusion'):
            for value in ('missing', None, '', ' '):
                with self.subTest(field=field, value=value):
                    self.api = API()
                    self.apply(self.event(5))
                    green = self.event(10, 'success')
                    self.event(20)
                    if value == 'missing':
                        self.api.runs[20].pop(field)
                    else:
                        self.api.runs[20][field] = value
                    with self.assertLogs(level='WARNING'):
                        self.apply(green)
                    self.assertEqual(self.api.issues[0]['state'], 'open')
                    self.assertEqual(len(self.api.writes), 1)

    def test_new_green_closes_and_links_run(self):
        self.apply(self.event(10))
        self.apply(self.event(20, 'success'))
        self.assertEqual(self.api.issues[0]['state'], 'closed')
        self.assertIn('/actions/runs/20/attempts/1', self.api.writes[1][2]['body'])

    def test_same_run_newer_attempt_recovers(self):
        self.apply(self.event(10))
        self.apply(self.event(10, 'success', 2))
        self.assertEqual(self.api.issues[0]['state'], 'closed')

    def test_fifo_out_of_order_failures_and_replay(self):
        newer = self.event(20)
        self.apply(newer)
        self.apply(self.event(10))
        self.apply(newer)
        self.assertEqual(len(self.api.writes), 1)
        self.apply(self.event(21, 'success'))
        self.apply(newer)
        self.assertEqual(self.api.issues[0]['state'], 'closed')

    def test_pagination_over_100_unrelated_issues(self):
        self.api.issues = [{'number': i + 1, 'body': 'unrelated', 'state': 'open',
                            'user': {'id': 42}} for i in range(105)]
        self.apply(self.event(10))
        self.apply(self.event(20, 'success'))
        self.assertEqual(self.api.issues[105]['state'], 'closed')
        self.assertTrue(all(i['state'] == 'open' for i in self.api.issues[:105]))
        self.assertGreaterEqual(self.api.reads.count('graphql'), 4)

    def test_marker_copied_by_non_app_is_not_owned(self):
        self.apply(self.event(10))
        self.api.issues[0]['user'] = {'id': 99}
        self.apply(self.event(20, 'success'))
        self.assertEqual(self.api.issues[0]['state'], 'open')

    def test_other_identity_is_untouched(self):
        self.apply(self.event(10))
        event = self.event(20, 'success')
        event.check = 'another-check'
        self.apply(event)
        self.assertEqual(self.api.issues[0]['state'], 'open')

    def test_api_failure_leaves_open_and_logs(self):
        self.apply(self.event(10))
        self.api.fail = '/actions/runs/20'
        with self.assertLogs(level='WARNING'):
            self.apply(self.event(20, 'success'))
        self.assertEqual(self.api.issues[0]['state'], 'open')
        self.assertEqual(len(self.api.writes), 1)

    def test_issue_search_failure_never_creates_duplicate(self):
        self.api.fail = 'graphql'
        with self.assertLogs(level='WARNING'):
            self.apply(self.event(10))
        self.assertEqual(self.api.writes, [])

    def test_run_rerun_at_mutation_time_prevents_close(self):
        self.apply(self.event(10))
        event = self.event(20, 'success')
        count = 0
        def hook(path, method):
            nonlocal count
            if path.endswith('/actions/runs/20'):
                count += 1
                if count == 2:
                    self.api.runs[20]['run_attempt'] = 2
        self.api.hook = hook
        with self.assertLogs(level='WARNING'):
            self.apply(event)
        self.assertEqual(self.api.issues[0]['state'], 'open')
        self.assertEqual(len(self.api.writes), 1)

    def test_cancelled_and_unknown_do_not_recover(self):
        self.apply(self.event(10))
        self.apply(self.event(20, 'cancelled'))
        self.apply(self.event(21, 'unknown'))
        self.apply(self.event(19, 'success'))
        self.assertEqual(self.api.issues[0]['state'], 'open')

    def test_read_failure_after_comment_still_does_not_close(self):
        self.apply(self.event(10))
        event = self.event(20, 'success')
        def hook(path, method):
            if path.endswith('/comments') and method == 'POST':
                self.api.fail = '/actions/runs/20'
        self.api.hook = hook
        with self.assertLogs(level='WARNING'):
            self.apply(event)
        self.assertEqual(self.api.issues[0]['state'], 'open')
        self.assertEqual(len(self.api.writes), 2)

    def test_new_failure_reopens_same_recovered_issue(self):
        self.apply(self.event(10))
        self.apply(self.event(20, 'success'))
        self.apply(self.event(30))
        self.assertEqual(len(self.api.issues), 1)
        self.assertEqual(self.api.issues[0]['state'], 'open')
        self.apply(self.event(25, 'success'))
        self.assertEqual(self.api.issues[0]['state'], 'open')

    def test_jobs_proof_failure_does_not_recover(self):
        self.apply(self.event(10))
        self.api.fail = '/jobs?'
        with self.assertLogs(level='WARNING'):
            self.apply(self.event(20, 'success'))
        self.assertEqual(self.api.issues[0]['state'], 'open')

    def test_attempt_mismatch_does_not_recover(self):
        self.apply(self.event(10))
        event = self.event(20, 'success')
        self.api.runs[20]['run_attempt'] = 2
        with self.assertLogs(level='WARNING'):
            self.apply(event)
        self.assertEqual(self.api.issues[0]['state'], 'open')

    def test_lifecycle_actions_reads_use_read_only_caller_token(self):
        text = (ROOT / '.github/workflows/red-main.yml').read_text()
        lifecycle = text[text.index('  lifecycle:'):text.index('  red-main:')]
        self.assertIn('ACTIONS_TOKEN: ${{ github.token }}', lifecycle)
        self.assertIn("env['GH_TOKEN'] = os.environ['ACTIONS_TOKEN']", lifecycle)
        self.assertNotIn('permission-actions:', lifecycle)
        self.assertIn('permission-issues: write', lifecycle)

    def test_no_rerun_dispatch_quarantine_or_revert(self):
        self.apply(self.event(10))
        self.apply(self.event(20, 'success'))
        self.assertTrue(all('/issues' in p for p, _, _ in self.api.writes))
        text = (ROOT / '.github/workflows/red-main.yml').read_text()
        self.assertIn("if: inputs.mode == 'repair'", text)
        lifecycle = text[text.index('  lifecycle:'):text.index('  red-main:')]
        self.assertNotIn('workflow_dispatch', lifecycle)
        self.assertNotIn('rerun', lifecycle)


if __name__ == '__main__':
    unittest.main()
