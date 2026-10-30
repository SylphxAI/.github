#!/usr/bin/env python3
"""Publish metadata evidence for an exact PR head from trusted default code."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.request

POLICY_PATH = Path(__file__).resolve().parent.parent / 'contribution-policy/contribution_policy.py'
spec = importlib.util.spec_from_file_location('metadata_policy', POLICY_PATH)
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)
CHECK_NAME = 'pr-metadata'
ACTIONS_APP = 15368


class GitHub(policy.GitHub):
    def write(self, method, path, payload):
        request = urllib.request.Request(self.root + '/repos/' + self.repository + '/' + path,
            method=method, data=json.dumps(payload).encode(),
            headers={'Authorization': 'Bearer ' + self.token, 'Accept': 'application/vnd.github+json',
                     'Content-Type': 'application/json', 'X-GitHub-Api-Version': '2022-11-28'})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.status != (201 if method == 'POST' else 200):
                    raise policy.PolicyError('metadata check write was not accepted')
                return json.load(response)
        except (urllib.error.URLError, OSError, ValueError):
            # A write with an unknown result is never retried.
            raise policy.PolicyError('metadata check write result is unknown; inspect its check before another dispatch') from None


def checks(api, head):
    page, result = 1, []
    while True:
        response = api.get(f'commits/{head}/check-runs?filter=latest&per_page=100&page={page}')
        rows = response['check_runs']
        result.extend(row for row in rows if row.get('head_sha') == head and
                      row.get('name') == CHECK_NAME and (row.get('app') or {}).get('id') == ACTIONS_APP)
        if len(rows) < 100:
            return result
        page += 1


def publish(api, number, head, run_id, policy_data, *, missing_only=False):
    if not isinstance(number, int) or isinstance(number, bool) or number < 1 or not policy.SHA.fullmatch(head):
        raise policy.PolicyError('metadata target requires a PR number and exact head SHA')
    current = api.get(f'pulls/{number}')
    if missing_only:
        if current.get('state') != 'open':
            return {'pr': number, 'head': head, 'status': 'closed'}
        head = current['head']['sha']
        if not policy.SHA.fullmatch(head):
            raise policy.PolicyError('current PR head is not immutable')
    if current.get('state') != 'open' or current['head']['sha'] != head:
        raise policy.PolicyError('metadata target changed; no check was published')
    previous = checks(api, head)
    if missing_only and previous:
        return {'pr': number, 'head': head, 'status': 'producer-present'}
    external_id = f'pr-metadata-backfill/{run_id}/{number}/{head}'
    if any(row.get('external_id') == external_id for row in previous):
        return {'pr': number, 'head': head, 'status': 'already-published'}
    created = api.write('POST', 'check-runs', {
        'name': CHECK_NAME, 'head_sha': head, 'status': 'in_progress', 'external_id': external_id})
    check_id = created['id']
    # Every accepted check gets a terminal result even when acquisition fails.
    # The snapshot fetches Git objects without checking out or executing PR code.
    try:
        pr, commits = policy.pull_snapshot(api, number, head)
        findings = policy.evaluate(pr, commits, policy_data)
        latest = api.get(f'pulls/{number}')
        if latest.get('state') != 'open' or latest['head']['sha'] != head or \
                any(latest[key] != pr[key] for key in ('title', 'body')) or \
                latest['base']['ref'] != pr['base']['ref']:
            raise policy.PolicyError('metadata target changed during validation')
        conclusion = 'failure' if findings else 'success'
        summary = '\n'.join(f"{row['rule']} at {row['location']}:{row['line']}" for row in findings)[:60000] \
            or 'Current PR text and original commit attribution passed contribution policy.'
    except (policy.PolicyError, KeyError, ValueError, OSError):
        conclusion = 'failure'
        summary = 'Current PR metadata could not be verified. Inspect this metadata workflow before dispatching again.'
    completed = api.write('PATCH', f'check-runs/{check_id}', {
        'status': 'completed', 'conclusion': conclusion,
        'completed_at': datetime.now(timezone.utc).isoformat(),
        'output': {'title': 'PR metadata', 'summary': summary}})
    if completed.get('head_sha') != head or completed.get('name') != CHECK_NAME or \
            (completed.get('app') or {}).get('id') != ACTIONS_APP or \
            completed.get('status') != 'completed' or completed.get('conclusion') != conclusion:
        raise policy.PolicyError('metadata check response does not match its exact-head producer')
    return {'pr': number, 'head': head, 'check_id': check_id, 'status': conclusion}


def run(api, event_name, event, sha, ref, run_id, policy_data, number='', head=''):
    default = event['repository']['default_branch']
    if not default or ref != 'refs/heads/' + default or event_name not in {'push', 'workflow_dispatch'}:
        raise policy.PolicyError('metadata backfill must run trusted code on the default branch')
    if not policy.SHA.fullmatch(sha) or not re.fullmatch(r'[1-9][0-9]*', str(run_id)):
        raise policy.PolicyError('metadata workflow identity is incomplete')
    if bool(number) != bool(head):
        raise policy.PolicyError('provide both pull-request and expected-head, or neither for missing-head backfill')
    if number:
        if not re.fullmatch(r'[1-9][0-9]*', str(number)):
            raise policy.PolicyError('pull-request must be a positive integer')
        return [publish(api, int(number), head, run_id, policy_data)]
    # Pagination covers every open PR. An existing producer, including queued,
    # running or red, is preserved; a backfill never overwrites its verdict.
    result = []
    for pr in api.pages('pulls?state=open'):
        result.append(publish(api, pr['number'], pr['head']['sha'], run_id, policy_data, missing_only=True))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pull-request', default='')
    parser.add_argument('--expected-head', default='')
    args = parser.parse_args()
    try:
        token = os.environ.get('PR_METADATA_TOKEN', '')
        if not token:
            raise policy.PolicyError('metadata check token is unavailable')
        result = run(GitHub(os.environ['GITHUB_REPOSITORY'], token), os.environ['GITHUB_EVENT_NAME'],
            json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text()), os.environ['GITHUB_SHA'],
            os.environ['GITHUB_REF'], os.environ['GITHUB_RUN_ID'], json.loads(policy.POLICY.read_text()),
            args.pull_request, args.expected_head)
        print(json.dumps({'metadata': result}, sort_keys=True))
        return int(any(row['status'] == 'failure' for row in result))
    except (policy.PolicyError, KeyError, ValueError, OSError):
        print('::error::Metadata backfill could not complete; inspect the exact-head checks before another dispatch.')
        return 1


if __name__ == '__main__':
    sys.exit(main())
