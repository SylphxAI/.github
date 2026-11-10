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


def action(path=ACTION):
    return yaml.safe_load(path.read_text())


CURL_STUB = r'''
import json, os, sys
args = sys.argv[1:]
config = sys.stdin.read()
with open(os.environ['WIRE'], 'a') as out:
    out.write(json.dumps({'args': args, 'config': config}) + '\n')
url = args[args.index('--url') + 1]
replies_data = json.load(open(os.environ['REPLIES']))
for fragment, value in list(replies_data.items()):
    if fragment in url:
        if isinstance(value, list):
            current = value.pop(0) if len(value) > 1 else value[0]
            json.dump(replies_data, open(os.environ['REPLIES'], 'w'))
            value = current
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


CLOCK_STUB = r'''
import atexit, importlib, importlib.util, json, os, subprocess, sys, types
sys.path.insert(0, os.environ['BROKER_SHARED'])
now = 0.0
def sleep(seconds):
    global now
    assert seconds >= 0
    now += seconds
    with open(os.environ['SLEEPS'], 'a') as out:
        out.write(json.dumps(seconds) + '\n')
def configure():
    # Keep process supervision on real time; charge controlled request cost only
    # to the action's clock. Curl fixture children do not install this hook.
    clock = types.SimpleNamespace(sleep=sleep, monotonic=lambda: now)
    for name in ('access_ready', 'broker'):
        if importlib.util.find_spec(name):
            importlib.import_module(name).time = clock
    costs = json.loads(os.environ['REQUEST_COSTS'])
    real_run = subprocess.run
    def run(args, *positional, **kwargs):
        global now
        result = real_run(args, *positional, **kwargs)
        url = args[args.index('--url') + 1]
        seconds = next((value for fragment, value in costs.items() if fragment in url),
                       costs.get('*', 0))
        assert 0 <= seconds <= kwargs['timeout']
        assert seconds <= float(args[args.index('--max-time') + 1])
        now += seconds
        return result
    subprocess.run = run
    def record_elapsed():
        with open(os.environ['ELAPSED'], 'w') as out:
            json.dump(now, out)
    atexit.register(record_elapsed)
if sys.argv[0].endswith('/exchange.py'):
    configure()
'''


def run_script(data, extra, *, action_path=ACTION, sleeps=None, elapsed=None, request_costs=None):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / 'curl').write_text('#!/usr/bin/env python3\n' + CURL_STUB)
        (root / 'curl').chmod(0o700)
        (root / 'replies').write_text(json.dumps(data))
        (root / 'wire').touch()
        environment = {**os.environ, **extra, 'RUNNER_TEMP': directory,
            'GITHUB_ACTION_PATH': str(action_path.parent), 'GITHUB_OUTPUT': str(root / 'out'),
            'REPLIES': str(root / 'replies'), 'WIRE': str(root / 'wire'), 'PATH': directory + ':' + os.environ['PATH']}
        if sleeps is not None:
            (root / 'sitecustomize.py').write_text(CLOCK_STUB)
            environment.update(PYTHONPATH=directory + os.pathsep + environment.get('PYTHONPATH', ''),
                               SLEEPS=str(root / 'sleeps'),
                               BROKER_SHARED=str(action_path.parent.parent / '_shared'),
                               ELAPSED=str(root / 'elapsed'), REQUEST_COSTS=json.dumps(request_costs or {}))
        result = subprocess.run(['bash', '-c', action(action_path)['runs']['steps'][0]['run']],
                                capture_output=True, text=True, env=environment, timeout=30)
        if sleeps is not None and (root / 'sleeps').exists():
            sleeps.extend(json.loads(line) for line in (root / 'sleeps').read_text().splitlines())
        if elapsed is not None:
            elapsed.append(json.loads((root / 'elapsed').read_text()))
        output = (root / 'out').read_text() if (root / 'out').exists() else ''
        wire = [json.loads(line) for line in (root / 'wire').read_text().splitlines()]
        assert not (root / 'broker-token').exists(), 'provider token persisted by action'
        return result.returncode, result.stdout + result.stderr, output, wire


def pinned_self_actions():
    """Resolve real declarative callers and execute their immutable action bundle."""
    prefix = 'SylphxAI/.github/.github/actions/broker-write-token@'
    callers = {}

    def visit(value):
        if isinstance(value, dict):
            use = value.get('uses', '')
            if isinstance(use, str) and use.startswith(prefix):
                pin = use[len(prefix):]
                if len(pin) != 40 or any(c not in '0123456789abcdef' for c in pin):
                    raise AssertionError('full commit SHA required: ' + use)
                callers.setdefault(pin, []).append(value)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    tracked = subprocess.check_output(['git', 'ls-files', '-z', '--', '*.yml', '*.yaml'], cwd=ROOT)
    for path in tracked.split(b'\0'):
        if path:
            visit(yaml.safe_load((ROOT / os.fsdecode(path)).read_text()))
    if not callers:
        raise AssertionError('no Broker action self-references found')
    with tempfile.TemporaryDirectory() as directory:
        for pin, steps in callers.items():
            bundle = Path(directory) / pin
            paths = subprocess.check_output(['git', 'ls-tree', '-rz', '--name-only', pin, '--',
                '.github/actions/broker-write-token', '.github/actions/_shared'], cwd=ROOT)
            for raw in paths.split(b'\0'):
                if raw:
                    path = os.fsdecode(raw)
                    target = bundle / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(subprocess.check_output(['git', 'show', pin + ':' + path], cwd=ROOT))
            yield bundle / '.github/actions/broker-write-token/action.yml', steps


class ExchangeTests(unittest.TestCase):
    def test_pinned_self_references_execute_bounded_typed_401_contract(self):
        lagging = {'_http': 401, 'code': 'NOT_YET_PROPAGATED', 'detail': 'private-propagation-detail'}
        refused = {'_http': 401, 'code': 'UNAUTHENTICATED', 'detail': 'authentication refused'}
        for pinned_action, callers in pinned_self_actions():
            declared = action(pinned_action)['inputs']
            for caller in callers:
                inputs = caller.get('with', {})
                self.assertLessEqual(set(inputs), set(declared))
                self.assertTrue(all(not value.get('required') or key in inputs or 'default' in value
                                    for key, value in declared.items()))
            for soft_fail in {caller.get('with', {}).get('soft-fail', 'false') for caller in callers}:
                for endpoint in ('/v1/whoami', ':exchangeToken'):
                    success = replies()[endpoint]
                    for name, sequence, accepted, waits, requests in (
                        ('ready-success', success, True, [0], 4),
                        ('typed-401-then-success', [lagging, success], True, [0, 2], 5),
                        ('other-401-then-success-is-not-retried', [refused, success], False, [0],
                         3 if endpoint == '/v1/whoami' else 4),
                        ('typed-401-deadline', lagging, False, [0] + [2] * 90,
                         92 if endpoint == '/v1/whoami' else 93),
                    ):
                        with self.subTest(pin=pinned_action.parents[3].name, endpoint=endpoint,
                                          case=name, soft_fail=soft_fail):
                            sleeps = []
                            code, log, output, wire = run_script(replies(**{endpoint: sequence}),
                                env(SOFT_FAIL=soft_fail), action_path=pinned_action, sleeps=sleeps)
                            self.assertEqual(code, 0 if accepted or soft_fail == 'true' else 1, log)
                            self.assertEqual('token=ghs_install\n' in output, accepted, log)
                            self.assertEqual(sleeps, waits)
                            self.assertEqual(len(wire), requests)
                            # Exactly one issuance, with the same key retained for every later attempt.
                            urls = [item['args'][item['args'].index('--url') + 1] for item in wire]
                            self.assertEqual(sum('/v1/access/github/token' in url for url in urls), 1)
                            for item in wire[2:]:
                                self.assertIn('Bearer syl_ci_key', item['config'])
                            if not accepted:
                                self.assertIn('token=\nfailure=', output)
                                if endpoint == '/v1/whoami':
                                    self.assertIn('HTTP 401' if name.startswith('other-401')
                                                  else 'did not become ready within 180 seconds', output)
                                    if name.startswith('other-401'):
                                        self.assertIn('UNAUTHENTICATED', output)
                                else:
                                    self.assertIn('request was not retried', output)
                            for secret in ('private-propagation-detail',):
                                self.assertNotIn(secret, log + output)

    def test_joint_propagation_stages_complete_or_report_failure_inside_caller_budget(self):
        lagging = {'_http': 401, 'code': 'NOT_YET_PROPAGATED'}
        for pinned_action, callers in pinned_self_actions():
            for soft_fail in {caller.get('with', {}).get('soft-fail', 'false') for caller in callers}:
                for name, accepted, seconds in (('both-stages-recover', True, 537.8),
                                                ('second-stage-exhausts', False, 538.9)):
                    with self.subTest(pin=pinned_action.parents[3].name, case=name, soft_fail=soft_fail):
                        data = replies()
                        data['/v1/access/github/token']['active_after_ms'] = 60000
                        data['/v1/whoami'] = [lagging] * 89 + [data['/v1/whoami']]
                        data[':exchangeToken'] = [lagging] * 89 + [data[':exchangeToken']] if accepted else lagging
                        sleeps, elapsed = [], []
                        code, log, output, wire = run_script(data, env(SOFT_FAIL=soft_fail),
                            action_path=pinned_action, sleeps=sleeps, elapsed=elapsed,
                            request_costs={'actions.example': 60, '/v1/access/github/token': 60, '*': 0.01})
                        self.assertEqual(code, 0 if accepted or soft_fail == 'true' else 1, log)
                        self.assertEqual('token=ghs_install\n' in output, accepted, log)
                        if not accepted:
                            self.assertIn('token=\nfailure=credential endpoint refused', output)
                        self.assertEqual(sleeps[0], 60)
                        self.assertEqual(sleeps[1:90], [2] * 89)
                        if accepted:
                            self.assertEqual(sleeps[90:], [2] * 89)
                        self.assertAlmostEqual(elapsed[0], seconds, places=5)
                        urls = [item['args'][item['args'].index('--url') + 1] for item in wire]
                        self.assertEqual(len(wire), 182)
                        for endpoint, count in (('actions.example', 1), ('/v1/access/github/token', 1),
                                                ('/v1/whoami', 90), (':exchangeToken', 90)):
                            self.assertEqual(sum(endpoint in url for url in urls), count)
                        for item in wire[2:]:
                            self.assertIn('Bearer syl_ci_key', item['config'])
                        for caller in callers:
                            budget = caller['timeout-minutes'] * 60
                            self.assertLess(elapsed[0], budget)
                            self.assertGreaterEqual(budget, 60 + 2 * 180 + 2 * 65 + 6)

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

    def test_explicit_permissions_allow_only_implicit_metadata_read(self):
        requested = {'contents': 'read', 'actions': 'read'}
        for read_only, endpoint in (('false', ':exchangeToken'), ('true', ':exchangeReadToken')):
            for issued in (requested, {**requested, 'metadata': 'read'}):
                with self.subTest(read_only=read_only, issued=issued):
                    code, log, output, wire = run_script(
                        replies(**{endpoint: grant(permissions=issued)}),
                        env(READ_ONLY=read_only, PERMISSIONS=json.dumps(requested)))
                    self.assertEqual(code, 0, log)
                    self.assertIn('token=ghs_install\n', output)
                    self.assertIn('permissions=' + json.dumps(issued, separators=(',', ':')), output)
                    body = json.loads(json.loads(wire[3]['config'].split('data = ')[1]))
                    self.assertEqual(body, {'repositories': [REPO], 'permissions': requested})
                    self.assertEqual(len(wire), 4)
            for issued in (
                {**requested, 'checks': 'read'},
                {**requested, 'metadata': 'write'},
                {**requested, 'metadata': 'admin'},
                {**requested, 'actions': 'write'},
                {**requested, 'contents': 'admin'},
                {'contents': 'read', 'metadata': 'read'},
                {}, None,
            ):
                with self.subTest(read_only=read_only, issued=issued):
                    code, log, output, wire = run_script(
                        replies(**{endpoint: grant(permissions=issued)}),
                        env(READ_ONLY=read_only, PERMISSIONS=json.dumps(requested)))
                    self.assertEqual(code, 1, log)
                    self.assertNotIn('token=ghs_install', output)
                    self.assertIn('token=\nfailure=', output)
                    self.assertEqual(len(wire), 4)

    def test_explicit_metadata_permission_stays_exact(self):
        requested = {'contents': 'write', 'metadata': 'write'}
        code, log, output, _ = run_script(
            replies(**{':exchangeToken': grant(permissions={**requested, 'metadata': 'read'})}),
            env(PERMISSIONS=json.dumps(requested)))
        self.assertEqual(code, 1, log)
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
        for refusal in (
            {'_http': 403, 'detail': 'private-provider-secret'},
            {'_http': 401, 'code': 'invalid_api_key', 'detail': 'private-provider-secret'},
            {'_http': 401, 'detail': 'private-provider-secret'},
        ):
            for soft_fail in ('true', 'false'):
                with self.subTest(refusal=refusal, soft_fail=soft_fail):
                    code, log, output, wire = run_script(replies(**{':exchangeToken': refusal}), env(SOFT_FAIL=soft_fail))
                    self.assertEqual(code, 0 if soft_fail == 'true' else 1)
                    self.assertEqual(len(wire), 4)
                    self.assertNotIn('private-provider-secret', log + output)
                    self.assertIn('request was not retried', log)
                    self.assertIn('token=\nfailure=', output)

    def test_broker_exchange_retries_only_key_not_yet_propagated_within_margin(self):
        for prop_code in ('key_not_yet_propagated', 'NOT_YET_PROPAGATED'):
            lagging = {'_http': 401, 'code': prop_code, 'detail': 'private-propagation-detail'}
            code, log, output, wire = run_script(
                replies(**{':exchangeToken': [lagging, grant()]}),
                env(),
            )
            self.assertEqual(code, 0, log)
            self.assertIn('token=ghs_install\n', output)
            self.assertEqual(len(wire), 5)
            self.assertNotIn('private-propagation-detail', log + output)
            code, log, output, wire = run_script(
                replies(**{':exchangeReadToken': [lagging, grant()]}),
                env(READ_ONLY='true', PERMISSIONS=json.dumps(PERMISSIONS)),
            )
            self.assertEqual(code, 0, log)
            self.assertIn('token=ghs_install\n', output)
            self.assertEqual(len(wire), 5)
            self.assertNotIn('private-propagation-detail', log + output)


if __name__ == '__main__':
    unittest.main()
