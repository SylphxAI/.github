"""Acceptance fixtures and complete acquisition through the public entry point."""
import contextlib
import copy
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
            'user': {'login': 'shtse8', 'id': 8020099, 'type': 'User'},
            'head': {'sha': HEAD, 'repo': {'updated_at': 'before'}},
            'base': {'sha': BASE, 'ref': 'main', 'repo': {'updated_at': 'before'}}}


def signed_object(payload, signature):
    headers, separator, message = payload.partition('\n\n')
    return (headers + '\ngpgsig ' + signature.replace('\n', '\n ') + separator + message).encode()


def signed_github(item):
    """Pair an immutable local object with the provider's signature response."""
    remote = copy.deepcopy(item)
    remote.pop('raw_commit', None)
    metadata = item['commit']
    payload = (f'tree {BASE}\nparent {BASE}\n'
               f'author {metadata["author"]["name"]} <{metadata["author"]["email"]}> 1000000000 +0000\n'
               f'committer {metadata["committer"]["name"]} <{metadata["committer"]["email"]}> 1000000000 +0000\n'
               f'\n{metadata["message"]}')
    signature = '-----BEGIN PGP SIGNATURE-----\nfixture\n-----END PGP SIGNATURE-----'
    item['raw_commit'] = signed_object(payload, signature)
    remote['committer'] = {'login': 'web-flow'}
    remote['commit']['verification'] = {'verified': True, 'reason': 'valid',
                                         'signature': signature, 'payload': payload}
    return remote


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

    def test_pull_request_opener_must_be_approved_because_it_authors_the_queue_squash(self):
        cases = [
            ('approved user', {'login': 'shtse8', 'id': 8020099, 'type': 'User'}, None),
            ('agent application', {'login': 'sylphx-desk-studio[bot]', 'id': 336710508, 'type': 'Bot'},
             'unapproved-pr-opener'),
            ('automation application', {'login': 'sylphx-ai[bot]', 'id': 212550965, 'type': 'Bot'},
             'unapproved-pr-opener'),
            ('workflow token', {'login': 'github-actions[bot]', 'id': 41898282, 'type': 'Bot'},
             'unapproved-pr-opener'),
            ('login look-alike', {'login': 'shtse8', 'id': 1, 'type': 'User'}, 'unapproved-pr-opener'),
            ('id look-alike', {'login': 'example', 'id': 8020099, 'type': 'User'}, 'unapproved-pr-opener'),
        ]
        for label, user, refused in cases:
            with self.subTest(case=label):
                pr = pull()
                pr['user'] = user
                findings = checker.evaluate(pr, [commit()], POLICY)
                if refused:
                    self.assertEqual(findings, [{'rule': refused, 'location': 'pull-request.user', 'line': 0}])
                else:
                    self.assertEqual(findings, [])
        for user in (None, {'login': 'shtse8'}, {'login': 'shtse8', 'id': '8020099'}):
            with self.subTest(user=user):
                pr = pull()
                if user is None:
                    del pr['user']
                else:
                    pr['user'] = user
                with self.assertRaises(checker.PolicyError):
                    checker.evaluate(pr, [commit()], POLICY)
        self.assertEqual(checker.evaluate(None, [commit()], POLICY), [])

    def test_unapproved_opener_prints_the_remedy_at_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / 'snapshot.json'
            pr = pull()
            pr['user'] = {'login': 'sylphx-desk-services[bot]', 'id': 336710515, 'type': 'Bot'}
            snapshot.write_text(json.dumps({'pull_request': pr, 'commits': [commit()]}))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = checker.main(['--input', str(snapshot)])
            self.assertEqual(status, 1)
            self.assertIn('unapproved-pr-opener at pull-request.user:0', output.getvalue())
            self.assertIn('reopen this branch as an approved account', output.getvalue())
            self.assertNotIn('sylphx-desk-services', output.getvalue())

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

    def test_pull_permission_error_is_actionable_and_distinct_from_rate_or_content_failures(self):
        api = checker.GitHub('example/repo', 'read-token')
        for status, path, message, headers, expected in [
                (403, 'pulls/1', 'Resource not accessible by integration', {},
                 'caller workflow lacks pull-requests: read'),
                (403, 'pulls/1', 'Resource not accessible by integration', {'X-RateLimit-Remaining': '0'},
                 'HTTP 403: rate limit exceeded'),
                (403, 'pulls/1', 'API rate limit exceeded', {}, 'HTTP 403: rate limit exceeded'),
                (403, 'commits/' + HEAD, 'Resource not accessible by integration', {}, 'HTTP 403'),
                (403, 'pulls/1', 'Forbidden by organization policy', {}, 'HTTP 403'),
                (404, 'pulls/1', 'Not Found', {}, 'HTTP 404')]:
            with self.subTest(status=status, path=path, message=message):
                error = urllib.error.HTTPError('https://api.github.com/', status, 'forbidden', headers,
                                               io.BytesIO(json.dumps({'message': message}).encode()))
                self.addCleanup(error.close)
                with mock.patch.object(checker.urllib.request, 'urlopen', side_effect=error):
                    with self.assertRaises(checker.PolicyError) as caught:
                        api.get(path)
                self.assertIn(expected, str(caught.exception))
                self.assertNotIn('read-token', str(caught.exception))
                if 'rate limit' in expected or path.startswith('commits/'):
                    self.assertNotIn('caller workflow lacks', str(caught.exception))

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

    def test_verified_github_queue_author_allows_only_configured_human(self):
        event = {'merge_group': {'base_sha': BASE, 'head_sha': HEAD, 'base_ref': 'refs/heads/main'}}
        cases = [
            ('account email', 'Kyle Tse', 'shtse8@gmail.com', 'GitHub', 'noreply@github.com', 'Improve parser', None),
            ('default email', 'Kyle Tse', 'hi@kylet.se', 'GitHub', 'noreply@github.com', 'Improve parser', None),
            ('other email', 'Kyle Tse', 'other@example.com', 'GitHub', 'noreply@github.com', 'Improve parser', 'unapproved-author'),
            ('other name', 'Example', 'shtse8@gmail.com', 'GitHub', 'noreply@github.com', 'Improve parser', 'unapproved-author'),
            ('bot author', 'example[bot]', 'shtse8@gmail.com', 'GitHub', 'noreply@github.com', 'Improve parser', 'unapproved-author'),
            ('automation author', 'sylphx-ai[bot]', '212550965+sylphx-ai[bot]@users.noreply.github.com',
             'GitHub', 'noreply@github.com', 'Improve parser', 'unapproved-author'),
            ('human committer', 'Kyle Tse', 'shtse8@gmail.com', 'Kyle Tse', 'hi@kylet.se', 'Improve parser', 'unapproved-author'),
            ('wrong committer email', 'Kyle Tse', 'shtse8@gmail.com', 'GitHub', 'other@example.com', 'Improve parser', 'unapproved-author'),
            ('tool coauthor', 'Kyle Tse', 'shtse8@gmail.com', 'GitHub', 'noreply@github.com',
             'Improve parser\n\nCo-authored-by: Codex <codex@example.com>', 'tool-coauthor'),
        ]
        for label, name, email, committer, committer_email, message, refused in cases:
            with self.subTest(case=label):
                synthetic = commit(message=message)
                synthetic['commit']['author'] = {'name': name, 'email': email}
                synthetic['commit']['committer'] = {'name': committer, 'email': committer_email}
                api = mock.Mock(repository='example/repo', token='read-token')
                api.get.return_value = signed_github(synthetic)
                api.queue_entries.return_value = [{'headCommit': {'oid': HEAD},
                                                  'pullRequest': {'number': 1, 'headRefOid': 'c' * 40}}]
                with mock.patch.object(checker, 'range_commits', return_value=[synthetic]), \
                        mock.patch.object(checker, 'pull_snapshot', return_value=(pull(), [commit('c' * 40)])):
                    findings = checker.check_event(api, 'merge_group', event, HEAD, POLICY)
                if refused:
                    self.assertIn(refused, {row['rule'] for row in findings})
                else:
                    self.assertEqual(findings, [])

    def test_queue_email_never_exempts_pushed_or_unsigned_original_pr_commits(self):
        api = mock.Mock(repository='example/repo', token='read-token')
        api.queue_entries.return_value = [{'headCommit': {'oid': HEAD},
                                          'pullRequest': {'number': 1, 'headRefOid': 'c' * 40}}]
        synthetic = commit()
        synthetic['commit']['committer'] = {'name': 'GitHub', 'email': 'noreply@github.com'}
        for author in ({'name': 'Kyle Tse', 'email': 'shtse8@gmail.com'},
                       {'name': 'Example', 'email': 'hi@kylet.se'}):
            with self.subTest(author=author):
                original = commit('c' * 40)
                original['commit']['author'] = author
                api.get.side_effect = lambda path: signed_github(synthetic) if path.endswith(HEAD) else original
                event = {'merge_group': {'base_sha': BASE, 'head_sha': HEAD, 'base_ref': 'refs/heads/main'}}
                with mock.patch.object(checker, 'range_commits', return_value=[synthetic, original]), \
                        mock.patch.object(checker, 'pull_snapshot', return_value=(pull(), [original])):
                    findings = checker.check_event(api, 'merge_group', event, HEAD, POLICY)
                    self.assertIn('unapproved-author', {row['rule'] for row in findings})
                with mock.patch.object(checker, 'pull_snapshot', return_value=(pull(), [original])):
                    findings = checker.check_event(api, 'pull_request', {'number': 1, 'pull_request': pull()}, HEAD, POLICY)
                    self.assertIn('unapproved-author', {row['rule'] for row in findings})

    def test_github_email_on_unmapped_queue_commit_still_fails_membership(self):
        api = mock.Mock(repository='example/repo', token='read-token')
        api.queue_entries.return_value = []
        api.pages.return_value = []
        synthetic = commit()
        synthetic['commit']['author']['email'] = 'shtse8@gmail.com'
        synthetic['commit']['committer'] = {'name': 'GitHub', 'email': 'noreply@github.com'}
        api.get.return_value = signed_github(synthetic)
        event = {'merge_group': {'base_sha': BASE, 'head_sha': HEAD, 'base_ref': 'refs/heads/main'}}
        with mock.patch.object(checker, 'range_commits', return_value=[synthetic]):
            with self.assertRaisesRegex(checker.PolicyError, 'membership is incomplete'):
                checker.check_event(api, 'merge_group', event, HEAD, POLICY)

    def test_account_email_remains_valid_after_earlier_entry_merges(self):
        api = mock.Mock(repository='example/repo', token='read-token')
        api.queue_entries.return_value = []
        merged = pull()
        merged.update(state='closed', merged_at='2026-10-30T09:00:00Z', merge_commit_sha=HEAD)
        api.pages.return_value = [merged]
        synthetic = commit()
        synthetic['commit']['author']['email'] = 'shtse8@gmail.com'
        synthetic['commit']['committer'] = {'name': 'GitHub', 'email': 'noreply@github.com'}
        api.get.return_value = signed_github(synthetic)
        event = {'merge_group': {'base_sha': BASE, 'head_sha': HEAD, 'base_ref': 'refs/heads/main'}}
        with mock.patch.object(checker, 'range_commits', return_value=[synthetic]), \
                mock.patch.object(checker, 'pull_snapshot', return_value=(merged, [commit('c' * 40)])):
            self.assertEqual(checker.check_event(api, 'merge_group', event, HEAD, POLICY), [])

    def test_actual_verified_update_branch_commit_passes_in_original_pr_range(self):
        remote = json.loads((ROOT / 'tests/fixtures/github-update-commit.json').read_text())
        self.assertEqual(remote['sha'], '26dfb76cdb88cedecd598cf18ab4a438b6cb0902')
        original = {'sha': remote['sha'], 'commit': {key: remote['commit'][key]
                    for key in ('author', 'committer', 'message')}}
        verification = remote['commit']['verification']
        original['raw_commit'] = signed_object(verification['payload'], verification['signature'])
        api = mock.Mock(repository='example/repo', token='read-token')
        api.get.return_value = remote
        pr = pull()
        pr['head']['sha'] = original['sha']
        with mock.patch.object(checker, 'pull_snapshot', return_value=(pr, [original])):
            self.assertEqual(checker.check_event(api, 'pull_request', {'number': 1, 'pull_request': pr},
                                                original['sha'], POLICY), [])
        api.get.assert_called_once_with('commits/' + original['sha'])

    def test_unsigned_or_non_web_flow_lookalikes_fail_for_both_human_emails(self):
        for email in ('hi@kylet.se', 'shtse8@gmail.com'):
            original = commit()
            original['commit']['author']['email'] = email
            original['commit']['committer'] = {'name': 'GitHub', 'email': 'noreply@github.com'}
            mutations = [
                ('unsigned', lambda remote: remote['commit']['verification'].update(verified=False, reason='unsigned')),
                ('other account', lambda remote: remote['committer'].update(login='example')),
                ('missing account', lambda remote: remote.update(committer=None)),
                ('missing signature', lambda remote: remote['commit']['verification'].update(signature=None)),
                ('missing payload', lambda remote: remote['commit']['verification'].update(payload='')),
                ('invalid reason', lambda remote: remote['commit']['verification'].update(reason='unknown_key')),
            ]
            for label, mutate in mutations:
                with self.subTest(email=email, case=label):
                    remote = signed_github(original)
                    mutate(remote)
                    api = mock.Mock(repository='example/repo', token='read-token')
                    api.get.return_value = remote
                    with mock.patch.object(checker, 'pull_snapshot', return_value=(pull(), [original])):
                        findings = checker.check_event(api, 'pull_request', {'number': 1, 'pull_request': pull()}, HEAD, POLICY)
                    self.assertIn('unapproved-committer', {row['rule'] for row in findings})

    def test_signature_verdict_cannot_be_used_for_different_immutable_metadata(self):
        original = commit()
        original['commit']['author']['email'] = 'shtse8@gmail.com'
        original['commit']['committer'] = {'name': 'GitHub', 'email': 'noreply@github.com'}
        for key in ('sha', 'message', 'author', 'committer'):
            with self.subTest(field=key):
                remote = signed_github(original)
                if key == 'sha':
                    remote[key] = 'd' * 40
                elif key == 'message':
                    remote['commit']['verification']['payload'] += 'Different commit'
                else:
                    remote['commit'][key]['email'] = 'other@example.com'
                api = mock.Mock(repository='example/repo', token='read-token')
                api.get.return_value = remote
                with self.assertRaisesRegex(checker.PolicyError, 'does not match immutable'):
                    checker.verified_github_commits(api, [original], POLICY)

    def test_signed_payload_preserves_terminal_lf_omitted_from_display_message(self):
        item = commit(message='Merge pull request #1\n\nImprove parser\n')
        item['commit']['committer'] = {'name': 'GitHub', 'email': 'noreply@github.com'}
        remote = signed_github(item)
        remote['commit']['message'] = 'Merge pull request #1\n\nImprove parser'
        api = mock.Mock()
        api.get.return_value = remote
        self.assertEqual(checker.verified_github_commits(api, [item], POLICY), {HEAD})

    def test_signed_payload_rejects_every_other_message_byte_change(self):
        payload_message = 'Improve parser\n'
        cases = ['Improve parser', 'Improve parser\n\n', 'Improve parser\r\n',
                 'Improve parser \n', 'Different parser\n', 'Improve parser\n\nBody\n']
        for message in cases:
            with self.subTest(message=repr(message)):
                item = commit(message=payload_message)
                item['commit']['committer'] = {'name': 'GitHub', 'email': 'noreply@github.com'}
                remote = signed_github(item)
                item['raw_commit'] = item['raw_commit'].removesuffix(payload_message.encode()) + message.encode()
                api = mock.Mock()
                api.get.return_value = remote
                with self.assertRaisesRegex(checker.PolicyError, 'does not match immutable'):
                    checker.verified_github_commits(api, [item], POLICY)

    def test_signed_payload_binds_all_headers_not_only_projected_identities(self):
        item = commit()
        item['commit']['committer'] = {'name': 'GitHub', 'email': 'noreply@github.com'}
        remote = signed_github(item)
        for before, after in [(b'tree ' + BASE.encode(), b'tree ' + HEAD.encode()),
                              (b'parent ' + BASE.encode(), b'parent ' + HEAD.encode()),
                              (b'1000000000 +0000', b'1000000001 +0000'),
                              (b'author Kyle Tse', b'author Example'),
                              (b'committer GitHub', b'committer Example'),
                              (b'hi@kylet.se', b'other@example.com'),
                              (b'noreply@github.com', b'other@example.com')]:
            with self.subTest(field=before):
                changed = copy.deepcopy(item)
                changed['raw_commit'] = changed['raw_commit'].replace(before, after)
                api = mock.Mock()
                api.get.return_value = remote
                with self.assertRaisesRegex(checker.PolicyError, 'does not match immutable'):
                    checker.verified_github_commits(api, [changed], POLICY)

    def test_verified_web_edit_still_rejects_attribution_and_unapproved_author(self):
        for author, message, rule in [
                ({'name': 'Kyle Tse', 'email': 'shtse8@gmail.com'},
                 'Improve parser\n\nCo-authored-by: Codex <codex@example.com>', 'tool-coauthor'),
                ({'name': 'Example', 'email': 'shtse8@gmail.com'}, 'Improve parser', 'unapproved-author')]:
            with self.subTest(rule=rule):
                original = commit(message=message)
                original['commit']['author'] = author
                original['commit']['committer'] = {'name': 'GitHub', 'email': 'noreply@github.com'}
                api = mock.Mock(repository='example/repo', token='read-token')
                api.get.return_value = signed_github(original)
                with mock.patch.object(checker, 'pull_snapshot', return_value=(pull(), [original])):
                    findings = checker.check_event(api, 'pull_request', {'number': 1, 'pull_request': pull()}, HEAD, POLICY)
                self.assertIn(rule, {row['rule'] for row in findings})

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
            return {'data': {'repository': {'mergeQueue': {'entries': {'totalCount': 2, 'nodes': nodes,
                    'pageInfo': {'hasNextPage': more, 'endCursor': cursor}}}}}}
        with mock.patch.object(api, 'read', side_effect=[page([{'number': 1}], True, 'next'),
                                                       page([{'number': 2}], False, 'last')]) as read:
            self.assertEqual(len(api.queue_entries('main')), 2)
            self.assertEqual(read.call_args_list[1].args[1]['variables']['cursor'], 'next')
        with mock.patch.object(api, 'read', return_value={'errors': [{'message': 'denied'}]}):
            with self.assertRaises(checker.PolicyError):
                api.queue_entries('main')

    def test_queue_permission_errors_remain_distinct_from_rate_and_other_errors(self):
        api = checker.GitHub('example/repo', 'read-token')
        for error, expected in [
                ({'type': 'FORBIDDEN', 'message': 'Resource not accessible by integration'},
                 'caller workflow lacks pull-requests: read'),
                ({'type': 'RATE_LIMITED', 'message': 'Too many requests'}, 'rate limit exceeded'),
                ({'type': 'FORBIDDEN', 'message': 'Organization policy'}, 'metadata is unreadable')]:
            with self.subTest(error=error), mock.patch.object(api, 'read', return_value={'errors': [error]}):
                with self.assertRaises(checker.PolicyError) as caught:
                    api.queue_entries('main')
                self.assertIn(expected, str(caught.exception))

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
            self.assertEqual(commits[0]['commit']['message'], 'Change')
            self.assertTrue(commits[0]['raw_commit'].endswith(b'\n\nChange'))
            findings = checker.evaluate(None, commits, POLICY)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0]['location'], shas[150])

    def test_batch_object_acquisition_preserves_exact_messages(self):
        items = [commit(HEAD), commit(BASE)]
        raws = [b'tree ' + BASE.encode() + b'\n\nChange',
                b'tree ' + HEAD.encode() + '\n\nRésumé\n\n'.encode()]
        data = b''.join(item['sha'].encode() + b' commit ' + str(len(raw)).encode() + b'\n' + raw + b'\n'
                        for item, raw in zip(items, raws))
        checker.attach_commit_objects(items, data)
        self.assertEqual([item['raw_commit'] for item in items], raws)
        self.assertEqual([item['commit']['message'] for item in items], ['Change', 'Résumé\n\n'])

    def test_batch_object_acquisition_rejects_missing_or_misbound_bytes(self):
        raw = b'tree ' + BASE.encode() + b'\n\nChange'
        valid = HEAD.encode() + b' commit ' + str(len(raw)).encode() + b'\n' + raw + b'\n'
        for data in (b'', valid[:-1], valid + b'extra', valid.replace(HEAD.encode(), BASE.encode(), 1),
                     valid.replace(b' commit ', b' blob ', 1), valid.replace(b' commit ', b' commit -', 1)):
            with self.subTest(data=data), self.assertRaisesRegex(checker.PolicyError, 'commit object'):
                checker.attach_commit_objects([commit()], data)

    def test_signing_payload_removes_only_the_signature_header(self):
        raw = (b'tree ' + BASE.encode() + b'\ngpgsig signature\n continuation\n'
               b'encoding UTF-8\n\nMessage\ngpgsig body text\n continuation\n\n')
        expected = b'tree ' + BASE.encode() + b'\nencoding UTF-8\n\nMessage\ngpgsig body text\n continuation\n\n'
        self.assertEqual(checker.signing_payload(raw), expected)
        for missing in (None, '', b''):
            with self.subTest(missing=missing), self.assertRaisesRegex(checker.PolicyError, 'unavailable'):
                checker.signing_payload(missing)

    def test_both_verdicts_run_policy_without_waiver(self):
        for action in ('needs-pass', 'ci-ok'):
            doc = yaml.safe_load((ROOT / '.github/actions' / action / 'action.yml').read_text())
            step = next(s for s in doc['runs']['steps'] if 'contribution_policy.py' in s.get('run', ''))
            self.assertIn('contribution_policy.py', step['run'])
            self.assertNotIn('if', step)
            self.assertEqual(doc['inputs']['separate-pr-metadata']['default'], 'false')
            self.assertIn("&& 'commits' || 'all'", step['env']['CONTRIBUTION_POLICY_SCOPE'])
        document = yaml.safe_load((ROOT / 'workflow-templates/pr-metadata.yml').read_text())
        self.assertIn('edited', document[True]['pull_request']['types'])
        self.assertEqual(document['permissions']['pull-requests'], 'read')

    def test_pr_text_correction_recovers_without_replacing_code_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / 'snapshot.json'
            pr, item = pull(), commit()
            pr['body'] = 'Agent: example'
            def status(scope):
                snapshot.write_text(json.dumps({'pull_request': pr, 'commits': [item]}))
                with contextlib.redirect_stdout(io.StringIO()):
                    return checker.main(['--input', str(snapshot), '--scope', scope])
            self.assertEqual(status('commits'), 0)
            self.assertEqual(status('all'), 1)
            pr['body'] = 'Validated parser behavior.'
            self.assertEqual(status('all'), 0)
            self.assertEqual(status('commits'), 0)
            # Mutable text ownership never exempts immutable commit attribution.
            item['commit']['author'] = {'name': 'Example', 'email': 'example@example.com'}
            self.assertEqual(status('commits'), 1)

    def test_code_snapshot_ignores_text_changes_but_keeps_revision_checks(self):
        api = mock.Mock(repository='example/repo', token='read-token')
        before, after = pull(), pull()
        after['body'] = 'Changed while checking code'
        api.get.side_effect = [before, after]
        with mock.patch.object(checker, 'range_commits', return_value=[commit()]):
            self.assertEqual(checker.pull_snapshot(api, 1, HEAD, check_text=False), (after, [commit()]))
        after['head']['sha'] = 'c' * 40
        api.get.side_effect = [before, after]
        with mock.patch.object(checker, 'range_commits', return_value=[commit()]):
            with self.assertRaises(checker.PolicyError):
                checker.pull_snapshot(api, 1, HEAD, check_text=False)

    def test_base_dispatch_cannot_publish_evidence_for_a_different_head_or_merge(self):
        api = mock.Mock(repository='example/repo', token='read-token')
        event = {'inputs': {'code-head': HEAD, 'code-base': BASE, 'code-merge': 'c' * 40,
                            'code-pr': '1', 'code-base-ref': 'main'}}
        with self.assertRaises(checker.PolicyError):
            checker.check_event(api, 'workflow_dispatch', event, 'd' * 40, POLICY)
        api.get.return_value = {'parents': [{'sha': 'd' * 40}, {'sha': HEAD}]}
        with self.assertRaises(checker.PolicyError):
            checker.check_event(api, 'workflow_dispatch', event, HEAD, POLICY)
        api.get.return_value = {'parents': [{'sha': BASE}, {'sha': HEAD}]}
        api.get.side_effect = [api.get.return_value, pull(), pull(), pull()]
        api.pages.return_value = [pull()]
        with mock.patch.object(checker, 'range_commits', return_value=[commit()]):
            self.assertEqual(checker.check_event(api, 'workflow_dispatch', event, HEAD, POLICY), [])

    def test_base_dispatch_rejects_retarget_before_or_during_verdict(self):
        event = {'inputs': {'code-head': HEAD, 'code-base': BASE, 'code-merge': 'c' * 40,
                            'code-pr': '1', 'code-base-ref': 'main'}}
        parents = {'parents': [{'sha': BASE}, {'sha': HEAD}]}
        for field, value in [('sha', 'd' * 40), ('ref', 'release')]:
            changed = pull()
            changed['base'][field] = value
            for responses in ([parents, changed], [parents, pull(), pull(), changed]):
                api = mock.Mock(repository='example/repo', token='read-token')
                api.get.side_effect = responses
                with self.subTest(field=field, reads=len(responses)), \
                        mock.patch.object(checker, 'range_commits', return_value=[commit()]):
                    with self.assertRaises(checker.PolicyError):
                        checker.check_event(api, 'workflow_dispatch', event, HEAD, POLICY,
                                            check_pull_text=False)


class MetadataTransport(unittest.TestCase):
    """Fault the real transport and CLI without network requests or real sleeps."""

    def setUp(self):
        self.api = checker.GitHub('example/repo', 'read-token')
        self.sleep = self.enterContext(mock.patch.object(checker.time, 'sleep'))
        self.jitter = self.enterContext(mock.patch.object(checker.random, 'uniform', return_value=0))
        self.output = self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    @staticmethod
    def response(data):
        return io.BytesIO(json.dumps(data).encode())

    def failure(self, status=503, headers=None, message='untrusted-body read-token ::error::injected'):
        error = urllib.error.HTTPError('https://api.github.com/?read-token', status, 'read-token',
                                       headers or {}, self.response({'message': message}))
        self.addCleanup(error.close)
        return error

    def main(self, replies, commits=None):
        with tempfile.TemporaryDirectory() as directory:
            event, report = Path(directory) / 'event.json', Path(directory) / 'report.json'
            event.write_text(json.dumps({'number': 1, 'pull_request': pull()}))
            with mock.patch.dict(os.environ, {'GITHUB_EVENT_NAME': 'pull_request',
                    'GITHUB_EVENT_PATH': str(event), 'GITHUB_REPOSITORY': 'example/repo',
                    'GITHUB_SHA': HEAD, 'CONTRIBUTION_POLICY_TOKEN': 'read-token'}), \
                    mock.patch.object(checker.urllib.request, 'urlopen', side_effect=replies) as opened, \
                    mock.patch.object(checker, 'range_commits', return_value=commits if commits is not None else [commit()]):
                status = checker.main(['--report', str(report)])
            return status, json.loads(report.read_text()), opened

    def assert_safe_output(self, report=None):
        text = self.output.getvalue() + json.dumps(report)
        for forbidden in ('read-token', 'untrusted-body', '::error::injected', 'https://', 'Retry-After'):
            self.assertNotIn(forbidden, text)

    def test_repository_root_and_resource_routes_preserve_the_provider_contract(self):
        routes = (
            ('', 'https://api.github.com/repos/example/repo'),
            ('pulls/1', 'https://api.github.com/repos/example/repo/pulls/1'),
            ('commits/abc/check-runs?filter=all&page=2',
             'https://api.github.com/repos/example/repo/commits/abc/check-runs?filter=all&page=2'),
        )
        for path, expected_url in routes:
            with self.subTest(path=path), mock.patch.object(
                    checker.urllib.request, 'urlopen', return_value=self.response({'id': 1})) as opened:
                self.assertEqual(self.api.get(path), {'id': 1})
                request = opened.call_args.args[0]
                self.assertEqual(request.full_url, expected_url)
                self.assertEqual(request.get_method(), 'GET')
                self.assertEqual(request.get_header('Authorization'), 'Bearer read-token')
                self.assertIsNone(request.data)
        self.sleep.assert_not_called()

    def test_503_get_recovers_with_identical_request_and_closes_before_wait(self):
        error = self.failure()
        self.sleep.side_effect = lambda delay: self.assertTrue(error.closed)
        with mock.patch.object(checker.urllib.request, 'urlopen',
                               side_effect=[error, self.response(pull())]) as opened:
            self.assertEqual(self.api.get('pulls/1'), pull())
        self.assertEqual(opened.call_count, 2)
        first, second = (call.args[0] for call in opened.call_args_list)
        self.assertIs(first, second)
        self.assertEqual(first.get_method(), 'GET')
        self.assertEqual(first.full_url, 'https://api.github.com/repos/example/repo/pulls/1')
        self.assertEqual(first.get_header('Authorization'), 'Bearer read-token')
        self.assertEqual(first.get_header('X-github-api-version'), '2022-11-28')
        self.assertTrue(all(call.kwargs == {'timeout': 30} for call in opened.call_args_list))
        self.sleep.assert_called_once_with(1)
        self.assertIn('HTTP 503', self.output.getvalue())
        self.assertIn('attempt 2/3', self.output.getvalue())
        self.assert_safe_output()

    def test_two_503s_recover_and_never_restart_the_policy_snapshot(self):
        status, report, opened = self.main([self.failure(), self.failure(),
                                           self.response(pull()), self.response(pull())])
        self.assertEqual(status, 0)
        self.assertEqual(report, {'passed': True, 'findings': []})
        self.assertEqual(opened.call_count, 4)  # Three attempts before, one fresh read after.
        self.assertEqual(self.sleep.call_args_list, [mock.call(1), mock.call(2)])
        self.assert_safe_output(report)

    def test_exhausted_503s_keep_exit_two_and_failed_report(self):
        errors = [self.failure() for _ in range(3)]
        status, report, opened = self.main(errors)
        self.assertEqual(status, 2)
        self.assertFalse(report['passed'])
        self.assertEqual(report['error'], 'GitHub metadata read failed (HTTP 503)')
        self.assertEqual(opened.call_count, 3)
        self.assertEqual(self.sleep.call_args_list, [mock.call(1), mock.call(2)])
        self.assertTrue(all(error.closed for error in errors))
        self.assert_safe_output(report)

    def test_permanent_http_errors_fail_once_without_sleep(self):
        for status in (401, 403, 404, 422, 429, 500, 502, 504):
            with self.subTest(status=status):
                error = self.failure(status, {'Retry-After': '1'})
                with mock.patch.object(checker.urllib.request, 'urlopen', side_effect=error) as opened:
                    with self.assertRaises(checker.PolicyError):
                        self.api.get('pulls/1')
                opened.assert_called_once()
                self.assertTrue(error.closed)
        self.sleep.assert_not_called()
        self.assert_safe_output()

    def test_fatal_403_after_503_stops_recovery_and_keeps_permission_diagnostic(self):
        status, report, opened = self.main([self.failure(), self.failure(403,
                                           message='Resource not accessible by integration')])
        self.assertEqual(status, 2)
        self.assertFalse(report['passed'])
        self.assertEqual(report['error'], 'caller workflow lacks pull-requests: read')
        self.assertEqual(opened.call_count, 2)
        self.sleep.assert_called_once_with(1)
        self.assert_safe_output(report)

    def test_503_rate_indicators_are_not_retried(self):
        for headers, message in [({'X-RateLimit-Remaining': '0'}, 'unavailable'),
                                  ({}, 'API rate limit exceeded'), ({}, 'Secondary rate limit')]:
            with self.subTest(headers=headers, message=message):
                with mock.patch.object(checker.urllib.request, 'urlopen',
                                       side_effect=self.failure(headers=headers, message=message)) as opened:
                    with self.assertRaisesRegex(checker.PolicyError, 'HTTP 503'):
                        self.api.get('pulls/1')
                opened.assert_called_once()
        self.sleep.assert_not_called()

    def test_retry_after_minimum_and_jitter_are_bounded(self):
        for header, expected in [(None, 1.5), ('0', 1.5), ('5', 5), ('30', 30),
                ('Thu, 01 Jan 1970 00:16:45 GMT', 5), ('Thu, 01 Jan 1970 00:00:00 GMT', 1.5)]:
            with self.subTest(header=header):
                self.sleep.reset_mock()
                self.jitter.return_value = 0.5
                headers = {} if header is None else {'Retry-After': header}
                with mock.patch.object(checker.time, 'time', return_value=1000), \
                        mock.patch.object(checker.urllib.request, 'urlopen',
                                         side_effect=[self.failure(headers=headers), self.response(pull())]):
                    self.assertEqual(self.api.get('pulls/1'), pull())
                self.sleep.assert_called_once_with(expected)
        self.assertTrue(all(call == mock.call(0, 1) for call in self.jitter.call_args_list))
        self.assert_safe_output()

    def test_invalid_or_overbudget_retry_after_fails_closed_without_shortening_wait(self):
        for header in ('31', '999999999999999999999999', '-1', '1.5', '', 'read-token',
                       'Thu, 01 Jan 1970 00:17:11 GMT', 'Thu, 01 Jan 1970 00:16:45'):
            with self.subTest(header=header), mock.patch.object(checker.time, 'time', return_value=1000), \
                    mock.patch.object(checker.urllib.request, 'urlopen',
                                     side_effect=self.failure(headers={'Retry-After': header})) as opened:
                with self.assertRaisesRegex(checker.PolicyError, 'HTTP 503'):
                    self.api.get('pulls/1')
                opened.assert_called_once()
        self.sleep.assert_not_called()
        self.assert_safe_output()

    def test_graphql_post_503_and_transport_or_json_failures_are_not_retried(self):
        with mock.patch.object(checker.urllib.request, 'urlopen', side_effect=self.failure()) as opened:
            with self.assertRaisesRegex(checker.PolicyError, 'HTTP 503'):
                self.api.queue_entries('main')
        opened.assert_called_once()
        self.assertEqual(opened.call_args.args[0].get_method(), 'POST')
        for result in (OSError('read-token'), io.BytesIO(b'untrusted-body read-token')):
            with self.subTest(result=type(result).__name__), \
                    mock.patch.object(checker.urllib.request, 'urlopen', side_effect=[result]) as opened:
                with self.assertRaisesRegex(checker.PolicyError, 'metadata read failed'):
                    self.api.get('pulls/1')
                opened.assert_called_once()
        self.sleep.assert_not_called()
        self.assert_safe_output()

    def test_recovered_metadata_still_refuses_identity_text_and_incomplete_fields(self):
        for label in ('author', 'committer', 'commit-text', 'pull-text', 'incomplete'):
            with self.subTest(case=label):
                current, item = pull(), commit()
                if label in ('author', 'committer'):
                    item['commit'][label] = {'name': 'Example', 'email': 'example@example.com'}
                elif label == 'commit-text':
                    item['commit']['message'] = 'Co-authored-by: Codex <codex@example.com>'
                elif label == 'pull-text':
                    current['body'] = 'Agent: example'
                else:
                    del current['title']
                status, report, opened = self.main([self.failure(), self.response(current),
                                                   self.response(current)], [item])
                self.assertEqual(status, 2 if label == 'incomplete' else 1)
                self.assertFalse(report['passed'])
                self.assertEqual(opened.call_count, 3)
                self.assert_safe_output(report)

    def test_503_during_second_snapshot_read_cannot_hide_metadata_or_revision_changes(self):
        for field in ('title', 'body', 'head', 'base'):
            with self.subTest(field=field):
                changed = pull()
                if field == 'head':
                    changed[field]['sha'] = 'c' * 40
                elif field == 'base':
                    changed[field]['ref'] = 'release'
                else:
                    changed[field] = 'Changed while checking'
                status, report, opened = self.main([self.response(pull()), self.failure(), self.response(changed)])
                self.assertEqual(status, 2)
                self.assertFalse(report['passed'])
                self.assertIn('changed during', report['error'])
                self.assertEqual(opened.call_count, 3)
                self.assert_safe_output(report)

    def test_recovered_provenance_read_still_requires_matching_signed_commit(self):
        for label in ('unsigned', 'wrong-sha', 'wrong-message'):
            with self.subTest(case=label):
                item = commit()
                item['commit']['committer'] = {'name': 'GitHub', 'email': 'noreply@github.com'}
                remote = signed_github(item)
                if label == 'unsigned':
                    remote['commit']['verification']['verified'] = False
                elif label == 'wrong-sha':
                    remote['sha'] = 'c' * 40
                else:
                    remote['commit']['verification']['payload'] += 'Different commit'
                status, report, opened = self.main([self.response(pull()), self.response(pull()),
                                                   self.failure(), self.response(remote)], [item])
                self.assertEqual(status, 1 if label == 'unsigned' else 2)
                self.assertFalse(report['passed'])
                self.assertEqual(opened.call_count, 4)
                self.assert_safe_output(report)


if __name__ == '__main__':
    unittest.main()
