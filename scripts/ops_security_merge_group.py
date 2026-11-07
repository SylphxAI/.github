#!/usr/bin/env python3
"""Project every member's exact-head operator stamp onto a public merge group.

Read-only: never posts a status, edits a PR, or modifies the merge queue. GitHub
Actions supplies the named check only on a merge_group event.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'ops_security_contribution_policy', ROOT / '.github/actions/contribution-policy/contribution_policy.py')
policy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(policy)


def check(api, event, sha):
    repository = event.get('repository') or {}
    group = event.get('merge_group') or {}
    if repository.get('full_name') != 'SylphxAI/.github' or repository.get('visibility') != 'public' or \
            repository.get('private') is not False or group.get('base_ref') != 'refs/heads/main' or \
            not isinstance(sha, str) or not policy.SHA.fullmatch(sha) or group.get('head_sha') != sha:
        raise policy.PolicyError('not the public .github/main merge-group candidate')
    commits, snapshots, queued = policy.merge_group_snapshot(
        api, group, check_pull_text=False, bind_all_heads=True)
    if not snapshots:
        raise policy.PolicyError('merge group has no resolved PR members')
    findings = []
    for pr, _ in snapshots:
        findings.extend(policy.ops_security_review_findings(api, pr))
    findings.extend(policy.recheck_merge_group(api, group, commits, snapshots, queued))
    return findings, len(snapshots)


def main():
    try:
        if os.environ.get('GITHUB_EVENT_NAME') != 'merge_group' or \
                os.environ.get('GITHUB_REPOSITORY') != 'SylphxAI/.github':
            raise policy.PolicyError('this producer runs only on .github merge groups, never PR heads')
        token = os.environ.get('GH_TOKEN', '')
        if not token:
            raise policy.PolicyError('read-only metadata token is unavailable')
        event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
        api = policy.GitHub('SylphxAI/.github', token)
        findings, count = check(api, event, os.environ.get('GITHUB_SHA', ''))
        for finding in findings:
            print('::error::{rule} at {location}'.format(**finding))
        print(f'ops-security/review: checked {count} exact-head PR stamp(s); {len(findings)} finding(s).')
        return int(bool(findings))
    except (policy.PolicyError, KeyError, ValueError, OSError, TypeError) as error:
        message = str(error) if isinstance(error, policy.PolicyError) else 'merge-group metadata could not be read'
        print('::error::ops-security/review: ' + message)
        return 2


if __name__ == '__main__':
    sys.exit(main())
