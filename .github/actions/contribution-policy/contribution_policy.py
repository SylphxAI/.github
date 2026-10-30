#!/usr/bin/env python3
"""Check immutable commit ranges and current pull request metadata."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[3]
POLICY = ROOT / 'policy/contribution-policy.json'
SHA = re.compile(r'[0-9a-f]{40}\Z')


class PolicyError(Exception):
    pass


def evaluate(pull_request, commits, policy):
    """Return value-free findings for REST-shaped metadata; never mutate input."""
    identities = {role: {(item['name'], item['email']) for item in policy['identities'] if role in item['roles']}
                  for role in ('author', 'committer')}
    names = '|'.join(re.escape(name) for name in policy['attribution_names'])
    harness_names = names + '|' + '|'.join(re.escape(name) for name in policy['harness_names'])
    attributed = re.compile(r'(?<![A-Za-z0-9])(?:' + names + r')(?![A-Za-z0-9])', re.I)
    coauthor = re.compile(r'co-authored-by\s*:\s*(.*)', re.I)
    generated = re.compile(r'^[\s>*_\-\[🤖]*generated\s+(?:with|by)\s*:?\s*\S', re.I)
    trailer = re.compile(r'^[\s>*_\-]*agent(?:-id)?\s*:\s*\S', re.I)
    harness = re.compile(r'^[\s>*_\-]*harness\s*:\s*\[?(?:' + harness_names + r')(?![A-Za-z0-9])', re.I)
    auxiliary = r'(?:was|is|were|are|has\s+been|have\s+been)\s+'
    contribution = (r'\b(?:(?:changes?|patch(?:es)?|commits?|pull\s+requests?|PRs?|implementations?|'
                    r'code|fix(?:es)?|updates?|work)\s+(?:' + auxiliary + r')?|'
                    r'(?:this|these|it|they)\s+' + auxiliary + r')')
    authorship = re.compile(r'(?:^[\s>*_\-🤖]*|' + contribution +
                            r')(?:written|authored|built|implemented|reviewed|assisted|generated)\s+'
                            r'(?:by|with|using)\s*:?[\s\[]*(?:the\s+|an?\s+)?(' + names +
                            r')(?![A-Za-z0-9])', re.I)
    internal = [(rule, re.compile(pattern)) for rule, pattern in policy['internal_patterns'].items()]
    findings = []

    def text(value, location):
        if value is None:
            value = ''
        if not isinstance(value, str):
            raise PolicyError('metadata contains a non-text field')
        for number, line in enumerate(value.splitlines(), 1):
            match = coauthor.search(line)
            rules = []
            credit = re.sub(r'([^\s<>@]+)@[^\s<>@]+', r'\1', match[1]) if match else ''
            if match and attributed.search(credit):
                rules.append('tool-coauthor')
            if generated.search(line):
                rules.append('generated-trailer')
            if trailer.search(line) or harness.search(line):
                rules.append('agent-trailer')
            if authorship.search(line):
                rules.append('tool-authorship')
            rules.extend(rule for rule, pattern in internal if pattern.search(line))
            findings.extend({'rule': rule, 'location': location, 'line': number} for rule in rules)

    if pull_request is not None:
        if not isinstance(pull_request.get('title'), str) or 'body' not in pull_request:
            raise PolicyError('pull request metadata is incomplete')
        for field in ('title', 'body'):
            text(pull_request.get(field), 'pull-request.' + field)
    for item in commits:
        sha = item.get('sha', '')
        if not SHA.fullmatch(sha):
            raise PolicyError('commit metadata has no immutable identity')
        commit = item.get('commit', {})
        if not {'author', 'committer', 'message'} <= commit.keys():
            raise PolicyError('commit metadata is incomplete')
        for role in ('author', 'committer'):
            identity = commit.get(role) or {}
            if (identity.get('name'), identity.get('email')) not in identities[role]:
                findings.append({'rule': 'unapproved-' + role, 'location': sha, 'line': 0})
        text(commit.get('message'), sha)
    return findings


class GitHub:
    def __init__(self, repository, token):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
            raise PolicyError('invalid repository identity')
        self.root = os.environ.get('GITHUB_API_URL', 'https://api.github.com').rstrip('/')
        if not self.root.startswith('https://'):
            raise PolicyError('GitHub API must use HTTPS')
        self.repository = repository
        self.token = token

    def read(self, url, data=None):
        request = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                         headers={'Authorization': 'Bearer ' + self.token,
                                                  'Accept': 'application/vnd.github+json',
                                                  'Content-Type': 'application/json',
                                                  'X-GitHub-Api-Version': '2022-11-28'})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise PolicyError(f'GitHub metadata read failed (HTTP {error.code})') from None
        except (OSError, ValueError):
            raise PolicyError('GitHub metadata read failed') from None

    def get(self, path):
        return self.read(self.root + '/repos/' + self.repository + '/' + path)

    def pages(self, path):
        result = []
        page = 1
        while True:
            separator = '&' if '?' in path else '?'
            rows = self.get(f'{path}{separator}per_page=100&page={page}')
            if not isinstance(rows, list):
                raise PolicyError('GitHub list response is invalid')
            result.extend(rows)
            if len(rows) < 100:
                return result
            page += 1

    def queue_entries(self, branch):
        query = '''query($owner:String!, $name:String!, $branch:String!, $cursor:String) {
          repository(owner:$owner, name:$name) { mergeQueue(branch:$branch) {
            entries(first:100, after:$cursor) {
              nodes { pullRequest { number headRefOid } headCommit { oid } }
              pageInfo { hasNextPage endCursor }
            }
          } }
        }'''
        owner, name = self.repository.split('/')
        cursor, entries = None, []
        while True:
            response = self.read(os.environ.get('GITHUB_GRAPHQL_URL', self.root + '/graphql'),
                                 {'query': query, 'variables': {'owner': owner, 'name': name,
                                  'branch': branch, 'cursor': cursor}})
            if response.get('errors'):
                raise PolicyError('GitHub merge queue metadata is unreadable')
            queue = response['data']['repository']['mergeQueue']
            if queue is None:
                return entries
            connection = queue['entries']
            entries.extend(connection['nodes'])
            page = connection['pageInfo']
            if not page['hasNextPage']:
                return entries
            if not page['endCursor'] or page['endCursor'] == cursor:
                raise PolicyError('GitHub merge queue pagination is incomplete')
            cursor = page['endCursor']


def range_commits(base, head, repository, token):
    if not SHA.fullmatch(base) or not SHA.fullmatch(head):
        raise PolicyError('event has no immutable commit range')
    # Share the existing read-only, authenticated object acquisition path.
    spec = importlib.util.spec_from_file_location('contribution_fetch', ROOT / '.github/actions/secret-scan/secret_scan.py')
    source = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(source)
    server = os.environ.get('GITHUB_SERVER_URL', 'https://github.com').rstrip('/')
    with tempfile.TemporaryDirectory(prefix='contribution-policy-') as directory:
        repo = Path(directory)
        try:
            source.fetch(repo, server + '/' + repository + '.git', base, head, token, filter_spec='tree:0')
            data = source.git(repo, 'log', '-z', '--no-show-signature',
                              '--format=%H%x00%an%x00%ae%x00%cn%x00%ce%x00%B', base + '..' + head)
        except source.ScanError:
            raise PolicyError('immutable commit acquisition failed') from None
    return parse_commits(data)


def parse_commits(data):
    fields = data.decode('utf-8', errors='replace').split('\0')
    if fields[-1] != '' or (len(fields) - 1) % 6:
        raise PolicyError('commit metadata is incomplete')
    result = []
    for offset in range(0, len(fields) - 1, 6):
        sha, author, email, committer, committer_email, message = fields[offset:offset + 6]
        result.append({'sha': sha, 'commit': {'author': {'name': author, 'email': email},
                       'committer': {'name': committer, 'email': committer_email}, 'message': message}})
    return result


def pull_snapshot(api, number, expected_head=None, *, check_text=True):
    before = api.get(f'pulls/{number}')
    head = before['head']['sha']
    if expected_head and head != expected_head:
        raise PolicyError('pull request head changed; run the check on its current head')
    commits = range_commits(before['base']['sha'], head, api.repository, api.token)
    after = api.get(f'pulls/{number}')
    for key in ('title', 'body') if check_text else ():
        if before[key] != after[key]:
            raise PolicyError('pull request metadata changed during the check; rerun it')
    if before['head']['sha'] != after['head']['sha'] or before['base']['ref'] != after['base']['ref']:
        raise PolicyError('pull request revision changed during the check; rerun it')
    return after, commits


def check_event(api, event_name, event, sha, policy, *, check_pull_text=True):
    def snapshot(number, head):
        if check_pull_text:
            return pull_snapshot(api, number, head)
        pr, commits = pull_snapshot(api, number, head, check_text=False)
        return None, commits

    if event_name in {'pull_request', 'pull_request_target'}:
        pr = event.get('pull_request', {})
        return evaluate(*snapshot(event.get('number') or pr['number'], pr['head']['sha']), policy)
    if event_name == 'merge_group':
        group = event['merge_group']
        commits = range_commits(group['base_sha'], group['head_sha'], api.repository, api.token)
        findings = evaluate(None, commits, policy)
        pulls, covered = {}, set()
        group_shas = {commit['sha'] for commit in commits}
        branch = group['base_ref'].removeprefix('refs/heads/')
        for entry in api.queue_entries(branch):
            synthetic = (entry.get('headCommit') or {}).get('oid')
            if synthetic in group_shas:
                pr = entry['pullRequest']
                pulls[pr['number']] = pr['headRefOid']
                covered.add(synthetic)
        for number, expected_head in pulls.items():
            pr, original = snapshot(number, expected_head)
            covered.update(commit['sha'] for commit in original)
            findings.extend(evaluate(pr, original, policy))
        for commit in commits:
            if commit['sha'] in covered:
                continue
            # Earlier entries can merge while a later group is still building.
            for pr in api.pages(f"commits/{commit['sha']}/pulls"):
                if pr['base']['ref'] == branch and pr.get('merged_at') and \
                        pr.get('merge_commit_sha') == commit['sha']:
                    current, original = snapshot(pr['number'], pr['head']['sha'])
                    covered.add(commit['sha'])
                    covered.update(item['sha'] for item in original)
                    findings.extend(evaluate(current, original, policy))
        if group_shas - covered:
            raise PolicyError('merge group membership is incomplete')
        return findings
    if event_name == 'workflow_dispatch':
        inputs = event.get('inputs') or {}
        if inputs.get('code-merge'):
            merge, head, base = (inputs.get('code-' + part, '') for part in ('merge', 'head', 'base'))
            if not all(SHA.fullmatch(value) for value in (merge, head, base)) or head != sha:
                raise PolicyError('dispatched code evidence does not match this head')
            parents = api.get('git/commits/' + merge).get('parents', [])
            if [parent['sha'] for parent in parents] != [base, head]:
                raise PolicyError('dispatched code merge does not match its base and head')
        # A dispatch on a PR head must not replace a failed PR verdict with green.
        pulls = [pr for pr in api.pages('pulls?state=open') if pr['head']['sha'] == sha]
        findings = []
        for pr in pulls:
            findings.extend(evaluate(*snapshot(pr['number'], sha), policy))
        return findings
    return []


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, help='Offline JSON with pull_request and commits fields')
    parser.add_argument('--report', type=Path)
    parser.add_argument('--scope', choices=('all', 'commits'), default='all',
                        help='commits is for code verdicts with a separately required PR metadata producer')
    args = parser.parse_args(argv)
    try:
        policy = json.loads(POLICY.read_text())
        if args.input:
            data = json.loads(args.input.read_text())
            findings = evaluate(data['pull_request'] if args.scope == 'all' else None, data['commits'], policy)
        else:
            event_name = os.environ.get('GITHUB_EVENT_NAME', '')
            if event_name not in {'pull_request', 'pull_request_target', 'merge_group', 'workflow_dispatch'}:
                print('Contribution policy: no pre-merge metadata on this event.')
                return 0
            token = os.environ.get('CONTRIBUTION_POLICY_TOKEN', '')
            if not token:
                raise PolicyError('read-only contribution metadata token is unavailable')
            event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
            api = GitHub(os.environ['GITHUB_REPOSITORY'], token)
            findings = check_event(api, event_name, event, os.environ.get('GITHUB_SHA', ''), policy,
                                   check_pull_text=args.scope == 'all')
        if args.report:
            args.report.write_text(json.dumps({'passed': not findings, 'findings': findings}, indent=2) + '\n')
        for finding in findings:
            print('::error::Contribution policy: {rule} at {location}:{line}'.format(**finding))
        print(f'Contribution policy: {len(findings)} finding(s).')
        return int(bool(findings))
    except (PolicyError, KeyError, ValueError, OSError) as error:
        message = str(error) if isinstance(error, PolicyError) else 'metadata or policy could not be read'
        if args.report:
            args.report.write_text(json.dumps({'passed': False, 'findings': [], 'error': message}) + '\n')
        print('::error::Contribution policy: ' + message)
        return 2


if __name__ == '__main__':
    sys.exit(main())
