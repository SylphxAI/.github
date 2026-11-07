#!/usr/bin/env python3
"""Operator-only App Check Run publication; never run from a PR/candidate job."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request

APP_ID = 1299545
CHECK_NAME = 'ops-security/review'
REPOSITORIES = {'SylphxAI/.github': 1091169653, 'SylphxAI/skills': 1427716146}
SHA = re.compile(r'[0-9a-f]{40}\Z')


class PublicationError(Exception):
    pass


class AppGitHub:
    def __init__(self, repository, token):
        if repository not in REPOSITORIES or not token:
            raise PublicationError('an authorized repository and scoped App token are required')
        self.repository, self.token = repository, token

    def request(self, method, path, payload=None):
        request = urllib.request.Request('https://api.github.com/repos/' + self.repository + '/' + path,
            data=json.dumps(payload).encode() if payload is not None else None, method=method,
            headers={'Authorization': 'Bearer ' + self.token, 'Accept': 'application/vnd.github+json',
                     'Content-Type': 'application/json', 'X-GitHub-Api-Version': '2022-11-28'})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            raise PublicationError(f'GitHub {method} failed: HTTP {code}; request not retried') from None
        except (OSError, ValueError):
            raise PublicationError(f'GitHub {method} outcome unknown; reconcile before any new attempt') from None


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def publish_check(api, head, conclusion, receipt_dir, summary, external_id):
    """Publish once, with a durable pre-submit marker and authoritative readback.

    This is deliberately not a retry loop. A retained marker (including a lost
    POST response) requires GET-only reconciliation, not another POST.
    """
    if not isinstance(head, str) or not SHA.fullmatch(head) or conclusion not in {'success', 'failure'}:
        raise PublicationError('exact SHA and explicit success/failure required')
    directory = Path(receipt_dir)
    directory.mkdir(parents=True, exist_ok=True)
    payload = {'name': CHECK_NAME, 'head_sha': head, 'status': 'completed', 'conclusion': conclusion,
               'completed_at': utc_now(), 'external_id': external_id,
               'output': {'title': 'Operator approval ' + conclusion, 'summary': summary}}
    marker = directory / 'publication-attempt.json'
    try:
        with marker.open('x') as stream:
            json.dump({'repository': api.repository, 'method': 'POST', 'endpoint': 'check-runs',
                       'requested_at': utc_now(), 'request': payload}, stream)
    except FileExistsError:
        raise PublicationError('publication attempt already exists; reconcile without resubmitting') from None
    run = api.request('POST', 'check-runs', payload)
    (directory / 'publication-response.json').write_text(json.dumps(run))
    expected = {'name': CHECK_NAME, 'head_sha': head, 'status': 'completed', 'conclusion': conclusion,
                'external_id': external_id}
    if type(run.get('id')) is not int or run['id'] <= 0 or (run.get('app') or {}).get('id') != APP_ID or \
            any(run.get(key) != value for key, value in expected.items()):
        raise PublicationError('publication response does not identify the canonical App check')
    observed = api.request('GET', f"check-runs/{run['id']}")
    (directory / 'publication-readback.json').write_text(json.dumps(observed))
    if observed.get('id') != run['id'] or (observed.get('app') or {}).get('id') != APP_ID or \
            any(observed.get(key) != value for key, value in expected.items()):
        raise PublicationError('canonical App check publication readback mismatch')
    receipt = {'repository': api.repository, 'head_sha': head, 'check_run_id': run['id'],
               'app_id': APP_ID, 'conclusion': conclusion, 'verified_at': utc_now()}
    (directory / 'publication-receipt.json').write_text(json.dumps(receipt))
    return receipt


def operator_stamp(api, metadata, number, head, conclusion, receipt_dir):
    """Manual operator interface: current open PR, exact reviewed head, public main only."""
    if api.repository != metadata.repository or type(number) is not int or number <= 0:
        raise PublicationError('matching metadata scope and a positive PR number are required')
    repository = metadata.request('GET', '')
    if repository.get('id') != REPOSITORIES[api.repository] or repository.get('full_name') != api.repository or \
            repository.get('visibility') != 'public' or repository.get('private') is not False or \
            repository.get('default_branch') != 'main':
        raise PublicationError('not the declared public repository')
    pr = metadata.request('GET', f'pulls/{number}')
    if pr.get('number') != number or pr.get('state') != 'open' or pr.get('merged_at') or \
            pr.get('head', {}).get('sha') != head or pr.get('base', {}).get('ref') != 'main' or \
            pr.get('base', {}).get('repo', {}).get('id') != repository['id']:
        raise PublicationError('PR is not the current reviewed public main head')
    return publish_check(api, head, conclusion, receipt_dir,
                         f'Operator decision for {api.repository} PR #{number}, exact reviewed head `{head}`.',
                         f'operator/{api.repository}/{number}/{head}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', choices=REPOSITORIES, required=True)
    parser.add_argument('--pr', type=int, required=True)
    parser.add_argument('--head', required=True)
    parser.add_argument('--conclusion', choices=('success', 'failure'), required=True)
    parser.add_argument('--receipt-dir', required=True)
    args = parser.parse_args()
    # Only an operator-owned, short-lived Broker grant supplies this variable.
    # No App key or repository secret is accepted, and no token enters argv/logs.
    if os.environ.get('GITHUB_ACTIONS'):
        raise PublicationError('manual operator approvals may not be issued by Actions')
    api = AppGitHub(args.repo, os.environ.get('OPS_SECURITY_APP_TOKEN', ''))
    # Keep source reads separate: a checks-only App grant cannot read PR metadata.
    metadata = AppGitHub(args.repo, os.environ.get('GH_TOKEN', ''))
    print(json.dumps(operator_stamp(api, metadata, args.pr, args.head, args.conclusion, args.receipt_dir)))


if __name__ == '__main__':
    try:
        main()
    except (PublicationError, ValueError, TypeError, KeyError) as error:
        raise SystemExit(str(error) if isinstance(error, PublicationError) else 'operator publication metadata is invalid')
