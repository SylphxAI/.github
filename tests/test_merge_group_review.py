"""Exact-head review admission through ci-ok's existing membership resolver."""
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'review_contribution_policy', ROOT / '.github/actions/contribution-policy/contribution_policy.py')
policy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(policy)
RULES = json.loads(policy.POLICY.read_text())
BASE, HEAD, GROUP, OLD = (char * 40 for char in 'abcd')
IDENTITY = {'name': 'Kyle Tse', 'email': 'hi@kylet.se'}


def commit(sha):
    return {'sha': sha, 'commit': {'author': IDENTITY.copy(), 'committer': IDENTITY.copy(),
                                 'message': 'Improve validation'}}


def pull(number=1, head=HEAD, title='[review] Improve validation'):
    return {'number': number, 'title': title, 'body': '', 'state': 'open', 'autoMergeRequest': None,
            'head': {'sha': head}, 'base': {'sha': BASE, 'ref': 'main'},
            'user': {'login': 'shtse8', 'id': 8020099}}


def run(sha=HEAD, conclusion='success', status='completed', app=1299545, name='Sylphx PR Review', id=1):
    return {'id': id, 'name': name, 'head_sha': sha, 'status': status,
            'conclusion': conclusion, 'app': {'id': app}}


def queue_page(nodes, total, more=False, cursor=None):
    return {'data': {'repository': {'mergeQueue': {'entries': {'totalCount': total, 'nodes': nodes,
            'pageInfo': {'hasNextPage': more, 'endCursor': cursor}}}}}}


class ReviewVerdictTest(unittest.TestCase):
    def setUp(self):
        self.api = mock.Mock(repository='example/repo', token='read-token')
        self.api.get.return_value = pull()
        self.api.check_runs.return_value = [run()]

    def verdict(self, pr=None, commits=None):
        return policy.review_findings(self.api, pr or pull(), commits or [commit(HEAD)])

    def assert_state(self, state):
        findings = self.verdict()
        self.assertEqual(len(findings), 1)
        self.assertIn(state, findings[0]['rule'])
        self.assertIn('PR #1', findings[0]['location'])
        self.assertIn(HEAD, findings[0]['location'])

    def test_exact_head_success_admits(self):
        self.assertEqual(self.verdict(), [])

    def test_all_non_success_states_refuse_and_name_pr_head_state(self):
        for state in ('neutral', 'failure', 'cancelled', 'timed_out', 'skipped', 'action_required', 'stale', None):
            with self.subTest(state=state):
                self.api.check_runs.return_value = [run(conclusion=state)]
                self.assert_state(state or 'missing conclusion')
        for state in ('queued', 'in_progress', 'waiting', None):
            with self.subTest(status=state):
                self.api.check_runs.return_value = [run(status=state)]
                self.assert_state('pending')

    def test_missing_refuses(self):
        self.api.check_runs.return_value = []
        self.assert_state('missing')

    def test_success_only_on_older_head_is_diagnostic_not_authority(self):
        self.api.check_runs.side_effect = lambda sha: [run(OLD)] if sha == OLD else []
        findings = self.verdict(commits=[commit(OLD), commit(HEAD)])
        self.assertIn('success on an older head ' + OLD, findings[0]['rule'])
        self.assertIn(HEAD, findings[0]['location'])

    def test_wrong_head_response_cannot_admit(self):
        self.api.check_runs.return_value = [run(OLD)]
        self.assert_state('success on an older head ' + OLD)

    def test_wrong_app_or_name_cannot_admit(self):
        for wrong in (run(app=42), run(name='Other review')):
            with self.subTest(wrong=wrong):
                self.api.check_runs.return_value = [wrong]
                self.assert_state('missing')

    def test_latest_canonical_run_wins_even_after_success(self):
        for latest in (run(conclusion='failure', id=2), run(status='in_progress', conclusion=None, id=2)):
            self.api.check_runs.return_value = [latest, run()]
            self.assertTrue(self.verdict())
        self.api.check_runs.return_value = [run(conclusion='failure'), run(id=2)]
        self.assertEqual(self.verdict(), [])

    def test_api_error_refuses_with_pr_and_head(self):
        self.api.check_runs.side_effect = policy.PolicyError('GitHub metadata read failed (HTTP 403)')
        self.assert_state('API read error: GitHub metadata read failed (HTTP 403)')

    def test_head_title_or_branch_movement_refuses(self):
        for path, value in ((('head', 'sha'), OLD), (('title',), 'No review'), (('base', 'ref'), 'other')):
            after = pull()
            target = after
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            self.api.get.return_value = after
            self.assert_state('changed during review validation')

    def test_non_review_needs_no_check_inventory(self):
        for title in ('Improve validation', 'prefix [review]', '[Review] Capitalized'):
            self.assertEqual(self.verdict(pull(title=title)), [])
        self.api.check_runs.assert_not_called()


class ReviewPaginationTest(unittest.TestCase):
    def setUp(self):
        self.api = policy.GitHub('example/repo', 'read-token')

    def test_review_success_beyond_page_boundary_is_seen(self):
        first = [run(id=i, app=42) for i in range(1, 101)]
        with mock.patch.object(self.api, 'get', side_effect=[{'total_count': 101, 'check_runs': first},
                {'total_count': 101, 'check_runs': [run(id=101)]}]) as get:
            rows = self.api.check_runs(HEAD)
        self.assertEqual(len(rows), 101)
        self.assertEqual(rows[-1]['app']['id'], 1299545)
        self.assertTrue(get.call_args_list[1].args[0].endswith('page=2'))

    def test_short_missing_duplicate_or_moving_check_pages_fail_closed(self):
        first = [run(id=i) for i in range(1, 101)]
        for pages in ([{'total_count': 2, 'check_runs': [run()]}],
                      [{'total_count': 101, 'check_runs': first}, {'total_count': 101, 'check_runs': []}],
                      [{'total_count': 101, 'check_runs': first}, {'total_count': 101, 'check_runs': [run()]}],
                      [{'total_count': 101, 'check_runs': first}, {'total_count': 100, 'check_runs': [run(id=101)]}],
                      [{'total_count': 1, 'check_runs': [None]}]):
            with self.subTest(pages=pages), mock.patch.object(self.api, 'get', side_effect=pages):
                with self.assertRaises(policy.PolicyError):
                    self.api.check_runs(HEAD)

    def test_second_review_page_api_error_refuses_instead_of_using_first_page_success(self):
        with mock.patch.object(self.api, 'get', side_effect=[{'total_count': 101,
                'check_runs': [run(id=i) for i in range(1, 101)]}, policy.PolicyError('HTTP 503')]):
            findings = policy.review_findings(self.api, pull(), [commit(HEAD)])
        self.assertIn('API read error: HTTP 503', findings[0]['rule'])

    def test_queue_page_boundary_and_gaps(self):
        entries = [{'headCommit': {'oid': GROUP}, 'pullRequest': {'number': i, 'headRefOid': HEAD}}
                   for i in range(1, 102)]
        with mock.patch.object(self.api, 'read', side_effect=[queue_page(entries[:100], 101, True, 'next'),
                                                            queue_page(entries[100:], 101)]):
            self.assertEqual(len(self.api.queue_entries('main')), 101)
        for pages in ([queue_page(entries[:100], 101)],
                      [queue_page(entries[:100], 101, True, 'next'), queue_page([], 101)],
                      [queue_page(entries[:100], 101, True, 'next'), queue_page(entries[100:], 102)],
                      [queue_page(entries[:1], 101, True, 'next'), queue_page(entries[1:2], 101, True, 'next')]):
            with self.subTest(pages=pages), mock.patch.object(self.api, 'read', side_effect=pages):
                with self.assertRaises(policy.PolicyError):
                    self.api.queue_entries('main')


class MergeGroupAdmissionTest(unittest.TestCase):
    def setUp(self):
        self.api = mock.Mock(repository='example/repo', token='read-token')
        self.prs = {1: pull()}
        self.group = {'merge_group': {'base_sha': BASE, 'head_sha': GROUP, 'base_ref': 'refs/heads/main'}}
        self.api.get.side_effect = lambda path: copy.deepcopy(self.prs[int(path.split('/')[1])])
        self.api.pages.return_value = []
        self.api.queue_entries.return_value = [self.entry(1, GROUP, HEAD)]
        self.api.check_runs.return_value = [run()]

    @staticmethod
    def entry(number, synthetic, head):
        return {'headCommit': {'oid': synthetic}, 'pullRequest': {'number': number, 'headRefOid': head}}

    def check(self, **options):
        with mock.patch.object(policy, 'range_commits', side_effect=lambda base, head, *_: [commit(head)]):
            return policy.check_event(self.api, 'merge_group', self.group, GROUP, RULES,
                                      require_merge_group_review=True, **options)

    def test_already_queued_pr_with_no_auto_merge_request_and_no_review_refuses(self):
        self.assertIsNone(self.prs[1]['autoMergeRequest'])
        self.api.check_runs.return_value = []
        self.assertIn('missing', self.check()[0]['rule'])
        self.api.queue_entries.assert_called_once_with('main')

    def test_exact_head_success_and_non_review_control(self):
        self.assertEqual(self.check(), [])
        self.prs[1]['title'] = 'Improve validation'
        self.api.check_runs.reset_mock()
        self.assertEqual(self.check(), [])
        self.api.check_runs.assert_not_called()

    def test_separate_metadata_cannot_disable_review_gate(self):
        self.api.check_runs.return_value = [run(conclusion='neutral')]
        self.assertIn('neutral', self.check(check_pull_text=False)[0]['rule'])

    def test_all_offenders_in_multi_pr_group_are_reported(self):
        second_group = 'e' * 40
        self.prs[2] = pull(2, OLD)
        self.api.queue_entries.return_value += [self.entry(2, second_group, OLD)]
        self.api.check_runs.side_effect = lambda sha: [run(sha, conclusion='failure' if sha == OLD else 'neutral')]
        with mock.patch.object(policy, 'range_commits', side_effect=lambda base, head, *_:
                               [commit(GROUP), commit(second_group)] if head == GROUP else [commit(head)]):
            findings = policy.check_event(self.api, 'merge_group', self.group, GROUP, RULES,
                                          require_merge_group_review=True)
        self.assertEqual(len(findings), 2)
        self.assertIn('PR #1 head ' + HEAD, findings[0]['location'])
        self.assertIn('neutral', findings[0]['rule'])
        self.assertIn('PR #2 head ' + OLD, findings[1]['location'])
        self.assertIn('failure', findings[1]['rule'])

    def test_group_with_one_success_and_one_failure_refuses(self):
        second_group = 'e' * 40
        self.prs[2] = pull(2, OLD)
        self.api.queue_entries.return_value += [self.entry(2, second_group, OLD)]
        self.api.check_runs.side_effect = lambda sha: [run(sha, conclusion='failure' if sha == OLD else 'success')]
        with mock.patch.object(policy, 'range_commits', side_effect=lambda base, head, *_:
                               [commit(GROUP), commit(second_group)] if head == GROUP else [commit(head)]):
            findings = policy.check_event(self.api, 'merge_group', self.group, GROUP, RULES,
                                          require_merge_group_review=True)
        self.assertEqual(len(findings), 1)
        self.assertIn('PR #2 head ' + OLD, findings[0]['location'])

    def test_pull_request_ci_does_not_wait_for_async_review(self):
        with mock.patch.object(policy, 'range_commits', return_value=[commit(HEAD)]):
            findings = policy.check_event(self.api, 'pull_request', {'number': 1, 'pull_request': pull()}, HEAD,
                                          RULES, require_merge_group_review=True)
        self.assertEqual(findings, [])
        self.api.check_runs.assert_not_called()

    def test_queue_head_movement_or_incomplete_membership_refuses(self):
        self.prs[1]['head']['sha'] = OLD
        with self.assertRaisesRegex(policy.PolicyError, 'head changed'):
            self.check()
        self.api.queue_entries.return_value = []
        with self.assertRaisesRegex(policy.PolicyError, 'membership is incomplete'):
            self.check()

    def test_ci_ok_manifest_calls_gate_and_cli_exits_nonzero_with_named_offender(self):
        action = yaml.safe_load((ROOT / '.github/actions/ci-ok/action.yml').read_text())
        step = next(step for step in action['runs']['steps'] if step['name'] == 'Check contribution metadata')
        command = shlex.split(step['run'])
        args = [arg.replace('$CONTRIBUTION_POLICY_SCOPE', 'commits') for arg in command[2:]]
        self.api.check_runs.return_value = []
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            event = Path(directory) / 'event.json'
            event.write_text(json.dumps(self.group))
            env = {'GITHUB_EVENT_NAME': 'merge_group', 'GITHUB_EVENT_PATH': str(event),
                   'GITHUB_REPOSITORY': 'example/repo', 'CONTRIBUTION_POLICY_TOKEN': 'read-token'}
            with mock.patch.dict(os.environ, env), mock.patch.object(policy, 'GitHub', return_value=self.api), \
                    mock.patch.object(policy, 'range_commits', side_effect=lambda base, head, *_: [commit(head)]), \
                    contextlib.redirect_stdout(output):
                self.assertEqual(policy.main(args), 1)
        self.assertIn('::error::Contribution policy: exact-head-review: missing at PR #1 head ' + HEAD, output.getvalue())


if __name__ == '__main__':
    unittest.main()
