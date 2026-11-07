"""Exercise the real manual publisher, including duplicate/unknown outcome refusal."""
import copy
import importlib.util
import json
import os
import io
import urllib.error
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('operator_publisher', ROOT / 'scripts/operator_review_check.py')
publisher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publisher)
HEAD = 'a' * 40


class OperatorPublicationTest(unittest.TestCase):
    def setUp(self):
        self.api = mock.Mock(repository='SylphxAI/skills')
        self.created = {'id': 17, 'name': 'ops-security/review', 'app': {'id': 1299545},
                        'head_sha': HEAD, 'status': 'completed', 'conclusion': 'success', 'external_id': 'manual-test'}
        self.api.request.side_effect = [copy.deepcopy(self.created), copy.deepcopy(self.created)]

    def publish(self, directory, conclusion='success'):
        return publisher.publish_check(self.api, HEAD, conclusion, directory, 'Reviewed exact head.', 'manual-test')

    def test_canonical_exact_head_publication_is_read_back(self):
        with tempfile.TemporaryDirectory() as directory:
            receipt = self.publish(directory)
            self.assertEqual(receipt['check_run_id'], 17)
            self.assertEqual(receipt['app_id'], 1299545)
            self.assertEqual(receipt['head_sha'], HEAD)
            saved = json.loads((Path(directory) / 'publication-receipt.json').read_text())
            self.assertEqual(saved, receipt)
            self.assertEqual(self.api.request.call_args_list[0].args[:2], ('POST', 'check-runs'))
            self.assertEqual(self.api.request.call_args_list[1].args, ('GET', 'check-runs/17'))
            with self.assertRaisesRegex(publisher.PublicationError, 'already exists'):
                self.publish(directory)
            self.assertEqual(self.api.request.call_count, 2)

    def test_lost_post_response_is_not_replayed(self):
        self.api.request.side_effect = publisher.PublicationError('outcome unknown')
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(publisher.PublicationError, 'outcome unknown'):
                self.publish(directory)
            self.assertTrue((Path(directory) / 'publication-attempt.json').exists())
            with self.assertRaisesRegex(publisher.PublicationError, 'already exists'):
                self.publish(directory)
            self.api.request.assert_called_once()

    def test_wrong_app_head_or_readback_never_produces_a_receipt(self):
        for field, value in (('app', {'id': 15368}), ('head_sha', 'b' * 40), ('name', 'ci-ok'),
                             ('conclusion', 'failure'), ('external_id', 'someone-else')):
            for stage in (0, 1):
                with self.subTest(field=field, stage=stage), tempfile.TemporaryDirectory() as directory:
                    rows = [copy.deepcopy(self.created), copy.deepcopy(self.created)]
                    rows[stage][field] = value
                    self.api.request.side_effect = rows
                    with self.assertRaises(publisher.PublicationError):
                        self.publish(directory)
                    self.assertFalse((Path(directory) / 'publication-receipt.json').exists())

    def test_failure_is_an_explicit_app_decision_not_a_neutral_success(self):
        self.created['conclusion'] = 'failure'
        self.api.request.side_effect = [self.created, self.created]
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.publish(directory, 'failure')['conclusion'], 'failure')
        for conclusion in ('neutral', 'skipped', 'pending', None):
            with tempfile.TemporaryDirectory() as directory, self.assertRaises(publisher.PublicationError):
                self.publish(directory, conclusion)


class PublicationTransportTest(unittest.TestCase):
    def test_checks_only_post_uses_expected_origin_auth_and_single_request(self):
        api = publisher.AppGitHub('SylphxAI/.github', 'offline-token')
        with mock.patch.object(publisher.urllib.request, 'urlopen') as open_url:
            open_url.return_value.__enter__.return_value = io.BytesIO(b'{"id":17}')
            self.assertEqual(api.request('POST', 'check-runs', {'name': 'ops-security/review'}), {'id': 17})
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, 'https://api.github.com/repos/SylphxAI/.github/check-runs')
        self.assertEqual(request.method, 'POST')
        self.assertEqual(request.get_header('Authorization'), 'Bearer offline-token')
        self.assertEqual(json.loads(request.data), {'name': 'ops-security/review'})
        self.assertEqual(open_url.call_args.kwargs, {'timeout': 30})
        open_url.assert_called_once()

    def test_http_refusal_or_lost_response_is_not_retried_or_logged_with_token(self):
        api = publisher.AppGitHub('SylphxAI/.github', 'offline-token')
        errors = [(urllib.error.HTTPError('https://api.github.com', 403, 'denied', {}, None), 'HTTP 403'),
                  (OSError('offline-token lost response'), 'outcome unknown'),
                  (ValueError('offline-token invalid JSON'), 'outcome unknown')]
        for error, message in errors:
            with self.subTest(message=message), mock.patch.object(publisher.urllib.request, 'urlopen', side_effect=error) as call:
                with self.assertRaisesRegex(publisher.PublicationError, message) as caught:
                    api.request('POST', 'check-runs', {})
                self.assertNotIn('offline-token', str(caught.exception))
                call.assert_called_once()


class ManualStampTest(unittest.TestCase):
    def setUp(self):
        self.api = mock.Mock(repository='SylphxAI/skills')
        self.repository = {'id': 1427716146, 'full_name': 'SylphxAI/skills', 'visibility': 'public',
                           'private': False, 'default_branch': 'main'}
        self.pr = {'number': 585, 'state': 'open', 'merged_at': None, 'head': {'sha': HEAD},
                   'base': {'ref': 'main', 'repo': {'id': 1427716146}}}
        self.api.request.side_effect = lambda method, path: self.repository if path == '' else self.pr

    def test_operator_must_name_current_exact_public_main_pr_head(self):
        with mock.patch.object(publisher, 'publish_check', return_value={'check_run_id': 17}) as post:
            self.assertEqual(publisher.operator_stamp(self.api, self.api, 585, HEAD, 'success', 'receipt'), {'check_run_id': 17})
            self.assertEqual(post.call_args.args[1:4], (HEAD, 'success', 'receipt'))
            self.pr['head']['sha'] = 'b' * 40
            with self.assertRaisesRegex(publisher.PublicationError, 'current reviewed'):
                publisher.operator_stamp(self.api, self.api, 585, HEAD, 'success', 'receipt')
            post.assert_called_once()

    def test_private_recreated_wrong_base_and_closed_targets_refuse_without_post(self):
        cases = [('repository', 'visibility', 'private'), ('repository', 'id', 99),
                 ('repository', 'default_branch', 'other'), ('pr', 'state', 'closed')]
        for obj, key, value in cases:
            target = getattr(self, obj)
            saved = target[key]
            target[key] = value
            with self.subTest(obj=obj, key=key), mock.patch.object(publisher, 'publish_check') as post:
                with self.assertRaises(publisher.PublicationError):
                    publisher.operator_stamp(self.api, self.api, 585, HEAD, 'success', 'receipt')
                post.assert_not_called()
            target[key] = saved
        self.pr['base']['ref'] = 'feature'
        with self.assertRaises(publisher.PublicationError):
            publisher.operator_stamp(self.api, self.api, 585, HEAD, 'success', 'receipt')

    def test_reads_use_operator_metadata_token_not_the_checks_only_app_grant(self):
        app = mock.Mock(repository='SylphxAI/skills')
        with mock.patch.object(publisher, 'publish_check', return_value={}) as post:
            publisher.operator_stamp(app, self.api, 585, HEAD, 'success', 'receipt')
            self.assertEqual(post.call_args.args[0], app)
            self.assertEqual(self.api.request.call_args_list, [mock.call('GET', ''), mock.call('GET', 'pulls/585')])
            app.request.assert_not_called()
        self.api.repository = 'SylphxAI/.github'
        with self.assertRaisesRegex(publisher.PublicationError, 'matching metadata scope'):
            publisher.operator_stamp(app, self.api, 585, HEAD, 'success', 'receipt')

    def test_manual_interface_refuses_actions_instead_of_auto_approving_heads(self):
        argv = ['operator_review_check.py', '--repo', 'SylphxAI/skills', '--pr', '585', '--head', HEAD,
                '--conclusion', 'success', '--receipt-dir', 'receipt']
        with mock.patch('sys.argv', argv), mock.patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}), \
                mock.patch.object(publisher, 'AppGitHub') as api:
            with self.assertRaisesRegex(publisher.PublicationError, 'may not be issued by Actions'):
                publisher.main()
            api.assert_not_called()


if __name__ == '__main__':
    unittest.main()
