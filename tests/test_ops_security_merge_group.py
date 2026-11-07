"""The operator stamp stays PR-head-only; the group projection is conjunctive."""
import copy
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import unittest
import tempfile
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('ops_security_group', ROOT / 'scripts/ops_security_merge_group.py')
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)
policy = gate.policy
BASE, HEAD1, HEAD2, GROUP1, GROUP2 = (c * 40 for c in 'abcde')


def pull(number, head, **extra):
    return {'number': number, 'head': {'sha': head}, 'base': {'sha': BASE, 'ref': 'main'},
            'title': 'Improve validation', 'body': '', **extra}


def commit(sha, *parents):
    return {'sha': sha, 'raw_commit': (f'tree {BASE}\n' + ''.join(f'parent {p}\n' for p in parents) +
                                      '\nImprove validation\n').encode()}


def entry(number, head, synthetic):
    return {'baseCommit': {'oid': BASE}, 'headCommit': {'oid': synthetic}, 'pullRequest': {'number': number, 'headRefOid': head}}


def approval(identifier=1, state='success', context='ops-security/review', *, head=HEAD1, app=1299545):
    return {'id': identifier, 'name': context, 'status': 'completed', 'conclusion': state,
            'app': {'id': app}, 'head_sha': head}


def status(identifier=1, state='success', context='ops-security/review'):
    # Retained original REST-shaped attack input. Commit statuses are no longer
    # an operator approval interface and cannot satisfy the App-bound rule.
    return {'id': identifier, 'context': context, 'state': state, 'creator': {'login': 'shtse8'}}


class GroupStampTest(unittest.TestCase):
    def setUp(self):
        self.event = {'repository': {'full_name': 'SylphxAI/.github', 'visibility': 'public', 'private': False},
                      'merge_group': {'base_sha': BASE, 'head_sha': GROUP2, 'base_ref': 'refs/heads/main'}}
        self.prs = {1: pull(1, HEAD1), 2: pull(2, HEAD2)}
        self.api = mock.Mock(repository='SylphxAI/.github', token='read-token')
        def get(path):
            if path.startswith('commits/'):
                item = next(row for row in self.commits if row['sha'] == path.split('/')[1])
                parents = [line[7:] for line in item['raw_commit'].decode().splitlines() if line.startswith('parent ')]
                return {'sha': item['sha'], 'parents': [{'sha': head} for head in parents]}
            return copy.deepcopy(self.prs[int(path.split('/')[1])])
        self.api.get.side_effect = get
        self.api.queue_entries.return_value = [entry(1, HEAD1, GROUP1), entry(2, HEAD2, GROUP2)]
        self.api.pages.return_value = []
        self.api.operator_checks.side_effect = lambda head: [approval(head=head)]
        self.commits = [commit(GROUP1, BASE, HEAD1), commit(GROUP2, GROUP1, HEAD2)]

    def check(self):
        def commits(base, head, *_):
            return self.commits if head == GROUP2 else [commit(head)]
        with mock.patch.object(policy, 'range_commits', side_effect=commits):
            return gate.check(self.api, self.event, GROUP2)

    def test_all_entries_ahead_are_checked_not_just_named_ref_pr(self):
        self.event['merge_group']['head_ref'] = 'refs/heads/gh-readonly-queue/main/pr-2-' + GROUP2
        self.assertEqual(self.check(), ([], 2))
        self.assertEqual(self.api.operator_checks.call_args_list, [mock.call(HEAD1), mock.call(HEAD2)])

    def test_earlier_missing_stamp_refuses_even_when_tip_is_stamped(self):
        self.api.operator_checks.side_effect = lambda head: [] if head == HEAD1 else [approval(head=head)]
        findings, count = self.check()
        self.assertEqual(count, 2)
        self.assertIn('missing', findings[0]['rule'])
        self.assertIn(HEAD1, findings[0]['location'])

    def test_latest_status_wins_in_any_response_order(self):
        for state in ('failure', 'error', 'pending', None):
            for rows in ([approval(2, state), approval(1)], [approval(1), approval(2, state)]):
                with self.subTest(state=state, rows=rows):
                    self.api.operator_checks.side_effect = lambda head: [{**row, "head_sha": head} for row in rows]
                    self.assertTrue(self.check()[0])
        self.api.operator_checks.side_effect = lambda head: [approval(1, 'failure', head=head), approval(2, head=head)]
        self.assertEqual(self.check(), ([], 2))

    def test_unrelated_success_is_not_a_stamp(self):
        self.api.operator_checks.side_effect = lambda head: [approval(context='ci-ok', head=head)]
        self.assertIn('missing', self.check()[0][0]['rule'])

    def test_only_authorized_operator_can_supply_the_latest_success(self):
        for app in (15368, 5155317, None, 999999999):
            self.api.operator_checks.side_effect = lambda head: [approval(head=head, app=app)]
            self.assertIn('missing canonical App approval', self.check()[0][0]['rule'])
        # Unrelated publishers cannot either approve or supersede a canonical
        # decision, matching GitHub's integration binding.
        self.api.operator_checks.side_effect = lambda head: [approval(head=head), approval(2, head=head, app=15368)]
        self.assertEqual(self.check(), ([], 2))

    def test_original_wrong_creator_status_counterexamples_are_not_approvals(self):
        forged = {**status(102), 'creator': {'id': 999999999, 'login': 'not-the-designated-operator', 'type': 'User'}}
        failure = {**status(101, 'failure'), 'creator': {'id': 8020099, 'login': 'shtse8', 'type': 'User'}}
        for rows in ([forged], [failure, forged]):
            # The unchanged REST payload bytes cannot masquerade as App Check Runs.
            self.api.operator_checks.side_effect = None
            self.api.operator_checks.return_value = rows
            self.assertIn('missing canonical App approval', self.check()[0][0]['rule'])
        self.api.statuses.assert_not_called()

    def test_neutral_skipped_pending_and_wrong_head_are_not_approval(self):
        for change in ({'conclusion': 'neutral'}, {'conclusion': 'skipped'}, {'status': 'in_progress'},
                       {'head_sha': 'f' * 40}):
            self.api.operator_checks.side_effect = lambda head: [{**approval(head=head), **change}]
            self.assertTrue(self.check()[0])

    def test_api_parent_list_must_match_the_immutable_object(self):
        original = self.api.get.side_effect
        self.api.get.side_effect = lambda path: ({'sha': GROUP2, 'parents': [{'sha': BASE}, {'sha': HEAD1}]}
                                               if path == 'commits/' + GROUP2 else original(path))
        with self.assertRaisesRegex(policy.PolicyError, 'API merge-group parents disagree'):
            self.check()

    def test_status_api_error_fails_closed(self):
        self.api.operator_checks.side_effect = policy.PolicyError('HTTP 503')
        self.assertIn('API read error', self.check()[0][0]['rule'])

    def test_already_merged_earlier_entry_is_still_checked(self):
        self.api.queue_entries.return_value = [entry(2, HEAD2, GROUP2)]
        self.prs[1].update(merged_at='2026-11-07T00:00:00Z', merge_commit_sha=GROUP1)
        self.api.pages.side_effect = lambda path: [pull(1, HEAD1, merged_at='2026-11-07T00:00:00Z',
                                                     merge_commit_sha=GROUP1)] if GROUP1 in path else []
        self.api.operator_checks.side_effect = lambda head: [] if head == HEAD1 else [approval(head=head)]
        findings, count = self.check()
        self.assertEqual(count, 2)
        self.assertIn(HEAD1, findings[0]['location'])

    def test_missing_member_or_queue_api_error_refuses(self):
        self.api.queue_entries.return_value = [entry(2, HEAD2, GROUP2)]
        with self.assertRaisesRegex(policy.PolicyError, 'membership is incomplete'):
            self.check()
        self.api.queue_entries.side_effect = policy.PolicyError('HTTP 403')
        with self.assertRaises(policy.PolicyError):
            self.check()

    def test_non_review_title_still_binds_exact_head_and_squash_tree(self):
        self.commits[0] = commit(GROUP1, BASE, HEAD2)
        with self.assertRaisesRegex(policy.PolicyError, 'membership/head changed'):
            self.check()
        self.commits[0] = commit(GROUP1, BASE)
        with mock.patch.object(policy, 'squash_merge_tree', return_value=GROUP2):
            with self.assertRaisesRegex(policy.PolicyError, 'squash tree mismatch'):
                self.check()
        with mock.patch.object(policy, 'squash_merge_tree', return_value=BASE):
            self.assertEqual(self.check(), ([], 2))

    def test_rechecks_membership_and_head_after_stamp_inventory(self):
        self.api.queue_entries.side_effect = [self.api.queue_entries.return_value, []]
        self.assertTrue(self.check()[0])

    def test_private_unknown_or_wrong_candidate_never_admits(self):
        for key, value in (('visibility', 'private'), ('visibility', None), ('private', True),
                           ('full_name', 'SylphxAI/skills')):
            event = copy.deepcopy(self.event)
            event['repository'][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(policy.PolicyError):
                gate.check(self.api, event, GROUP2)
        with self.assertRaises(policy.PolicyError):
            gate.check(self.api, self.event, HEAD2)


class StatusPaginationTest(unittest.TestCase):
    def test_full_history_and_later_error_are_not_hidden(self):
        api = gate.GitHub('SylphxAI/.github', 'read-token')
        rows = [approval(i) for i in range(1, 101)]
        with mock.patch.object(api, 'get', side_effect=[{'total_count': 101, 'check_runs': rows},
                                                       {'total_count': 101, 'check_runs': [approval(101)]}]):
            self.assertEqual(len(api.operator_checks(HEAD1)), 101)
        with mock.patch.object(api, 'get', side_effect=[{'total_count': 101, 'check_runs': rows}, policy.PolicyError('HTTP 503')]):
            with self.assertRaises(policy.PolicyError):
                api.operator_checks(HEAD1)
        for response in ({}, {'total_count': 1, 'check_runs': [None]},
                         {'total_count': 1, 'check_runs': [approval(0)]},
                         {'total_count': 2, 'check_runs': [approval(), approval()]},
                         {'total_count': 2, 'check_runs': [approval()]},
                         {'total_count': 0, 'check_runs': [approval()]}):
            with self.subTest(response=response), mock.patch.object(api, 'get', return_value=response):
                with self.assertRaises(policy.PolicyError):
                    api.operator_checks(HEAD1)


class TrustedSourceTest(unittest.TestCase):
    def setUp(self):
        self.repository = {'id': 1091169653, 'full_name': 'SylphxAI/.github', 'visibility': 'public',
                           'private': False, 'default_branch': 'main'}
        self.run = {'id': 42, 'event': 'merge_group', 'workflow_id': 7, 'head_sha': GROUP2,
                    'head_branch': 'gh-readonly-queue/main/pr-2-' + GROUP2, 'repository': self.repository}
        self.api = mock.Mock()
        self.api.get.side_effect = lambda path: (self.repository if path == '' else
                                                {'id': 7} if path == 'actions/workflows/project-control.yml' else self.run)
        self.api.queue_entries.return_value = [entry(1, HEAD1, GROUP1), entry(2, HEAD2, GROUP2)]

    def test_native_run_and_queue_define_target_without_candidate_artifacts(self):
        event, run = gate.trusted_group(self.api, 'workflow_run', {'workflow_run': {'id': 42, 'head_sha': HEAD1},
                                                                'merge_group': {'base_sha': HEAD2}})
        self.assertEqual(run, 42)
        self.assertEqual(event['merge_group']['base_sha'], BASE)
        self.assertEqual(event['merge_group']['head_sha'], GROUP2)
        self.assertEqual(gate.trusted_group(self.api, 'workflow_dispatch', {}, '42')[0], event)

    def test_non_native_source_run_cannot_approve_a_pr_head(self):
        for key, value in (('event', 'pull_request'), ('head_sha', HEAD1), ('workflow_id', 9),
                           ('head_branch', 'feature'), ('id', 99)):
            with self.subTest(key=key):
                saved = self.run[key]
                self.run[key] = value
                with self.assertRaises(policy.PolicyError):
                    gate.trusted_group(self.api, 'workflow_run', {'workflow_run': {'id': 42}})
                self.run[key] = saved
        for event in ('merge_group', 'pull_request', 'pull_request_target'):
            with self.assertRaises(policy.PolicyError):
                gate.trusted_group(self.api, event, {}, '42')

    def test_retired_duplicated_or_incomplete_queue_entry_refuses(self):
        for entries in ([], [entry(1, HEAD1, GROUP2), entry(2, HEAD2, GROUP2)],
                        [{**entry(2, HEAD2, GROUP2), 'baseCommit': None}]):
            self.api.queue_entries.return_value = entries
            with self.assertRaises(policy.PolicyError):
                gate.trusted_group(self.api, 'workflow_run', {'workflow_run': {'id': 42}})

    def test_unknown_private_or_recreated_repository_refuses(self):
        for key, value in (('id', 99), ('visibility', None), ('visibility', 'private'), ('private', True),
                           ('full_name', 'SylphxAI/skills'), ('default_branch', 'release')):
            with self.subTest(key=key):
                saved = self.repository[key]
                self.repository[key] = value
                with self.assertRaises(policy.PolicyError):
                    gate.trusted_group(self.api, 'workflow_run', {'workflow_run': {'id': 42}})
                self.repository[key] = saved


class ProjectionEntrypointTest(unittest.TestCase):
    """Drive the complete main wrapper through source resolution and publication."""
    setUp = TrustedSourceTest.setUp
    def execute(self, directory, checks=None, mutate=None):
        fx = GroupStampTest('runTest')
        fx.setUp()
        if checks is not None:
            fx.api.operator_checks.side_effect = checks
        # Preserve real immutable membership checks and operator gate, mocking
        # only provider transports and the final App transport.
        original = fx.api.get.side_effect
        fx.api.get.side_effect = lambda path: original(path) if path.startswith(('commits/', 'pulls/')) else self.api.get(path)
        fx.api.queue_entries.return_value = self.api.queue_entries.return_value
        event = Path(directory) / 'event.json'
        event.write_text(json.dumps({'workflow_run': {'id': 42}}))
        (Path(directory) / 'pr-review-auth.json').write_text(json.dumps({'checkToken': 'offline-app-token'}))
        env = {'GITHUB_REPOSITORY': 'SylphxAI/.github', 'GITHUB_REF': 'refs/heads/main',
               'GITHUB_WORKFLOW_REF': 'SylphxAI/.github/.github/workflows/pr-review.yml@refs/heads/main',
               'GITHUB_EVENT_NAME': 'workflow_run', 'GITHUB_EVENT_PATH': str(event), 'GH_TOKEN': 'read-token',
               'RUNNER_TEMP': directory, 'GITHUB_RUN_ID': '90', 'GITHUB_RUN_ATTEMPT': '1'}
        def commits(base, head, *_):
            if mutate:
                mutate()
            return fx.commits if head == GROUP2 else [commit(head)]
        output = io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch.object(gate, 'GitHub', return_value=fx.api), \
                mock.patch.object(policy, 'range_commits', side_effect=commits), \
                mock.patch.object(gate.publisher_module, 'AppGitHub') as app, \
                mock.patch.object(gate.publisher_module, 'publish_check', return_value={'check_run_id': 17}) as publish, \
                contextlib.redirect_stdout(output):
            result = gate.main()
        return result, output.getvalue(), publish, app

    def test_default_branch_native_group_success_publishes_only_combined_sha(self):
        with tempfile.TemporaryDirectory() as directory:
            result, output, publish, app = self.execute(directory)
        self.assertEqual(result, 0)
        publish.assert_called_once()
        self.assertEqual(publish.call_args.args[1:3], (GROUP2, 'success'))
        self.assertNotIn(HEAD1, [call.args[1] for call in publish.call_args_list])
        app.assert_called_once_with('SylphxAI/.github', 'offline-app-token')

    def test_missing_member_publishes_failure_and_names_offender(self):
        with tempfile.TemporaryDirectory() as directory:
            result, output, publish, _ = self.execute(directory, checks=lambda head: [] if head == HEAD1 else [approval(head=head)])
        self.assertEqual(result, 1)
        self.assertIn('missing canonical App approval', output)
        self.assertIn(HEAD1, output)
        self.assertEqual(publish.call_args.args[1:3], (GROUP2, 'failure'))

    def test_retired_group_during_inventory_never_receives_a_check(self):
        with tempfile.TemporaryDirectory() as directory:
            result, output, publish, _ = self.execute(directory, mutate=lambda: self.run.update(head_sha=HEAD1))
        self.assertEqual(result, 2)
        self.assertIn('absent or duplicated', output)
        publish.assert_not_called()

    def test_push_during_app_inventory_is_rejected_after_reads(self):
        fx = GroupStampTest('runTest')
        fx.setUp()
        def checks(head):
            if head == HEAD2:
                fx.prs[1]['head']['sha'] = 'f' * 40
            return [approval(head=head)]
        fx.api.operator_checks.side_effect = checks
        self.assertTrue(any('membership/head changed' in row['rule'] for row in fx.check()[0]))


class QueueMetadataPaginationTest(unittest.TestCase):
    @staticmethod
    def response(rows, total, more=False, cursor=None):
        return {'data': {'repository': {'mergeQueue': {'entries': {'totalCount': total, 'nodes': rows,
                'pageInfo': {'hasNextPage': more, 'endCursor': cursor}}}}}}

    def test_authoritative_base_is_retained_across_all_pages(self):
        api = gate.GitHub('SylphxAI/.github', 'read-token')
        pages = [self.response([entry(1, HEAD1, GROUP1)], 2, True, 'next'),
                 self.response([entry(2, HEAD2, GROUP2)], 2)]
        with mock.patch.object(api, 'read', side_effect=pages) as read:
            rows = api.queue_entries('main')
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]['baseCommit']['oid'], BASE)
        self.assertEqual(read.call_args.args[1]['variables']['cursor'], 'next')

    def test_partial_changed_duplicate_cursor_and_unavailable_queue_refuse(self):
        api = gate.GitHub('SylphxAI/.github', 'read-token')
        for pages in ([self.response([], 1)], [{'data': {'repository': {'mergeQueue': None}}}],
                      [{'errors': [{'message': 'denied'}]}],
                      [self.response([entry(1, HEAD1, GROUP1)], 2, True, 'next'), self.response([], 3)],
                      [self.response([], 2, True, 'next')],
                      [self.response([entry(1, HEAD1, GROUP1)], 3, True, 'next'),
                       self.response([entry(2, HEAD2, GROUP2)], 3, True, 'next')]):
            with self.subTest(pages=pages), mock.patch.object(api, 'read', side_effect=pages):
                with self.assertRaises(policy.PolicyError):
                    api.queue_entries('main')


class ProducerContractTest(unittest.TestCase):
    def test_no_pr_head_producer_or_candidate_write_permission(self):
        workflow = yaml.safe_load((ROOT / '.github/workflows/pr-review.yml').read_text())
        triggers = workflow.get('on', workflow.get(True))
        self.assertEqual(set(triggers), {'workflow_run', 'workflow_dispatch'})
        job = workflow['jobs']['operator-projection']
        self.assertEqual(job['permissions'], {'contents': 'read', 'pull-requests': 'read', 'checks': 'read',
                                              'actions': 'read', 'id-token': 'write'})
        self.assertIn("github.event.workflow_run.event == 'merge_group'", job['if'])
        self.assertIn("github.ref == 'refs/heads/main'", job['if'])
        checkout = job['steps'][0]['with']
        self.assertEqual(checkout['ref'], '${{ github.workflow_sha }}')
        self.assertFalse(checkout['persist-credentials'])
        self.assertFalse((ROOT / '.github/workflows/ops-security-merge-group.yml').exists())
        self.assertEqual(job['steps'][-2]['run'], 'python3 scripts/ops_security_merge_group.py')
        for event in ('merge_group', 'pull_request', 'push'):
            with self.subTest(event=event), mock.patch.dict(os.environ, {'GITHUB_EVENT_NAME': event,
                    'GITHUB_REPOSITORY': 'SylphxAI/.github'}), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(gate.main(), 2)

    def test_bound_required_check_policy_round_trips(self):
        spec = importlib.util.spec_from_file_location('merge_queue_policy', ROOT / 'scripts/apply_merge_queue.py')
        queue = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(queue)
        data = queue.load_policy(ROOT / 'policy/merge-queue.json')
        checks = queue.desired_checks(data, 'SylphxAI/.github', 20321046, 'sylphx-merge-queue')
        self.assertIn({'context': 'ops-security/review', 'integration_id': 1299545}, checks)
        self.assertIn({'context': 'ops-security/review', 'integration_id': 1299545}, queue.merge_checks([], checks))

    def test_projection_keeps_the_initial_pr_head_requirement(self):
        declared = json.loads((ROOT / 'policy/merge-queue.json').read_text())
        scoped = [row for row in declared['required_checks'] if row['repo'] == 'SylphxAI/.github']
        self.assertEqual(len(scoped), 1)
        self.assertEqual(scoped[0]['ruleset_id'], 20321046)
        self.assertIn({'context': 'ops-security/review', 'integration_id': 1299545}, scoped[0]['checks'])
        skills = json.loads((ROOT / 'policy/public-skills-operator-review.json').read_text())
        self.assertEqual(skills['conditions']['ref_name'], {'include': ['refs/heads/main'], 'exclude': []})
        checks = skills['rules'][0]['parameters']['required_status_checks']
        self.assertIn({'context': 'ops-security/review', 'integration_id': 1299545}, checks)
        self.assertEqual(skills['bypass_actors'], [])


if __name__ == '__main__':
    unittest.main()
