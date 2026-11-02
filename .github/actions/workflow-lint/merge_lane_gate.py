#!/usr/bin/env python3
"""Required R8 verdict over immutable workflow source, without a caller checkout."""
from __future__ import annotations

import datetime
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import subprocess
import time

SOURCE = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(SOURCE / 'scripts'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from merge_lane import merge_lane_findings
from audit_optimistic_merge import load_policy, exemption_for, waived_rows
from perf_gate import delivered


def contribution():
    spec = importlib.util.spec_from_file_location('merge_lane_contribution',
        SOURCE / '.github/actions/contribution-policy/contribution_policy.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source_sha(event_name, event, sha):
    # GITHUB_SHA is the actual tested merge revision on pull_request and
    # merge_group. Do not substitute a PR head (secret scanning uses that).
    if event_name == 'merge_group':
        group_sha = (event.get('merge_group') or {}).get('head_sha')
        if group_sha != sha:
            raise ValueError('merge-group payload does not bind the tested revision')
        sha = group_sha
    if not re.fullmatch(r'[0-9a-f]{40}', sha or '') or set(sha) == {'0'}:
        raise ValueError('merge-lane check requires an immutable workflow revision')
    return sha


def retry_read(operation):
    for attempt in range(3):
        try:
            return operation()
        except Exception as error:
            # Do not route around or hammer an explicit provider rate limit.
            if 'rate limit' in str(error).lower() or attempt == 2:
                raise
            time.sleep(attempt + 1)


def delivery_state():
    for attempt in range(3):
        state = delivered(dict(os.environ))
        if state is not None:
            return state
        if attempt < 2:
            time.sleep(attempt + 1)
    print('::warning::merge-lane: delivery property API read unavailable after 3 attempts; inspecting source as non-delivered')
    return False


def local_workflows(sha, event):
    """Read immutable Git objects when this checkout contains the tested commit.

    Never read mutable worktree files or execute repository code. Missing local
    objects fall back to the API; readable local source uses no source API call.
    """
    workspace = os.environ.get('GITHUB_WORKSPACE')
    if not workspace:
        return None

    def git(*args):
        result = subprocess.run(['git', '--no-replace-objects', '-C', workspace, *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15)
        return result.stdout

    try:
        if git('cat-file', '-t', sha).strip() != b'commit':
            return None
        rows = git('ls-tree', '-z', sha + ':.github/workflows').split(b'\0')
        workflows = {}
        for row in rows:
            if not row:
                continue
            metadata, name = row.split(b'\t', 1)
            name = name.decode('utf-8')
            if not name.endswith(('.yml', '.yaml')):
                continue
            mode, kind, oid = metadata.split()
            if mode not in (b'100644', b'100755') or kind != b'blob':
                return None
            source = git('cat-file', 'blob', oid.decode('ascii'))
            if b'\0' in source or len(source) > 1000000:
                return None
            workflows[name] = source.decode('utf-8')
        private = (event.get('repository') or {}).get('private')
        return (workflows, private if isinstance(private, bool) else None) if workflows else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def read_workflows(api, sha):
    owner, name = api.repository.split('/')
    query = '''query($owner:String!,$name:String!,$revision:String!,$sha:GitObjectID!){
      repository(owner:$owner,name:$name){isPrivate
        commit:object(oid:$sha){... on Commit{oid}}
        object(expression:$revision){... on Tree{entries{name type mode object{... on Blob{text isTruncated isBinary}}}}}}
    }'''
    result = retry_read(lambda: api.read(api.root + '/graphql', {'query': query, 'variables': {
        'owner': owner, 'name': name, 'revision': sha + ':.github/workflows', 'sha': sha}}))
    if result.get('errors'):
        raise ValueError('immutable workflow tree read failed')
    repository = (result.get('data') or {}).get('repository')
    if not repository or not isinstance(repository.get('isPrivate'), bool) or (repository.get('commit') or {}).get('oid') != sha:
        raise ValueError('immutable workflow tree or visibility is unreadable')
    tree = repository.get('object')
    if tree is None:
        # A repository with no workflows cannot be executing this verdict.
        raise ValueError('immutable workflow tree is unreadable')
    entries = tree.get('entries')
    if not isinstance(entries, list):
        raise ValueError('immutable workflow tree is unreadable')
    workflows = {}
    for entry in entries:
        name = entry.get('name', '')
        if not name.endswith(('.yml', '.yaml')):
            continue
        blob = entry.get('object') or {}
        if entry.get('type') != 'blob' or entry.get('mode') not in (33188, 33261) or blob.get('isTruncated') is not False or blob.get('isBinary') is not False or not isinstance(blob.get('text'), str):
            raise ValueError('immutable workflow source is unreadable or truncated')
        workflows[name] = blob['text']
    if not workflows:
        raise ValueError('immutable workflow tree has no readable workflows')
    return workflows, repository['isPrivate']


def exempt(repository, today):
    policy = load_policy(SOURCE / 'policy/optimistic-merge.json')
    entry, _ = exemption_for(policy, repository, today)
    # Existing audit-only no-build/frozen entries do not silently waive a new
    # required runner rule. Only an explicit, expiring R8 policy does.
    return entry if entry and (entry['class'] == 'hands-off' or
        entry['class'] == 'static-runner-policy' and 'R8' in waived_rows(policy, entry)) else None


def main():
    event_name = os.environ.get('GITHUB_EVENT_NAME', '')
    if event_name not in ('pull_request', 'merge_group', 'workflow_dispatch'):
        print('merge-lane: no pre-merge workflow revision for this event; skipped')
        return 0
    try:
        repository = os.environ['GITHUB_REPOSITORY']
        entry = exempt(repository, datetime.datetime.now(datetime.timezone.utc).date())
        if entry:
            print(f"merge-lane: explicit {entry['class']} exemption; owner={entry['owner']}, review={entry['review']}")
            return 0
        state = delivery_state()
        if state is True:
            print('merge-lane: delivered customer repository; skipped')
            return 0
        event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_bytes())
        policy = contribution()
        api = policy.GitHub(repository, os.environ.get('GITHUB_TOKEN', ''))
        if event_name == 'workflow_dispatch':
            inputs = event.get('inputs') or {}
            if not any(inputs.get('code-' + key) for key in ('merge', 'head', 'base', 'pr', 'base-ref')):
                print('merge-lane: dispatch is not a pre-merge code verdict; skipped')
                return 0
            policy.dispatch_target(api, event, os.environ.get('GITHUB_SHA', ''))
            sha = source_sha(event_name, event, inputs['code-merge'])
        else:
            sha = source_sha(event_name, event, os.environ.get('GITHUB_SHA', ''))
        local = local_workflows(sha, event)
        workflows, private = local if local is not None else read_workflows(api, sha)
        notes = []
        findings = merge_lane_findings(workflows, private=private, org=repository.split('/')[0], notes=notes)
        for note in notes:
            print(f'::notice file=.github/workflows/{note.file},line={note.line}::job {note.job}: {note.problem}')
        for finding in findings:
            print(f'::error file=.github/workflows/{finding.file},line={finding.line}::job {finding.job}: {finding.problem}; select its same-size -merge twin on merge_group')
        print(f'merge-lane: {sha}, {len(workflows)} workflows, {len(findings)} violation(s)')
        return int(bool(findings))
    except Exception:
        # API/subprocess errors must not leak arbitrary remote data or credentials.
        print('::error::merge-lane: workflow source or target could not be verified (API/source read failure, not a lane violation); required verdict refused')
        return 1


if __name__ == '__main__':
    sys.exit(main())
