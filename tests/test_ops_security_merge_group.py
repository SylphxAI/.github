"""The operator stamp stays PR-head-only; the group projection is conjunctive."""
import copy
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import unittest
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
    return {'headCommit': {'oid': synthetic}, 'pullRequest': {'number': number, 'headRefOid': head}}


def status(identifier=1, state='success', context='ops-security/review'):
    return {'id': identifier, 'context': context, 'state': state}


class GroupStampTest(unittest.TestCase):
    def setUp(self):
        self.event = {'repository': {'full_name': 'SylphxAI/.github', 'visibility': 'public', 'private': False},
                      'merge_group': {'base_sha': BASE, 'head_sha': GROUP2, 'base_ref': 'refs/heads/main'}}
        self.prs = {1: pull(1, HEAD1), 2: pull(2, HEAD2)}
        self.api = mock.Mock(repository='SylphxAI/.github', token='read-token')
        self.api.get.side_effect = lambda path: copy.deepcopy(self.prs[int(path.split('/')[1])])
        self.api.queue_entries.return_value = [entry(1, HEAD1, GROUP1), entry(2, HEAD2, GROUP2)]
        self.api.pages.return_value = []
        self.api.statuses.return_value = [status()]
        self.commits = [commit(GROUP1, BASE, HEAD1), commit(GROUP2, GROUP1, HEAD2)]

    def check(self):
        def commits(base, head, *_):
            return self.commits if head == GROUP2 else [commit(head)]
        with mock.patch.object(policy, 'range_commits', side_effect=commits):
            return gate.check(self.api, self.event, GROUP2)

    def test_all_entries_ahead_are_checked_not_just_named_ref_pr(self):
        self.event['merge_group']['head_ref'] = 'refs/heads/gh-readonly-queue/main/pr-2-' + GROUP2
        self.assertEqual(self.check(), ([], 2))
        self.assertEqual(self.api.statuses.call_args_list, [mock.call(HEAD1), mock.call(HEAD2)])

    def test_earlier_missing_stamp_refuses_even_when_tip_is_stamped(self):
        self.api.statuses.side_effect = lambda head: [] if head == HEAD1 else [status()]
        findings, count = self.check()
        self.assertEqual(count, 2)
        self.assertIn('missing', findings[0]['rule'])
        self.assertIn(HEAD1, findings[0]['location'])

    def test_latest_status_wins_in_any_response_order(self):
        for state in ('failure', 'error', 'pending', None):
            for rows in ([status(2, state), status(1)], [status(1), status(2, state)]):
                with self.subTest(state=state, rows=rows):
                    self.api.statuses.return_value = rows
                    self.assertTrue(self.check()[0])
        self.api.statuses.return_value = [status(1, 'failure'), status(2)]
        self.assertEqual(self.check(), ([], 2))

    def test_unrelated_success_is_not_a_stamp(self):
        self.api.statuses.return_value = [status(context='ci-ok')]
        self.assertIn('missing', self.check()[0][0]['rule'])

    def test_status_api_error_fails_closed(self):
        self.api.statuses.side_effect = policy.PolicyError('HTTP 503')
        self.assertIn('API read error', self.check()[0][0]['rule'])

    def test_already_merged_earlier_entry_is_still_checked(self):
        self.api.queue_entries.return_value = [entry(2, HEAD2, GROUP2)]
        self.api.pages.side_effect = lambda path: [pull(1, HEAD1, merged_at='2026-11-07T00:00:00Z',
                                                     merge_commit_sha=GROUP1)] if GROUP1 in path else []
        self.api.statuses.side_effect = lambda head: [] if head == HEAD1 else [status()]
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
        api = policy.GitHub('SylphxAI/.github', 'read-token')
        rows = [status(i, context='ci-ok') for i in range(1, 101)]
        with mock.patch.object(api, 'get', side_effect=[rows, [status(101)]]):
            self.assertEqual(len(api.statuses(HEAD1)), 101)
        with mock.patch.object(api, 'get', side_effect=[rows, policy.PolicyError('HTTP 503')]):
            with self.assertRaises(policy.PolicyError):
                api.statuses(HEAD1)
        for response in ({}, [None], [status(0)], [status(), status()]):
            with self.subTest(response=response), mock.patch.object(api, 'get', return_value=response):
                with self.assertRaises(policy.PolicyError):
                    api.statuses(HEAD1)


class ProducerContractTest(unittest.TestCase):
    def test_no_pr_head_producer_or_write_permission(self):
        workflow = yaml.safe_load((ROOT / '.github/workflows/ops-security-merge-group.yml').read_text())
        triggers = workflow.get('on', workflow.get(True))
        self.assertEqual(set(triggers), {'merge_group'})
        self.assertEqual(workflow['jobs']['stamps']['name'], 'ops-security/review')
        self.assertEqual(workflow['permissions'], {'contents': 'read', 'pull-requests': 'read', 'statuses': 'read'})
        self.assertIn("github.event.repository.visibility == 'public'", workflow['jobs']['stamps']['runs-on'])
        for event in ('pull_request', 'push', 'workflow_dispatch'):
            with self.subTest(event=event), mock.patch.dict(os.environ, {'GITHUB_EVENT_NAME': event,
                    'GITHUB_REPOSITORY': 'SylphxAI/.github'}), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(gate.main(), 2)

    def test_unbound_required_check_policy_round_trips(self):
        spec = importlib.util.spec_from_file_location('merge_queue_policy', ROOT / 'scripts/apply_merge_queue.py')
        queue = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(queue)
        data = queue.load_policy(ROOT / 'policy/merge-queue.json')
        checks = queue.desired_checks(data, 'SylphxAI/.github', 20321046, 'sylphx-merge-queue')
        self.assertIn({'context': 'ops-security/review', 'integration_id': None}, checks)
        self.assertIn({'context': 'ops-security/review', 'integration_id': None},
                      queue.merge_checks([], checks))


if __name__ == '__main__':
    unittest.main()
