#!/usr/bin/env python3
"""Require base-declared generated provenance through the caller's dependency graph."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time

ACTIONS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ACTIONS / '_shared'))
from review_policy import MAX_POLICY_BYTES, POLICY_PATH, parse_policy, provenance_jobs

SHA = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')
MAX_METADATA_BYTES = 16 * 1024 * 1024
MAX_FETCH_BYTES = 64 * 1024 * 1024
ACQUISITION_SECONDS = 90


def helper(name, directory, filename):
    spec = importlib.util.spec_from_file_location(name, ACTIONS / directory / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scan = helper('generated_source', 'secret-scan', 'secret_scan.py')
contribution = helper('generated_contribution', 'contribution-policy', 'contribution_policy.py')


def valid_sha(value):
    return isinstance(value, str) and SHA.fullmatch(value) and set(value) != {'0'}


def event_range(event_name, event, sha, api=None):
    """Native PRs inspect the tested merge tree, never a mutable worktree."""
    expected_parents = None
    if event_name in ('pull_request', 'pull_request_target'):
        pr = event['pull_request']
        base, original = pr['base']['sha'], pr['head']['sha']
        head = sha if event_name == 'pull_request' else pr.get('merge_commit_sha')
        if head != original:
            expected_parents = [base, original]
    elif event_name == 'merge_group':
        base, head = event['merge_group']['base_sha'], event['merge_group']['head_sha']
        if head != sha:
            raise ValueError('merge-group revision does not match the tested commit')
    elif event_name == 'push':
        base, head = event.get('before'), sha
        if base == '0' * 40 or base == '0' * 64:
            return None  # Branch creation has no base declaration to authorize an exemption.
    elif event_name == 'workflow_dispatch':
        inputs = event.get('inputs') or {}
        if not any(inputs.get('code-' + key) for key in ('merge', 'head', 'base', 'pr', 'base-ref')):
            return None
        _, base, original = contribution.dispatch_target(api, event, sha)
        head, expected_parents = inputs['code-merge'], [base, original]
    else:
        return None
    if not valid_sha(base) or not valid_sha(head) or (expected_parents and not all(map(valid_sha, expected_parents))):
        raise ValueError('generated provenance requires an immutable base and tested revision')
    return base, head, expected_parents


def evaluate(policy, paths, needs, required):
    jobs, missing = provenance_jobs(policy, paths)
    bad = [f'{path}: generated path has no base-declared provenance job' for path in missing]
    if not isinstance(needs, dict):
        return bad + ['generated provenance needs must be toJSON(needs)']
    for job in sorted(jobs):
        if job not in required:
            bad.append(f'{job}: generated provenance job must be explicitly listed in required')
        value = needs.get(job)
        if not isinstance(value, dict) or value.get('result') != 'success':
            bad.append(f'{job}: generated provenance job must be in needs with result success')
    return bad


class Snapshot:
    def __init__(self, repo, deadline, env=None):
        self.repo, self.deadline = repo, deadline
        self.env = dict(os.environ, GIT_NO_LAZY_FETCH='1') if env is None else env

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError('generated provenance acquisition deadline expired')
        return remaining

    def git(self, *args, limit=MAX_METADATA_BYTES):
        result = scan.command(['git', '--no-replace-objects', '-C', str(self.repo), *args],
                              env=self.env, timeout=self.remaining(), max_bytes=MAX_FETCH_BYTES, output_limit=limit)
        if len(result) > limit:
            raise ValueError('generated provenance metadata exceeds its byte limit')
        return result

    def policy(self, base):
        if self.git('cat-file', '-t', base).strip() != b'commit':
            raise ValueError('generated provenance base is not a commit')
        rows = self.git('ls-tree', '-z', base, '--', POLICY_PATH, limit=4096).split(b'\0')
        if rows == [b'']:
            return parse_policy(None)
        if len(rows) != 2 or rows[-1] != b'':
            raise ValueError('generated provenance policy tree is unreadable')
        metadata, path = rows[0].split(b'\t')
        mode, kind, oid = metadata.split()
        if path.decode() != POLICY_PATH or mode not in (b'100644', b'100755') or kind != b'blob':
            raise ValueError('generated provenance policy must be an ordinary file')
        if int(self.git('cat-file', '-s', oid.decode(), limit=100)) > MAX_POLICY_BYTES:
            raise ValueError('generated provenance policy exceeds its byte limit')
        return parse_policy(self.git('cat-file', 'blob', oid.decode(), limit=MAX_POLICY_BYTES).decode('utf-8'))

    def changed(self, base, head, expected_parents=None):
        raw = self.git('cat-file', 'commit', head, limit=1024 * 1024)
        if expected_parents is not None:
            parents = [line.removeprefix(b'parent ').decode() for line in raw.split(b'\n\n', 1)[0].splitlines()
                       if line.startswith(b'parent ')]
            if parents != expected_parents:
                raise ValueError('tested PR merge does not match its base and head')
        raw = self.git('diff', '--no-ext-diff', '--no-textconv', '--no-renames', '--name-only', '-z', base, head, '--')
        if raw and not raw.endswith(b'\0'):
            raise ValueError('generated provenance changed-path inventory is incomplete')
        paths = raw.decode('utf-8').split('\0')[:-1]
        if any(not path or path.startswith('/') or any(part in ('', '.', '..') for part in path.split('/')) or
               re.search(r'[\x00\r\n\\]', path) for path in paths):
            raise ValueError('generated provenance changed-path inventory contains an unsupported path')
        return paths


def enforce(base, head, parents, needs, required, *, repository, token, workspace=None, remote=None):
    deadline = time.monotonic() + ACQUISITION_SECONDS
    if workspace:
        local = Snapshot(workspace, deadline)
        try:
            policy = local.policy(base)
            if not policy['generated']['paths']:
                return []
            paths = local.changed(base, head, parents)
        except (OSError, ValueError, scan.ScanError):
            pass  # Missing shallow/local objects are not absence; use isolated acquisition.
        else:
            return evaluate(policy, paths, needs, required)
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('generated provenance repository identity is invalid')
    if remote is None:
        server = os.environ.get('GITHUB_SERVER_URL', 'https://github.com').rstrip('/')
        if not server.startswith('https://'):
            raise ValueError('generated provenance source server must use HTTPS')
        remote = server + '/' + repository + '.git'
    with tempfile.TemporaryDirectory(prefix='generated-provenance-') as directory:
        repo = Path(directory)
        env = scan.fetch(repo, remote, '', base, token, depth=1, timeout=max(0.001, deadline - time.monotonic()), max_bytes=MAX_FETCH_BYTES)
        snapshot = Snapshot(repo, deadline, env)
        policy = snapshot.policy(base)
        if not policy['generated']['paths']:
            return []  # No head fetch, changed-source reads or inventory for ordinary callers.
        if head != base:
            snapshot.git('fetch', '--quiet', '--no-tags', '--depth=1', '--filter=blob:none', 'origin', head, limit=MAX_FETCH_BYTES)
        return evaluate(policy, snapshot.changed(base, head, parents), needs, required)


def main():
    try:
        event_name = os.environ.get('GITHUB_EVENT_NAME', '')
        if event_name not in ('pull_request', 'pull_request_target', 'merge_group', 'push', 'workflow_dispatch'):
            print('Generated provenance: no code range on this event.')
            return 0
        event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
        repository = os.environ['GITHUB_REPOSITORY']
        token = os.environ.get('GENERATED_POLICY_TOKEN', '')
        if (event.get('repository') or {}).get('full_name') != repository:
            raise ValueError('generated provenance event repository does not match the caller')
        api = contribution.GitHub(repository, token)
        selected = event_range(event_name, event, os.environ.get('GITHUB_SHA', ''), api)
        if selected is None:
            print('Generated provenance: no base-declared code range on this event.')
            return 0
        base, head, parents = selected
        needs = json.loads(os.environ.get('NEEDS', ''))
        required = {name.strip() for name in os.environ.get('REQUIRED', '').split(',') if name.strip()}
        bad = enforce(base, head, parents, needs, required, repository=repository, token=token,
                      workspace=os.environ.get('GITHUB_WORKSPACE'))
        for reason in bad:
            print('::error::Generated provenance: ' + reason)
        print(f'Generated provenance: {base}..{head}; {len(bad)} violation(s).')
        return int(bool(bad))
    except Exception:
        # Git/API errors and caller-controlled values may contain credentials or source.
        print('::error::Generated provenance: base policy, immutable range or complete path inventory could not be verified; required verdict refused')
        return 1


if __name__ == '__main__':
    sys.exit(main())
