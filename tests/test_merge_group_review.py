"""Exact-head review admission through ci-ok's existing membership resolver."""
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
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


def group_commit(sha=GROUP, head=HEAD, parent=BASE):
    item = commit(sha)
    item['raw_commit'] = f'tree {BASE}\nparent {parent}\nparent {head}\n\nMerge PR'.encode()
    return item


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


    def test_malformed_app_identity_is_named_read_error(self):
        self.api.check_runs.return_value = [{**run(), 'app': []}]
        self.assert_state('API read error: review Check Run App identity is incomplete')


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

    def test_queue_disappearing_after_a_partial_page_is_not_a_complete_inventory(self):
        entries = [{'headCommit': {'oid': GROUP}, 'pullRequest': {'number': i, 'headRefOid': HEAD}}
                   for i in range(1, 101)]
        absent = {'data': {'repository': {'mergeQueue': None}}}
        with mock.patch.object(self.api, 'read', side_effect=[queue_page(entries, 101, True, 'next'), absent]):
            with self.assertRaisesRegex(policy.PolicyError, 'pagination is incomplete or changed'):
                self.api.queue_entries('main')
        with mock.patch.object(self.api, 'read', return_value=absent):
            self.assertEqual(self.api.queue_entries('main'), [])

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
        with mock.patch.object(policy, 'range_commits', side_effect=lambda base, head, *_: [group_commit() if head == GROUP else commit(head)]):
            return policy.check_event(self.api, 'merge_group', self.group, GROUP, RULES,
                                      require_merge_group_review=True, **options)

    def test_already_queued_pr_with_no_auto_merge_request_and_no_review_refuses(self):
        self.assertIsNone(self.prs[1]['autoMergeRequest'])
        self.api.check_runs.return_value = []
        self.assertIn('missing', self.check()[0]['rule'])
        self.assertEqual(self.api.queue_entries.call_count, 2)
        self.api.queue_entries.assert_called_with('main')

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
                               [group_commit(), group_commit(second_group, OLD, GROUP)] if head == GROUP else [commit(head)]):
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
                               [group_commit(), group_commit(second_group, OLD, GROUP)] if head == GROUP else [commit(head)]):
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

    def test_duplicate_membership_cannot_hide_a_pagination_gap(self):
        self.api.queue_entries.return_value *= 2
        with self.assertRaisesRegex(policy.PolicyError, 'membership is duplicated'):
            self.check()

    def test_queue_head_movement_or_incomplete_membership_refuses(self):
        self.prs[1]['head']['sha'] = OLD
        with self.assertRaisesRegex(policy.PolicyError, 'head changed'):
            self.check()
        self.api.queue_entries.return_value = []
        with self.assertRaisesRegex(policy.PolicyError, 'membership is incomplete'):
            self.check()

    def test_head_member_removal_member_addition_and_title_churn_refuse(self):
        before = [self.entry(1, GROUP, HEAD)]
        second_group = 'e' * 40
        cases = [
            [self.entry(1, GROUP, OLD)],
            [],
            [self.entry(1, GROUP, HEAD), self.entry(2, second_group, OLD)],
        ]
        for after in cases:
            with self.subTest(after=after):
                self.api.queue_entries.side_effect = [before, after]
                with mock.patch.object(policy, 'range_commits', side_effect=lambda base, head, *_:
                                       [group_commit(), group_commit(second_group, OLD, GROUP)] if head == GROUP else [commit(HEAD), commit(second_group)]):
                    findings = policy.check_event(self.api, 'merge_group', self.group, GROUP, RULES,
                                                  require_merge_group_review=True)
                self.assertTrue(any('membership/head changed during evaluation' in f['rule'] for f in findings))
        self.api.queue_entries.side_effect = None
        reads = 0
        def changing_title(path):
            nonlocal reads
            reads += 1
            return pull(title='Different title') if reads >= 4 else pull()
        self.api.get.side_effect = changing_title
        self.assertIn('membership/head changed during evaluation', self.check()[0]['rule'])

    def test_flag_absent_preserves_mismatched_parent_behavior(self):
        synthetic = commit(GROUP)
        synthetic['raw_commit'] = f'tree {BASE}\nparent {BASE}\nparent {OLD}\n\nMerge PR'.encode()
        for title in ('Improve validation', '[review] Improve validation'):
            self.prs[1]['title'] = title
            with self.subTest(title=title), mock.patch.object(policy, 'range_commits', side_effect=lambda base, head, *_:
                    [synthetic] if head == GROUP else [commit(head)]):
                self.assertEqual(policy.check_event(self.api, 'merge_group', self.group, GROUP, RULES), [])
        self.api.check_runs.assert_not_called()

    def test_incomplete_or_unsupported_immutable_position_fails_closed(self):
        for raw, reason in ((None, 'tree/parents are incomplete or unsupported'),
                            (f'tree {BASE}\n\nMerge PR'.encode(), 'tree/parents are incomplete or unsupported'),
                            (f'tree {BASE}\nparent invalid\n\nMerge PR'.encode(), 'tree/parents are incomplete or unsupported'),
                            (f'tree {BASE}\nparent {OLD}\nparent {HEAD}\n\nMerge PR'.encode(), 'position is incomplete')):
            synthetic = {**commit(GROUP), 'raw_commit': raw}
            with self.subTest(raw=raw), mock.patch.object(policy, 'range_commits', side_effect=lambda base, head, *_:
                    [synthetic] if head == GROUP else [commit(head)]):
                with self.assertRaisesRegex(policy.PolicyError, reason):
                    policy.check_event(self.api, 'merge_group', self.group, GROUP, RULES,
                                       require_merge_group_review=True)
        self.prs[1]['title'] = 'Improve validation'
        with mock.patch.object(policy, 'range_commits', return_value=[commit(GROUP)]), \
                mock.patch.object(policy, 'squash_merge_tree') as tree:
            self.assertEqual(policy.check_event(self.api, 'merge_group', self.group, GROUP, RULES,
                                               require_merge_group_review=True), [])
            tree.assert_not_called()

    def test_first_pr_head_mutated_during_second_pr_review_read_refuses(self):
        second_group = 'e' * 40
        self.prs[2] = pull(2, OLD)
        self.api.queue_entries.return_value += [self.entry(2, second_group, OLD)]
        def review(sha):
            if sha == OLD:
                self.prs[1]['head']['sha'] = 'f' * 40
            return [run(sha)]
        self.api.check_runs.side_effect = review
        with mock.patch.object(policy, 'range_commits', side_effect=lambda base, head, *_:
                [group_commit(), group_commit(second_group, OLD, GROUP)] if head == GROUP else [commit(head)]):
            findings = policy.check_event(self.api, 'merge_group', self.group, GROUP, RULES,
                                          require_merge_group_review=True)
        self.assertEqual(findings, [{'rule': 'exact-head-review: membership/head changed during evaluation',
                                    'location': 'PR #1 head ' + HEAD, 'line': 0}])

    def test_merge_commit_parent_pins_original_head_even_before_first_queue_read(self):
        synthetic = commit(GROUP)
        synthetic['raw_commit'] = f'tree {BASE}\nparent {BASE}\nparent {OLD}\n\nMerge PR'.encode()
        with mock.patch.object(policy, 'range_commits', return_value=[synthetic]):
            with self.assertRaisesRegex(policy.PolicyError, f'PR #1 head {HEAD}.*parent pins {OLD}'):
                policy.check_event(self.api, 'merge_group', self.group, GROUP, RULES,
                                   require_merge_group_review=True)
        self.api.check_runs.assert_not_called()

    def test_unchanged_merge_commit_parent_and_membership_pass(self):
        synthetic = commit(GROUP)
        synthetic['raw_commit'] = f'tree {BASE}\nparent {BASE}\nparent {HEAD}\n\nMerge PR'.encode()
        with mock.patch.object(policy, 'range_commits', side_effect=lambda base, head, *_:
                               [synthetic] if head == GROUP else [commit(HEAD)]):
            self.assertEqual(policy.check_event(self.api, 'merge_group', self.group, GROUP, RULES,
                                               require_merge_group_review=True), [])
        self.assertEqual(self.api.queue_entries.call_count, 2)

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
                    mock.patch.object(policy, 'range_commits', side_effect=lambda base, head, *_: [group_commit() if head == GROUP else commit(head)]), \
                    contextlib.redirect_stdout(output):
                self.assertEqual(policy.main(args), 1)
        self.assertIn('::error::Contribution policy: exact-head-review: missing at PR #1 head ' + HEAD, output.getvalue())


class SquashTreeAdapterTest(unittest.TestCase):
    def setUp(self):
        self.source = mock.Mock(ScanError=RuntimeError)
        self.source.fetch.return_value = {'GIT_TERMINAL_PROMPT': '0', 'REVIEW_TEST_AUTH': 'scoped'}
        patch = mock.patch.object(policy, 'immutable_git_source', return_value=self.source)
        patch.start()
        self.addCleanup(patch.stop)

    def test_fetch_and_merge_keep_the_same_auth_environment(self):
        with mock.patch.object(policy.subprocess, 'run', return_value=mock.Mock(returncode=0,
                stdout=(OLD + '\n').encode())) as execute:
            self.assertEqual(policy.squash_merge_tree('example/repo', 'read-token', BASE, HEAD), OLD)
        self.source.fetch.assert_called_once()
        self.assertEqual(self.source.fetch.call_args.args[1:],
                         ('https://github.com/example/repo.git', BASE, HEAD, 'read-token'))
        self.assertEqual(execute.call_args.args[0][-5:],
                         ['merge-tree', '--write-tree', '--no-messages', BASE, HEAD])
        self.assertEqual(execute.call_args.kwargs['env'], self.source.fetch.return_value)
        self.assertEqual(execute.call_args.kwargs['timeout'], 60)

    def test_acquisition_errors_and_merge_diagnostics_are_not_printed(self):
        self.source.fetch.side_effect = RuntimeError('private diagnostic')
        with self.assertRaisesRegex(policy.PolicyError, '^squash tree proof object acquisition failed$'):
            policy.squash_merge_tree('example/repo', 'read-token', BASE, HEAD)
        self.source.fetch.side_effect = None
        for status, stdout in ((1, b'private conflict'), (2, b'private diagnostic'), (0, b'incomplete')):
            reason = 'conflicts' if status == 1 else 'failed'
            with self.subTest(status=status), mock.patch.object(policy.subprocess, 'run',
                    return_value=mock.Mock(returncode=status, stdout=stdout)), \
                    self.assertRaisesRegex(policy.PolicyError, '^squash tree proof ' + reason + '$'):
                policy.squash_merge_tree('example/repo', 'read-token', BASE, HEAD)
        for error, reason in ((subprocess.TimeoutExpired('git', 60), 'timed out after 60s'),
                              (OSError('private diagnostic'), 'could not execute Git')):
            with self.subTest(error=error), mock.patch.object(policy.subprocess, 'run', side_effect=error), \
                    self.assertRaisesRegex(policy.PolicyError, '^squash tree proof ' + reason + '$'):
                policy.squash_merge_tree('example/repo', 'read-token', BASE, HEAD)


class SquashTreeBindingTest(unittest.TestCase):
    """Run real object acquisition and merge-tree, not a mocked tree oracle."""
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.repo = self.root / 'example/repo.git'
        self.repo.mkdir(parents=True)
        self.env = {**os.environ, 'GIT_AUTHOR_NAME': 'Kyle Tse', 'GIT_AUTHOR_EMAIL': 'hi@kylet.se',
                    'GIT_COMMITTER_NAME': 'Kyle Tse', 'GIT_COMMITTER_EMAIL': 'hi@kylet.se'}
        self.git('init', '-q')
        self.git('config', 'commit.gpgsign', 'false')
        (self.repo / 'shared.txt').write_text('before\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'Initialize validation')
        self.base = self.git('rev-parse', 'HEAD')
        self.api = mock.Mock(repository='example/repo', token='')
        self.prs = {}
        self.entries = []
        self.api.get.side_effect = lambda path: copy.deepcopy(self.prs[int(path.split('/')[1])])
        self.api.queue_entries.side_effect = lambda _: copy.deepcopy(self.entries)
        self.api.pages.return_value = []
        self.api.check_runs.side_effect = lambda sha: [run(sha)]

    def git(self, *args, input=None):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], input=input,
                                       env=self.env, stderr=subprocess.PIPE).decode().strip()

    def head(self, name, path, content):
        self.git('checkout', '-qB', name, self.base)
        (self.repo / path).write_text(content)
        self.git('add', '.')
        self.git('commit', '-qm', 'Improve validation')
        return self.git('rev-parse', 'HEAD')

    def synthetic(self, parent, head):
        tree = self.git('merge-tree', '--write-tree', parent, head).splitlines()[0]
        return self.git('commit-tree', tree, '-p', parent, input=b'Improve validation\n')

    def member(self, number, synthetic, head):
        self.prs[number] = pull(number, head)
        self.prs[number]['base']['sha'] = self.base
        self.entries.append(MergeGroupAdmissionTest.entry(number, synthetic, head))

    def check(self, base, group, enabled=True):
        event = {'merge_group': {'base_sha': base, 'head_sha': group, 'base_ref': 'refs/heads/main'}}
        with mock.patch.dict(os.environ, {'GITHUB_SERVER_URL': self.root.as_uri()}):
            return policy.check_event(self.api, 'merge_group', event, group, RULES,
                                      require_merge_group_review=enabled)

    def test_matching_squash_tree_admits_with_a_stale_pr_base(self):
        head = self.head('first', 'first.txt', 'reviewed\n')
        parent = self.head('main-update', 'unrelated.txt', 'base update\n')
        synthetic = self.synthetic(parent, head)
        self.member(1, synthetic, head)
        self.assertEqual(self.check(parent, synthetic), [])
        self.api.check_runs.assert_called_once_with(head)

    def test_stable_replacement_head_with_success_cannot_authorize_old_squash_tree(self):
        original = self.head('first', 'first.txt', 'reviewed old version\n')
        synthetic = self.synthetic(self.base, original)
        replacement = self.head('replacement', 'first.txt', 'replacement version\n')
        self.member(1, synthetic, replacement)
        self.assertEqual(self.check(self.base, synthetic, enabled=False), [])
        with self.assertRaisesRegex(policy.PolicyError, f'PR #1 head {replacement}.*squash tree mismatch'):
            self.check(self.base, synthetic)

    def test_conflicting_reviewed_head_refuses(self):
        head = self.head('first', 'shared.txt', 'reviewed change\n')
        parent = self.head('main-update', 'shared.txt', 'conflicting base update\n')
        synthetic = self.git('commit-tree', self.git('rev-parse', head + '^{tree}'), '-p', parent,
                             input=b'Improve validation\n')
        self.member(1, synthetic, head)
        with self.assertRaisesRegex(policy.PolicyError, f'PR #1 head {head}.*squash tree proof conflicts'):
            self.check(parent, synthetic)

    def test_two_pr_squash_group_proves_each_position(self):
        first = self.head('first', 'first.txt', 'first reviewed change\n')
        second = self.head('second', 'second.txt', 'second reviewed change\n')
        synthetic1 = self.synthetic(self.base, first)
        synthetic2 = self.synthetic(synthetic1, second)
        self.member(1, synthetic1, first)
        self.member(2, synthetic2, second)
        self.assertEqual(self.check(self.base, synthetic2), [])
        self.assertEqual({call.args[0] for call in self.api.check_runs.call_args_list}, {first, second})
        for number, path in ((1, 'first.txt'), (2, 'second.txt')):
            replacement = self.head(f'replacement-{number}', path, 'unreviewed replacement\n')
            original = self.prs[number]['head']['sha']
            self.prs[number]['head']['sha'] = replacement
            self.entries[number - 1]['pullRequest']['headRefOid'] = replacement
            with self.subTest(number=number), self.assertRaisesRegex(
                    policy.PolicyError, f'PR #{number} head {replacement}.*squash tree mismatch'):
                self.check(self.base, synthetic2)
            self.prs[number]['head']['sha'] = original
            self.entries[number - 1]['pullRequest']['headRefOid'] = original


class StandaloneReviewGateTest(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            'standalone_review_gate', ROOT / '.github/actions/review-gate/review_gate.py')
        self.gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.gate)
        import sys
        self.resolver = sys.modules['contribution_policy']
        self.api = mock.Mock(repository='example/repo', token='read-token')
        self.api.get.return_value = pull()
        self.api.queue_entries.return_value = [MergeGroupAdmissionTest.entry(1, GROUP, HEAD)]
        self.api.pages.return_value = []
        self.api.check_runs.return_value = []

    def invoke(self, event_name='merge_group', synthetic=None):
        action = yaml.safe_load((ROOT / '.github/actions/review-gate/action.yml').read_text())
        args = shlex.split(action['runs']['steps'][0]['run'])[2:]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'event.json'
            path.write_text(json.dumps({'merge_group': {'base_sha': BASE, 'head_sha': GROUP,
                                                        'base_ref': 'refs/heads/main'}}))
            env = {'GITHUB_EVENT_NAME': event_name, 'GITHUB_EVENT_PATH': str(path),
                   'GITHUB_REPOSITORY': 'example/repo', 'CONTRIBUTION_POLICY_TOKEN': 'read-token'}
            output = io.StringIO()
            with mock.patch.dict(os.environ, env), mock.patch.object(self.gate, 'GitHub', return_value=self.api), \
                    mock.patch.object(self.resolver, 'range_commits', side_effect=lambda base, head, *_:
                                      [synthetic or group_commit()] if head == GROUP else [commit(head)]), \
                    contextlib.redirect_stdout(output):
                return self.gate.main(args), output.getvalue()

    def test_real_shared_resolver_and_review_verdict_refuse_queued_missing_review(self):
        status, output = self.invoke()
        self.assertEqual(status, 1)
        self.assertIn('exact-head-review: missing at PR #1 head ' + HEAD, output)
        self.assertEqual(self.api.queue_entries.call_count, 2)
        self.api.queue_entries.assert_called_with('main')

    def test_standalone_cannot_skip_immutable_head_binding(self):
        self.api.check_runs.return_value = [run()]
        status, output = self.invoke(synthetic=commit(GROUP))
        self.assertEqual(status, 2)
        self.assertIn('PR #1 head ' + HEAD + ': immutable merge-group tree/parents are incomplete', output)

    def test_standalone_controls(self):
        self.api.check_runs.return_value = [run()]
        self.assertEqual(self.invoke()[0], 0)
        self.api.get.return_value = pull(title='Improve validation')
        self.api.check_runs.reset_mock()
        self.assertEqual(self.invoke()[0], 0)
        self.api.check_runs.assert_not_called()
        self.assertEqual(self.invoke('pull_request')[0], 0)

    def test_membership_api_error_fails_closed(self):
        self.api.queue_entries.side_effect = self.resolver.PolicyError('HTTP 503')
        status, output = self.invoke()
        self.assertEqual(status, 2)
        self.assertIn('HTTP 503', output)

    def test_every_local_verdict_caller_has_review_read_permissions(self):
        callers = set()
        for path in (ROOT / '.github/workflows').glob('*.yml'):
            workflow = yaml.safe_load(path.read_text())
            for name, job in workflow.get('jobs', {}).items():
                if not any(step.get('uses') in {'./.github/actions/ci-ok', './.github/actions/needs-pass'}
                           for step in job.get('steps', [])):
                    continue
                callers.add((path.name, name))
                permissions = job.get('permissions', workflow.get('permissions', {}))
                for scope in ('contents', 'pull-requests', 'checks'):
                    with self.subTest(workflow=path.name, job=name, scope=scope):
                        self.assertIn(permissions.get(scope), ('read', 'write'))
        self.assertEqual(callers, {('project-control.yml', 'ci-ok'),
                                   ('project-control.yml', 'template-verdict')})

    def test_needs_pass_uses_the_same_enabled_cli_path(self):
        action = yaml.safe_load((ROOT / '.github/actions/needs-pass/action.yml').read_text())
        step = next(step for step in action['runs']['steps'] if step['name'] == 'Check contribution metadata')
        args = [arg.replace('$CONTRIBUTION_POLICY_SCOPE', 'all') for arg in shlex.split(step['run'])[2:]]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'event.json'
            path.write_text(json.dumps({'merge_group': {'base_sha': BASE, 'head_sha': GROUP,
                                                        'base_ref': 'refs/heads/main'}}))
            env = {'GITHUB_EVENT_NAME': 'merge_group', 'GITHUB_EVENT_PATH': str(path),
                   'GITHUB_REPOSITORY': 'example/repo', 'CONTRIBUTION_POLICY_TOKEN': 'read-token'}
            with mock.patch.dict(os.environ, env), mock.patch.object(policy, 'GitHub', return_value=self.api), \
                    mock.patch.object(policy, 'range_commits', side_effect=lambda base, head, *_: [group_commit() if head == GROUP else commit(head)]), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(policy.main(args), 1)


if __name__ == '__main__':
    unittest.main()
