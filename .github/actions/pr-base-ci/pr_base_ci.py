#!/usr/bin/env python3
"""Retargeting changes the tested base; ordinary PR text edits do not."""
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import quote
import urllib.request


class MergePending(ValueError):
    pass


def dispatch(api, event, workflow, repository):
    if event.get('action') != 'edited' or 'base' not in event.get('changes', {}):
        return False
    if not re.fullmatch(r'[A-Za-z0-9_.-]+\.ya?ml', workflow):
        raise ValueError('code workflow must be a filename')
    expected = event['pull_request']
    current = api('GET', f"pulls/{event['number']}")
    if current['state'] != 'open' or current['head']['sha'] != expected['head']['sha'] or \
            current['base']['ref'] != expected['base']['ref']:
        raise ValueError('PR revision changed before base validation; current code CI must finish')
    if current['head']['repo']['full_name'] != repository:
        raise ValueError('Retargeted fork PR needs a push or reopen to validate its new base')
    branch = current['head']['ref']
    ref = api('GET', 'git/ref/heads/' + quote(branch, safe=''))
    if ref['object']['sha'] != current['head']['sha']:
        raise ValueError('branch changed before base validation; current code CI must finish')
    merge = current.get('merge_commit_sha')
    if not merge:
        raise MergePending('PR merge is not available for base validation')
    commit = api('GET', 'git/commits/' + merge)
    parents = [parent['sha'] for parent in commit['parents']]
    if parents != [current['base']['sha'], current['head']['sha']]:
        raise MergePending('PR merge does not yet represent the current base and head')
    api('POST', 'actions/workflows/' + workflow + '/dispatches', {'ref': branch, 'inputs': {
        'code-merge': merge, 'code-head': current['head']['sha'], 'code-base': current['base']['sha'],
        'code-pr': str(event['number']), 'code-base-ref': current['base']['ref']}})
    return True


def main():
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
    # Ordinary metadata events do not need a write token or any API call.
    if event.get('action') != 'edited' or 'base' not in event.get('changes', {}):
        print('PR base unchanged; no code workflow dispatched.')
        return 0
    repository = os.environ['GITHUB_REPOSITORY']
    root = os.environ.get('GITHUB_API_URL', 'https://api.github.com').rstrip('/')
    token = os.environ['PR_BASE_CI_TOKEN']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository) or not root.startswith('https://'):
        raise ValueError('invalid GitHub API identity')

    def api(method, path, body=None):
        request = urllib.request.Request(root + '/repos/' + repository + '/' + path,
            method=method, data=json.dumps(body).encode() if body is not None else None,
            headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
                     'Content-Type': 'application/json', 'X-GitHub-Api-Version': '2022-11-28'})
        with urllib.request.urlopen(request, timeout=30) as response:
            if method == 'POST':
                if response.status != 204:
                    raise ValueError('code workflow dispatch was not accepted')
                print('Code workflow dispatch accepted (HTTP 204); code CI must supply its own verdict.')
                return None
            return json.load(response)

    # GitHub computes mergeability asynchronously. Wait for that read to settle;
    # the dispatch itself is sent once and is never retried after an unknown result.
    deadline = time.monotonic() + 30
    while True:
        try:
            dispatch(api, event, os.environ['PR_BASE_CI_WORKFLOW'], repository)
            return 0
        except MergePending:
            if time.monotonic() >= deadline:
                raise
            time.sleep(2)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (KeyError, ValueError, OSError):
        # Never repeat a request or print credentials after an unknown result.
        print('::error::PR base validation could not start; inspect the code workflow before retrying.')
        sys.exit(1)
