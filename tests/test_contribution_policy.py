"""Acceptance fixtures and complete acquisition through the public entry point."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
import urllib.error

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / '.github/actions/contribution-policy/contribution_policy.py'
spec = importlib.util.spec_from_file_location('contribution_policy', SCRIPT)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)
POLICY = json.loads(checker.POLICY.read_text())
IDENTITY = {'name': 'Kyle Tse', 'email': 'hi@kylet.se'}
HEAD = 'a' * 40
BASE = 'b' * 40
WORK = 'W-' + '12345'


def commit(sha=HEAD, message='Improve parser'):
    return {'sha': sha, 'commit': {'author': IDENTITY.copy(), 'committer': IDENTITY.copy(), 'message': message}}


def pull():
    return {'number': 1, 'state': 'open', 'title': 'Improve parser', 'body': 'Validated parser behavior.',
            'head': {'sha': HEAD, 'repo': {'updated_at': 'before'}},
            'base': {'sha': BASE, 'ref': 'main', 'repo': {'updated_at': 'before'}}}


class ContributionPolicy(unittest.TestCase):
    def test_acceptance_fixtures_at_cli(self):
        fixtures = json.loads((ROOT / 'tests/fixtures/contribution-policy.json').read_text())
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / 'snapshot.json'
            report = Path(directory) / 'report.json'
            for case in fixtures:
                with self.subTest(case=case['name']):
                    pr, item = pull(), commit()
                    for field in ('title', 'body'):
                        if field in case:
                            pr[field] = case[field]
                    for field in ('message', 'author', 'committer'):
                        if field in case:
                            item['commit'][field] = case[field]
                    snapshot.write_text(json.dumps({'pull_request': pr, 'commits': [item]}))
                    with contextlib.redirect_stdout(io.StringIO()):
                        status = checker.main(['--input', str(snapshot), '--report', str(report)])
                    result = json.loads(report.read_text())
                    self.assertEqual(status, 0 if case.get('pass') else 1)
                    if not case.get('pass'):
                        self.assertIn(case['rule'], {finding['rule'] for finding in result['findings']})

    def test_each_approved_pair_passes_in_its_configured_roles(self):
        for identity in POLICY['identities']:
            with self.subTest(identity=identity):
                item = commit()
                for role in identity['roles']:
                    item['commit'][role] = identity
                self.assertEqual(checker.evaluate(pull(), [item], POLICY), [])

    def test_identity_pair_cannot_mix_approved_names_and_emails(self):
        item = commit()
        item['commit']['author']['email'] = 'noreply@github.com'
        self.assertEqual(checker.evaluate(None, [item], POLICY)[0]['rule'], 'unapproved-author')

    def test_refusal_does_not_repeat_untrusted_text(self):
        item = commit(message=WORK + ' ::notice::untrusted')
        findings = checker.evaluate(None, [item], POLICY)
        self.assertNotIn(WORK, json.dumps(findings))
        self.assertNotIn('::notice::', json.dumps(findings))

    def test_missing_metadata_cannot_read_as_clean(self):
        with self.assertRaises(checker.PolicyError):
            checker.evaluate({}, [], POLICY)
        item = commit()
        del item['commit']['message']
        with self.assertRaises(checker.PolicyError):
            checker.evaluate(None, [item], POLICY)

    def test_unreadable_private_metadata_fails_without_token_output(self):
        api = checker.GitHub('example/repo', 'read-token')
        error = urllib.error.HTTPError('https://api.github.com/', 403, 'forbidden', {}, None)
        with mock.patch.object(checker.urllib.request, 'urlopen', side_effect=error):
            with self.assertRaisesRegex(checker.PolicyError, r'HTTP 403') as caught:
                api.get('pulls/1')
        self.assertNotIn('read-token', str(caught.exception))

    def test_current_metadata_read_and_immutable_head_are_required(self):
        api = mock.Mock(repository='example/repo', token='read-token')
        for field in ('title', 'body'):
            before, after = pull(), pull()
            after[field] = 'Changed while checking'
            api.get.side_effect = [before, after]
            with mock.patch.object(checker, 'range_commits', return_value=[commit()]):
                with self.assertRaises(checker.PolicyError):
                    checker.pull_snapshot(api, 1, HEAD)
        api.get.side_effect = None
        api.get.return_value = pull()
        with self.assertRaises(checker.PolicyError):
            checker.pull_snapshot(api, 1, 'c' * 40)

    def test_unrelated_repository_updates_do_not_refuse(self):
        api = mock.Mock(repository='example/repo', token='read-token')
        before, after = pull(), pull()
        after['head']['repo']['updated_at'] = 'after'
        after['base']['sha'] = 'c' * 40
        api.get.side_effect = [before, after]
        with mock.patch.object(checker, 'range_commits', return_value=[commit()]):
            self.assertEqual(checker.pull_snapshot(api, 1, HEAD), (after, [commit()]))

    def test_pull_event_fetches_live_text_instead_of_event_copy(self):
        api = mock.Mock(repository='example/repo', token='read-token')
        current = pull()
        current['body'] = WORK
        api.get.return_value = current
        with mock.patch.object(checker, 'range_commits', return_value=[commit()]):
            findings = checker.check_event(api, 'pull_request', {'number': 1, 'pull_request': pull()}, HEAD, POLICY)
        self.assertIn('work-id', {row['rule'] for row in findings})

    def test_merge_group_checks_original_commits_even_when_squash_is_clean(self):
        api = mock.Mock(repository='example/repo', token='read-token')
        api.queue_entries.return_value = [{'headCommit': {'oid': HEAD},
                                          'pullRequest': {'number': 1, 'headRefOid': HEAD}}]
        api.pages.return_value = []
        event = {'merge_group': {'base_sha': BASE, 'head_sha': HEAD, 'base_ref': 'refs/heads/main'}}
        original = commit('c' * 40, 'Co-authored-by: Codex <codex@example.com>')
        with mock.patch.object(checker, 'range_commits', return_value=[commit()]), \
                mock.patch.object(checker, 'pull_snapshot', return_value=(pull(), [original])):
            findings = checker.check_event(api, 'merge_group', event, HEAD, POLICY)
        self.assertIn('tool-coauthor', {row['rule'] for row in findings})
        api.queue_entries.return_value = []
        api.pages.return_value = []
        with mock.patch.object(checker, 'range_commits', return_value=[commit()]):
            with self.assertRaises(checker.PolicyError):
                checker.check_event(api, 'merge_group', event, HEAD, POLICY)

    def test_merged_queue_entry_remains_checked_after_leaving_queue(self):
        api = mock.Mock(repository='example/repo', token='read-token')
        api.queue_entries.return_value = []
        merged = pull()
        merged.update(state='closed', merged_at='2026-10-30T09:00:00Z', merge_commit_sha=HEAD)
        api.pages.return_value = [merged]
        event = {'merge_group': {'base_sha': BASE, 'head_sha': HEAD, 'base_ref': 'refs/heads/main'}}
        with mock.patch.object(checker, 'range_commits', return_value=[commit()]), \
                mock.patch.object(checker, 'pull_snapshot', return_value=(merged, [commit(message='Implemented using Claude.')])):
            findings = checker.check_event(api, 'merge_group', event, HEAD, POLICY)
        self.assertIn('tool-authorship', {row['rule'] for row in findings})

    def test_original_range_does_not_read_unrelated_open_pr_metadata(self):
        api = mock.Mock(repository='example/repo', token='read-token')
        api.queue_entries.return_value = [{'headCommit': {'oid': HEAD},
                                          'pullRequest': {'number': 1, 'headRefOid': 'c' * 40}}]
        unrelated = pull()
        unrelated.update(number=2, body=WORK)
        api.pages.return_value = [unrelated]
        event = {'merge_group': {'base_sha': BASE, 'head_sha': HEAD, 'base_ref': 'refs/heads/main'}}
        original = commit('c' * 40)
        with mock.patch.object(checker, 'range_commits', return_value=[commit(), original]), \
                mock.patch.object(checker, 'pull_snapshot', return_value=(pull(), [original])):
            self.assertEqual(checker.check_event(api, 'merge_group', event, HEAD, POLICY), [])
        api.pages.assert_not_called()

    def test_synthetic_queue_commit_resolves_without_commit_associations(self):
        api = mock.Mock(repository='example/repo', token='read-token')
        api.queue_entries.return_value = [{'headCommit': {'oid': HEAD},
                                          'pullRequest': {'number': 1, 'headRefOid': 'c' * 40}}]
        api.pages.return_value = []
        event = {'merge_group': {'base_sha': BASE, 'head_sha': HEAD, 'base_ref': 'refs/heads/main'}}
        original = commit('c' * 40, 'Implemented using Claude.')
        with mock.patch.object(checker, 'range_commits', return_value=[commit()]), \
                mock.patch.object(checker, 'pull_snapshot', return_value=(pull(), [original])) as snapshot:
            findings = checker.check_event(api, 'merge_group', event, HEAD, POLICY)
        self.assertIn('tool-authorship', {row['rule'] for row in findings})
        snapshot.assert_called_once_with(api, 1, 'c' * 40)
        api.pages.assert_not_called()

    def test_unmapped_group_commit_does_not_read_as_clean(self):
        api = mock.Mock(repository='example/repo', token='read-token')
        api.queue_entries.return_value = [{'headCommit': {'oid': HEAD},
                                          'pullRequest': {'number': 1, 'headRefOid': 'c' * 40}}]
        api.pages.return_value = []
        event = {'merge_group': {'base_sha': BASE, 'head_sha': HEAD, 'base_ref': 'refs/heads/main'}}
        with mock.patch.object(checker, 'range_commits', return_value=[commit(), commit('d' * 40)]), \
                mock.patch.object(checker, 'pull_snapshot', return_value=(pull(), [commit('c' * 40)])):
            with self.assertRaisesRegex(checker.PolicyError, 'membership is incomplete'):
                checker.check_event(api, 'merge_group', event, HEAD, POLICY)

    def test_dispatch_on_pr_head_cannot_skip_policy(self):
        api = mock.Mock(repository='example/repo', token='read-token')
        api.pages.return_value = [pull()]
        bad = pull()
        bad['title'] = WORK
        with mock.patch.object(checker, 'pull_snapshot', return_value=(bad, [commit()])):
            findings = checker.check_event(api, 'workflow_dispatch', {}, HEAD, POLICY)
        self.assertTrue(findings)
        api.pages.assert_called_once_with('pulls?state=open')

    def test_paginated_associations_are_complete(self):
        api = checker.GitHub('example/repo', 'read-token')
        with mock.patch.object(api, 'get', side_effect=[[{'number': n} for n in range(100)], [{'number': 101}]]) as get:
            self.assertEqual(len(api.pages('commits/' + HEAD + '/pulls')), 101)
        self.assertTrue(get.call_args_list[1].args[0].endswith('page=2'))

    def test_queue_pagination_and_permission_errors(self):
        api = checker.GitHub('example/repo', 'read-token')
        def page(nodes, more, cursor):
            return {'data': {'repository': {'mergeQueue': {'entries': {'nodes': nodes,
                    'pageInfo': {'hasNextPage': more, 'endCursor': cursor}}}}}}
        with mock.patch.object(api, 'read', side_effect=[page([{'number': 1}], True, 'next'),
                                                       page([{'number': 2}], False, 'last')]) as read:
            self.assertEqual(len(api.queue_entries('main')), 2)
            self.assertEqual(read.call_args_list[1].args[1]['variables']['cursor'], 'next')
        with mock.patch.object(api, 'read', return_value={'errors': [{'message': 'denied'}]}):
            with self.assertRaises(checker.PolicyError):
                api.queue_entries('main')

    def test_missing_token_fails_pre_merge_but_push_needs_none(self):
        with mock.patch.dict(os.environ, {'GITHUB_EVENT_NAME': 'pull_request', 'CONTRIBUTION_POLICY_TOKEN': ''}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(checker.main([]), 2)
        with mock.patch.dict(os.environ, {'GITHUB_EVENT_NAME': 'push'}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(checker.main([]), 0)

    def test_real_git_range_is_not_truncated_at_api_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote = root / 'example/repo.git'
            remote.parent.mkdir()
            subprocess.run(['git', 'init', '--bare', '-q', str(remote)], check=True)
            records = []
            for index in range(302):
                name = 'Example' if index == 150 else 'Kyle Tse'
                records.append(f'commit refs/heads/main\nauthor {name} <hi@kylet.se> 1000000000 +0000\n'
                               f'committer Kyle Tse <hi@kylet.se> 1000000000 +0000\ndata 6\nChange\n')
            subprocess.run(['git', '-C', str(remote), 'fast-import', '--quiet'], input='\n'.join(records).encode(), check=True)
            shas = subprocess.check_output(['git', '-C', str(remote), 'rev-list', '--reverse', 'main'], text=True).splitlines()
            with mock.patch.dict(os.environ, {'GITHUB_SERVER_URL': root.as_uri()}):
                commits = checker.range_commits(shas[0], shas[-1], 'example/repo', '')
            self.assertEqual(len(commits), 301)
            findings = checker.evaluate(None, commits, POLICY)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0]['location'], shas[150])

    def test_both_verdicts_run_policy_without_waiver(self):
        for action in ('needs-pass', 'ci-ok'):
            doc = yaml.safe_load((ROOT / '.github/actions' / action / 'action.yml').read_text())
            step = doc['runs']['steps'][0]
            self.assertIn('contribution_policy.py', step['run'])
            self.assertNotIn('if', step)
        for name in ('optimistic-gate', 'ci-fast-path', 'rust-ci'):
            document = yaml.safe_load((ROOT / 'workflow-templates' / (name + '.yml')).read_text())
            self.assertIn('edited', document[True]['pull_request']['types'])
            self.assertEqual(document['permissions']['pull-requests'], 'read')


if __name__ == '__main__':
    unittest.main()
