#!/usr/bin/env python3
"""Execute Broker action against fake wire replies; never claim live acceptance."""
import datetime
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / '.github/actions/broker-write-token/action.yml'
POLICY = 'orgs/example/trust_policies/source'
REPO = 'Example/source'
ISSUER = 'github-apps/1234'
CONNECTION = 'orgs/example/connections/github-tenant'
PERMISSIONS = {'contents': 'read', 'metadata': 'read'}


def action():
    return yaml.safe_load(ACTION.read_text())


CURL_STUB = r'''
import json, os, sys
args = sys.argv[1:]
config = sys.stdin.read()
with open(os.environ['WIRE'], 'a') as out:
    out.write(json.dumps({'args': args, 'config': config}) + '\n')
url = args[args.index('--url') + 1]
for fragment, value in json.load(open(os.environ['REPLIES'])).items():
    if fragment in url:
        status = value.get('_http', 200)
        body = dict(value)
        body.pop('_http', None)
        print(json.dumps(body))
        print(status, end='')
        sys.exit(0)
sys.exit(22)
'''


def env(**over):
    return {**dict(BROKER_POLICY=POLICY, REPO=REPO, API_URL='https://api.example', SOFT_FAIL='false',
                   READ_ONLY='false', PERMISSIONS='{}', ACTIONS_ID_TOKEN_REQUEST_URL='https://actions.example/token?job=123',
                   ACTIONS_ID_TOKEN_REQUEST_TOKEN='request-token'), **over}


def replies(**over):
    data = {
        'actions.example': {'value': 'oidc-jwt'},
        '/v1/access/github/token': {'api_key': 'syl_ci_key', 'active_after_ms': 0,
            'key': {'name': 'orgs/example/projects/prj_test/envs/env_test/api_keys/test',
                    'status': {'principal': 'orgs/example/principals/prn_test'}}},
        '/v1/whoami': {'org': 'orgs/example', 'principal': 'prn_test'},
        ':exchangeToken': grant(), ':exchangeReadToken': grant(),
        '/apps/tenant-app': {'id': 1234, 'slug': 'tenant-app'},
    }
    data.update(over)
    return data


def grant(**over):
    expiry = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=59)
    return {**dict(token='ghs_install', repositories=[REPO], permissions=PERMISSIONS,
                   issuer=ISSUER, connection=CONNECTION, expire_time=expiry.isoformat(),
                   token_exchange=POLICY + '/token_exchanges/tkx_example'), **over}


def run_script(data, extra):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / 'curl').write_text('#!/usr/bin/env python3\n' + CURL_STUB)
        (root / 'curl').chmod(0o700)
        (root / 'replies').write_text(json.dumps(data))
        (root / 'wire').touch()
        environment = {**os.environ, **extra, 'RUNNER_TEMP': directory,
            'GITHUB_ACTION_PATH': str(ACTION.parent), 'GITHUB_OUTPUT': str(root / 'out'),
            'REPLIES': str(root / 'replies'), 'WIRE': str(root / 'wire'), 'PATH': directory + ':' + os.environ['PATH']}
        result = subprocess.run(['bash', '-c', action()['runs']['steps'][0]['run']], capture_output=True, text=True, env=environment)
        output = (root / 'out').read_text() if (root / 'out').exists() else ''
        wire = [json.loads(line) for line in (root / 'wire').read_text().splitlines()]
        assert not (root / 'broker-token').exists(), 'provider token persisted by action'
        return result.returncode, result.stdout + result.stderr, output, wire


class ExchangeTests(unittest.TestCase):
    def test_complete_wire_and_metadata_without_secret_argv_or_file(self):
        code, log, output, wire = run_script(replies(), env(APP_SLUG='tenant-app'))
        self.assertEqual(code, 0, log)
        self.assertIn('token=ghs_install\n', output)
        self.assertIn('issuer=' + ISSUER, output)
        self.assertIn('connection=' + CONNECTION, output)
        self.assertIn('app-slug=tenant-app', output)
        self.assertEqual(len(wire), 5)
        self.assertIn('audience=sylphx-access', ' '.join(wire[0]['args']))
        self.assertIn(json.dumps({'repositories': [REPO]}), json.loads(wire[3]['config'].split('data = ')[1]))
        for item in wire:
            for secret in ('request-token', 'oidc-jwt', 'syl_ci_key', 'ghs_install'):
                self.assertNotIn(secret, ' '.join(item['args']))
        self.assertIn('::add-mask::ghs_install', log)

    def test_read_only_rpc_has_explicit_exact_permissions_and_one_repository(self):
        code, log, output, wire = run_script(replies(), env(READ_ONLY='true', PERMISSIONS=json.dumps(PERMISSIONS)))
        self.assertEqual(code, 0, log)
        self.assertIn(':exchangeReadToken', ' '.join(wire[3]['args']))
        body = json.loads(json.loads(wire[3]['config'].split('data = ')[1]))
        self.assertEqual(body, {'repositories': [REPO], 'permissions': PERMISSIONS})

    def test_invalid_read_grants_fail_before_issuance(self):
        for permissions in ({}, {'contents': 'write'}, [], {'contents': 'admin'}):
            with self.subTest(permissions=permissions):
                code, log, output, wire = run_script({}, env(READ_ONLY='true', PERMISSIONS=json.dumps(permissions)))
                self.assertEqual(code, 1, log)
                self.assertEqual(wire, [])
                self.assertNotIn('token=ghs_install', output)

    def test_missing_runtime_oidc_is_not_replaced_by_aliases(self):
        step = action()['runs']['steps'][0]
        self.assertNotIn('ACTIONS_ID_TOKEN_REQUEST_TOKEN', step['env'])
        for missing in ('ACTIONS_ID_TOKEN_REQUEST_TOKEN', 'ACTIONS_ID_TOKEN_REQUEST_URL'):
            code, log, output, wire = run_script({}, env(**{missing: '', 'OIDC_URL': 'https://actions.example', 'OIDC_TOKEN': 'alias'}))
            self.assertEqual(code, 1)
            self.assertIn('id-token: write', log)
            self.assertEqual(wire, [])

    def test_readiness_refusal_never_sends_broker_exchange(self):
        code, log, output, wire = run_script(replies(**{'/v1/whoami': {'_http': 403, 'code': 'PERMISSION_DENIED'}}), env())
        self.assertEqual(code, 1)
        self.assertIn('Access readiness refused', log)
        self.assertEqual(len(wire), 3)
        self.assertNotIn('token=ghs_install', output)

    def test_access_identity_mismatch_never_sends_broker_exchange(self):
        code, log, output, wire = run_script(replies(**{'/v1/whoami': {'org': 'orgs/foreign', 'principal': 'prn_test'}}), env())
        self.assertEqual(code, 1)
        self.assertEqual(len(wire), 3)
        self.assertIn('no Broker exchange was sent', log)

    def test_metadata_mismatch_and_overbroad_grants_fail_closed(self):
        for change in ({'repositories': [REPO, 'Example/other']}, {'permissions': {'contents': 'write'}},
                       {'issuer': 'github-apps/0'}, {'issuer': {}}, {'connection': 'orgs/foreign/connections/other'},
                       {'token_exchange': POLICY + '/token_exchanges/'}, {'expire_time': '2000-01-01T00:00:00Z'},
                       {'expire_time': '2099-01-01T00:00:00Z'}, {'expire_time': '2026-11-03T00:00:00'}):
            with self.subTest(change=change):
                code, log, output, wire = run_script(replies(**{':exchangeReadToken': grant(**change)}), env(READ_ONLY='true', PERMISSIONS=json.dumps(PERMISSIONS)))
                self.assertEqual(code, 1, log)
                self.assertNotIn('token=ghs_install', output)
                self.assertEqual(len(wire), 4)

    def test_pins_and_app_slug_are_checked_not_assumed(self):
        for extra in ({'EXPECTED_ISSUER': 'github-apps/987'}, {'EXPECTED_CONNECTION': 'orgs/example/connections/different'}, {'APP_SLUG': 'tenant-app'}):
            code, log, output, wire = run_script(replies(**{'/apps/tenant-app': {'id': 987, 'slug': 'tenant-app'}}), env(**extra))
            self.assertEqual(code, 1, log)
            self.assertNotIn('token=ghs_install', output)
        code, log, output, wire = run_script(replies(), env(EXPECTED_ISSUER=ISSUER, EXPECTED_CONNECTION=CONNECTION))
        self.assertEqual(code, 0, log)
        self.assertIn('app-slug=\n', output)  # No company-global identity default.

    def test_refused_write_is_never_retried_and_response_secrets_are_not_echoed(self):
        for soft_fail in ('true', 'false'):
            code, log, output, wire = run_script(replies(**{':exchangeToken': {'_http': 403, 'detail': 'private-provider-secret'}}), env(SOFT_FAIL=soft_fail))
            self.assertEqual(code, 0 if soft_fail == 'true' else 1)
            self.assertEqual(len(wire), 4)
            self.assertNotIn('private-provider-secret', log + output)
            self.assertIn('request was not retried', log)
            self.assertIn('token=\nfailure=', output)


if __name__ == '__main__':
    unittest.main()
