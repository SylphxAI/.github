#!/usr/bin/env python3
"""One-shot OIDC -> Access -> Broker exchange. No provider-write retries."""
import datetime
import json
import os
import re
import subprocess
from urllib.parse import urlsplit

from access_ready import issued_subject, wait_for_identity, read_identity, ReadinessError


class ExchangeError(Exception):
    pass


def mask(value):
    # GitHub workflow-command escaping: a credential cannot inject a command.
    print('::add-mask::' + value.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A'), flush=True)


def request(url, *, token=None, body=None):
    """Secrets travel only on stdin, never argv, URL, stderr or retry logs."""
    config = []
    if token:
        config.append('header = ' + json.dumps('Authorization: Bearer ' + token))
    args = ['curl', '-sS', '--max-time', '60', '--url', url, '-K', '-', '--write-out', '\n%{http_code}']
    if body is not None:
        args += ['--request', 'POST']
        config += ['header = "content-type: application/json"', 'data = ' + json.dumps(json.dumps(body))]
    result = subprocess.run(args, input='\n'.join(config) + '\n', text=True, capture_output=True, timeout=65)
    if result.returncode:
        raise ExchangeError('credential exchange failed at the network; request was not retried')
    try:
        raw, status = result.stdout.rsplit('\n', 1)
        data = json.loads(raw)
        if int(status) != 200 or not isinstance(data, dict):
            raise ValueError()
        return data
    except (ValueError, TypeError):
        # Provider error bodies can reflect credentials; do not print them.
        raise ExchangeError('credential endpoint refused or returned invalid JSON; request was not retried') from None


def validate(api_url, policy, repository, permissions, read_only):
    url = urlsplit(api_url)
    if url.scheme != 'https' or not url.netloc or url.username or url.password or url.query or url.fragment:
        raise ExchangeError('ordinary HTTPS API endpoint required')
    if not isinstance(policy, str) or not re.fullmatch(r'orgs/[A-Za-z0-9_-]+/trust_policies/[A-Za-z0-9_-]+', policy):
        raise ExchangeError('invalid trust-policy resource')
    if not isinstance(repository, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository) or any(p in ('.', '..') for p in repository.split('/')):
        raise ExchangeError('one owner/name repository required')
    if not isinstance(permissions, dict) or any(not isinstance(k, str) or not re.fullmatch(r'[a-z][a-z_]*', k) or v not in ('read', 'write') for k, v in permissions.items()):
        raise ExchangeError('permissions must be a JSON map of provider permission names to read/write')
    if read_only and (not permissions or any(v != 'read' for v in permissions.values())):
        raise ExchangeError('read-only exchange requires explicit nonempty read permissions')


def access(api_url, ci_name):
    url = os.environ.get('ACTIONS_ID_TOKEN_REQUEST_URL', '')
    token = os.environ.get('ACTIONS_ID_TOKEN_REQUEST_TOKEN', '')
    if not url or not token:
        raise ExchangeError('no Actions OIDC request URL/token: caller must grant id-token: write')
    oidc = request(url + ('&' if '?' in url else '?') + 'audience=sylphx-access', token=token).get('value')
    if not isinstance(oidc, str) or not oidc:
        raise ExchangeError('GitHub OIDC request returned no token; id-token: write is required')
    mask(oidc)
    issued = request(api_url.rstrip('/') + '/v1/access/github/token', body={'name': ci_name, 'token': oidc})
    key = issued.get('api_key')
    if not isinstance(key, str) or not key or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise ExchangeError('no Access key: check exact [[ci]] declaration and signed workflow authority')
    mask(key)
    org, principal = issued_subject(issued)
    identity = wait_for_identity(read_identity, key, issued.get('active_after_ms'), api_url)
    if identity.get('org') != org or org + '/principals/' + identity.get('principal', '') != principal:
        raise ExchangeError('issued Access key identity differs from the ready identity; no Broker exchange was sent')
    return key


def exchange(api_url, policy, repository, permissions, read_only=False, ci_name='', app_slug='', key=None, expected_issuer='', expected_connection=''):
    validate(api_url, policy, repository, permissions, read_only)
    if not key:
        key = access(api_url, ci_name or policy.rsplit('/', 1)[1])
    method = 'exchangeReadToken' if read_only else 'exchangeToken'
    body = {'repositories': [repository]}
    if permissions:
        body['permissions'] = permissions
    result = request(api_url.rstrip('/') + '/v1/' + policy + ':' + method, token=key, body=body)
    token = result.get('token')
    if not isinstance(token, str) or not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise ExchangeError('no installation token from Broker')
    mask(token)
    if any(not isinstance(result.get(field), str) for field in ('issuer', 'connection', 'token_exchange', 'expire_time')):
        raise ExchangeError('Broker credential metadata is absent or malformed')
    if (result.get('repositories') != [repository]
            or not isinstance(result.get('permissions'), dict)
            or not result['permissions']
            or (permissions and result['permissions'] != permissions)
            or (read_only and any(v != 'read' for v in result['permissions'].values()))
            or not re.fullmatch(r'github-apps/[1-9][0-9]*', result.get('issuer', ''))
            or not re.fullmatch(re.escape(policy) + r'/token_exchanges/[A-Za-z0-9_-]+', result['token_exchange'])
            or not re.fullmatch(re.escape(policy.split('/trust_policies/')[0]) + r'/connections/[A-Za-z0-9_-]+', result['connection'])):
        raise ExchangeError('Broker credential metadata does not match the exact requested grant')
    if (expected_issuer and result['issuer'] != expected_issuer) or (expected_connection and result['connection'] != expected_connection):
        raise ExchangeError('Broker issuer or Connection differs from the caller pin')
    try:
        expiry = datetime.datetime.fromisoformat(result['expire_time'].replace('Z', '+00:00'))
        ttl = (expiry - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
        if not 0 < ttl <= 3660:
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise ExchangeError('Broker credential expiry is absent or invalid') from None
    if app_slug:
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]*', app_slug):
            raise ExchangeError('invalid app-slug')
        app = request('https://api.github.com/apps/' + app_slug)
        if app.get('slug') != app_slug or str(app.get('id')) != result['issuer'].split('/')[1]:
            raise ExchangeError('App slug does not match the Broker issuer')
    result['app-slug'] = app_slug
    return result
