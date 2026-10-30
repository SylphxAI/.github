#!/usr/bin/env python3
"""Scan added commits without executing target code or printing secret values."""
from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

VERSION = '8.30.1'
CHECKSUMS = {
    'x86_64': ('x64', '551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb'),
    'aarch64': ('arm64', 'e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080'),
}
SHA = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')
ALLOWLIST = '.desk-secret-allowlist.json'


class ScanError(Exception):
    """Only constant, value-free messages may cross the output boundary."""


def command(args, *, cwd=None, env=None):
    result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, timeout=180)
    if result.returncode:
        # Git and detector diagnostics can contain blob contents or credentials.
        raise ScanError('secret scan command failed; raw diagnostics withheld')
    return result.stdout


def git(repo, *args):
    return command(['git', '-C', str(repo), *args])


def install(directory, version):
    if version != VERSION or platform.system() != 'Linux' or platform.machine() not in CHECKSUMS:
        raise ScanError('secret scan requires the checksum-pinned Linux gitleaks release')
    arch, digest = CHECKSUMS[platform.machine()]
    url = f'https://github.com/gitleaks/gitleaks/releases/download/v{VERSION}/gitleaks_{VERSION}_linux_{arch}.tar.gz'
    with urllib.request.urlopen(url, timeout=60) as response:
        archive = response.read()
    if hashlib.sha256(archive).hexdigest() != digest:
        raise ScanError('gitleaks release checksum mismatch')
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as bundle:
        member = bundle.getmember('gitleaks')
        if not member.isfile():
            raise ScanError('gitleaks release has no regular executable')
        binary = directory / 'gitleaks'
        binary.write_bytes(bundle.extractfile(member).read())
        binary.chmod(0o700)
    return str(binary)


def revisions(event_name, event, sha):
    if event_name == 'pull_request':
        pr = event.get('pull_request', {})
        base, head = pr.get('base', {}).get('sha', ''), pr.get('head', {}).get('sha', '')
    elif event_name == 'merge_group':
        group = event.get('merge_group', {})
        base, head = group.get('base_sha', ''), group.get('head_sha', '')
    else:
        base, head = event.get('before', ''), sha
        if not base or set(base) == {'0'}:
            base = ''
    if not isinstance(head, str) or not SHA.fullmatch(head) or set(head) == {'0'} or \
            (base and (not isinstance(base, str) or not SHA.fullmatch(base) or set(base) == {'0'})) or \
            (event_name in {'pull_request', 'merge_group'} and not base):
        raise ScanError('secret scan event has no valid immutable commit range')
    return base, head


def fetch(repo, remote, base, head, token, *, filter_spec='blob:none'):
    git(repo, 'init', '--bare', '-q')
    git(repo, 'remote', 'add', 'origin', remote)
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0')
    if token:
        # Scope authentication to this GitHub server, never a URL from PR code.
        server = os.environ.get('GITHUB_SERVER_URL', 'https://github.com').rstrip('/')
        header = 'AUTHORIZATION: basic ' + base64.b64encode(('x-access-token:' + token).encode()).decode()
        env.update(GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0=f'http.{server}/.extraheader', GIT_CONFIG_VALUE_0=header)
    # Fetch complete commit ancestry but defer unrelated blobs. The same
    # environment authenticates lazy blob reads during detection and allowlisting.
    command(['git', '-C', str(repo), 'fetch', '--quiet', '--no-tags', '--filter=' + filter_spec,
             'origin', *([base] if base else []), head], env=env)
    return env


def allowlist(repo, head):
    tree = git(repo, 'ls-tree', '-z', head, '--', ALLOWLIST)
    if not tree:
        return set()
    fields, _, name = tree.rstrip(b'\0').partition(b'\t')
    mode, kind, blob = fields.split()
    if mode != b'100644' or kind != b'blob' or name != ALLOWLIST.encode():
        raise ScanError('secret allowlist must be a regular JSON file')
    if int(git(repo, 'cat-file', '-s', blob.decode())) > 1024 * 1024:
        raise ScanError('secret allowlist exceeds 1 MiB')
    try:
        data = json.loads(git(repo, 'cat-file', 'blob', blob.decode()))
        if not isinstance(data, dict) or set(data) != {'version', 'findings'} or \
                type(data['version']) is not int or data['version'] != 1 or not isinstance(data['findings'], list):
            raise ValueError()
        allowed = set()
        for item in data['findings']:
            if not isinstance(item, dict) or set(item) != {'commit', 'path', 'line', 'sha256', 'reason'}:
                raise ValueError()
            commit, path, line, digest, reason = (item[k] for k in ('commit', 'path', 'line', 'sha256', 'reason'))
            if not isinstance(commit, str) or not SHA.fullmatch(commit) or \
                    not isinstance(path, str) or not path or type(line) is not int or line < 1 or \
                    not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest) or \
                    not isinstance(reason, str) or not reason.strip():
                raise ValueError()
            key = commit, path, line, digest
            if key in allowed:
                raise ValueError()
            allowed.add(key)
        return allowed
    except (ValueError, TypeError, KeyError):
        raise ScanError('secret allowlist has invalid schema or review reason') from None


def merge_additions(repo, commit):
    """Combined diffs identify resolution lines absent from every parent.

    Gitleaks parses ordinary patches, so it scans the first-parent patch and
    these line numbers discard inherited additions from its merge findings.
    """
    patch = git(repo, '-c', 'core.quotePath=true', 'show', '--format=', '--cc',
                '--unified=0', '--no-renames', '--no-ext-diff', '--no-textconv', commit).decode('utf-8')
    path, number, parents = None, 0, 0
    additions = set()
    for row in patch.split('\n'):
        if row.startswith(('diff --git ', 'diff --cc ', 'diff --combined ')):
            path, parents = None, 0
        elif not parents and row.startswith('+++ '):
            name = row[4:]
            if name.startswith('"'):
                # Git's C quoting uses octal UTF-8 bytes.
                path = ast.literal_eval(name).encode('latin1').decode('utf-8')
            else:
                path = name.removesuffix('\t')
            if path.startswith('b/'):
                path = path[2:]
        elif row.startswith('@@@'):
            parents = len(row) - len(row.lstrip('@')) - 1
            match = re.search(r'\+(\d+)(?:,\d+)? @+', row)
            if not match:
                raise ScanError('secret scan could not parse a merge resolution')
            number = int(match[1])
        elif parents and row and not row.startswith('\\'):
            prefix = row[:parents]
            if prefix == '+' * parents:
                additions.add((path, number))
            if '-' not in prefix:
                number += 1
    return additions


def scan(repo, base, head, binary, directory, config=''):
    for rev in (base, head):
        if rev:
            git(repo, 'cat-file', '-e', rev + '^{commit}')
    allowed = allowlist(repo, head)
    policy = directory / 'gitleaks.toml'
    policy.write_text('[extend]\nuseDefault = true\n')
    report = directory / 'findings.json'
    log_range = f'{base}..{head}' if base else f'-1 {head}'
    print('Scanning added commits: ' + log_range)
    result = subprocess.run([
        binary, 'git', str(repo), '--log-opts=' +
        '--full-history --no-ext-diff --no-textconv --unified=0 --no-renames --diff-merges=first-parent ' + log_range,
        '--config', config or str(policy), '--ignore-gitleaks-allow',
        '--gitleaks-ignore-path', str(directory / 'no-ignores'),
        '--no-banner', '--no-color', '--log-level', 'error', '--report-format', 'json', '--report-path', str(report),
    ], cwd=directory, capture_output=True, timeout=180)
    # The raw report is private to this temporary directory and is never uploaded.
    if result.returncode not in (0, 1) or not report.is_file():
        raise ScanError('gitleaks did not complete; raw diagnostics withheld')
    findings = json.loads(report.read_bytes())
    if not isinstance(findings, list) or (result.returncode == 1 and not findings):
        raise ScanError('gitleaks returned an invalid findings report')
    refused = set()
    exempted = set()
    merges = {}
    secrets = {value for item in findings for value in
               [item.get('Secret', ''), *item.get('Secret', '').splitlines()] if value}
    for item in findings:
        commit, path, start, end = (item[k] for k in ('Commit', 'File', 'StartLine', 'EndLine'))
        if not SHA.fullmatch(commit) or not isinstance(path, str) or not path or \
                type(start) is not int or type(end) is not int or not 0 < start <= end:
            raise ScanError('gitleaks returned an invalid finding location')
        if commit not in merges:
            parents = git(repo, 'rev-list', '--parents', '-n', '1', commit).split()
            merges[commit] = merge_additions(repo, commit) if len(parents) > 2 else None
        lines = git(repo, 'show', commit + ':' + path).split(b'\n')
        if end > len(lines):
            raise ScanError('gitleaks finding is outside its source blob')
        for number in range(start, end + 1):
            if merges[commit] is not None and (path, number) not in merges[commit]:
                continue
            key = commit, path, number, hashlib.sha256(lines[number - 1]).hexdigest()
            (exempted if key in allowed else refused).add(key)
    for commit, path, line, digest in sorted(refused):
        # Paths can themselves contain a finding; escape control characters and
        # prevent a crafted filename from injecting another workflow command.
        location = '<withheld: path contains finding>' if any(s in path for s in secrets) else path
        location = json.dumps(location, ensure_ascii=True).replace('%', '%25')
        print(f'::error::Secret finding: commit {commit} path {location} line {line} sha256 {digest}')
    if exempted:
        print(f'reviewed findings allowed={len(exempted)}')
    if refused:
        print(f'Secret scan refused {len(refused)} added line(s); values are never printed. '
              f'Review false positives in {ALLOWLIST} with commit, path, line, sha256 and reason.')
        return 1
    print('Secret scan passed: no unreviewed findings in added commits.')
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default='.')
    parser.add_argument('--fetch', action='store_true')
    parser.add_argument('--remote', default='')
    parser.add_argument('--pre-merge-only', action='store_true')
    parser.add_argument('--gitleaks', default='')
    parser.add_argument('--version', default=VERSION)
    parser.add_argument('--config', default='')
    args = parser.parse_args()
    event_name = os.environ.get('GITHUB_EVENT_NAME', '')
    if args.pre_merge_only and event_name not in {'pull_request', 'merge_group'}:
        print('Secret scan: no pre-merge range for this event.')
        return 0
    try:
        event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_bytes())
        base, head = revisions(event_name, event, os.environ.get('GITHUB_SHA', ''))
        with tempfile.TemporaryDirectory(prefix='shared-secret-scan-') as tmp:
            directory = Path(tmp)
            repo = Path(args.repo).resolve()
            if args.fetch:
                repo = directory / 'repository.git'
                repo.mkdir()
                server = os.environ.get('GITHUB_SERVER_URL', 'https://github.com').rstrip('/')
                repository = os.environ.get('GITHUB_REPOSITORY', '')
                if not args.remote and not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
                    raise ScanError('secret scan has no valid target repository')
                remote = args.remote or f'{server}/{repository}.git'
                env = fetch(repo, remote, base, head, os.environ.get('SECRET_SCAN_TOKEN', ''))
                os.environ.update(env)
            binary = args.gitleaks or install(directory, args.version)
            return scan(repo, base, head, binary, directory, str(Path(args.config).resolve()) if args.config else '')
    except ScanError as error:
        print('::error::' + str(error))
    except Exception:
        # JSON, subprocess, filesystem and download exceptions may carry values.
        print('::error::Secret scan could not complete; raw diagnostics withheld.')
    return 1


if __name__ == '__main__':
    sys.exit(main())
