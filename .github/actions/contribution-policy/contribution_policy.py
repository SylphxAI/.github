#!/usr/bin/env python3
"""Check immutable commit ranges and current pull request metadata."""
from __future__ import annotations

import argparse
from email.utils import parsedate_to_datetime
import importlib.util
import json
import os
from pathlib import Path
import re
import random
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[3]
POLICY = ROOT / 'policy/contribution-policy.json'
SHA = re.compile(r'[0-9a-f]{40}\Z')
GET_503_BACKOFF_SECONDS = (1, 2)
RETRY_AFTER_LIMIT_SECONDS = 30


class PolicyError(Exception):
    pass


def evaluate(pull_request, commits, policy, *, verified_github_commits=()):
    """Return value-free findings for REST-shaped metadata; never mutate input."""
    identities = {role: {(item['name'], item['email']) for item in policy['identities'] if role in item['roles']}
                  for role in ('author', 'committer')}
    github = policy['github_generated']
    github_authors = {(item['name'], item['email']) for item in github['authors']}
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
        # A queue squash is authored by the account that opened the pull request, so an
        # unapproved opener is refused here instead of after the merge queue has built it.
        opener = pull_request.get('user')
        if not isinstance(opener, dict) or not isinstance(opener.get('login'), str) or \
                not isinstance(opener.get('id'), int):
            raise PolicyError('pull request metadata is incomplete')
        openers = {(item['login'], item['id']) for item in policy['pull_request_openers']}
        if (opener['login'], opener['id']) not in openers:
            findings.append({'rule': 'unapproved-pr-opener', 'location': 'pull-request.user', 'line': 0})
    for item in commits:
        sha = item.get('sha', '')
        if not SHA.fullmatch(sha):
            raise PolicyError('commit metadata has no immutable identity')
        commit = item.get('commit', {})
        if not {'author', 'committer', 'message'} <= commit.keys():
            raise PolicyError('commit metadata is incomplete')
        committer = commit.get('committer') or {}
        github_committer = all(committer.get(key) == github['committer'][key] for key in ('name', 'email'))
        verified = github_committer and sha in verified_github_commits
        for role in ('author', 'committer'):
            identity = commit.get(role) or {}
            pair = (identity.get('name'), identity.get('email'))
            approved = pair in identities[role]
            if github_committer:
                approved = verified and (role == 'committer' or pair in github_authors)
            if not approved:
                findings.append({'rule': 'unapproved-' + role, 'location': sha, 'line': 0})
        text(commit.get('message'), sha)
    return findings


def metadata_retry_delay(retry_after, backoff):
    """Honor a server's minimum wait without extending the bounded GET budget."""
    delay = backoff + random.uniform(0, 1)
    if retry_after is not None:
        try:
            value = retry_after.strip()
            if re.fullmatch(r'[0-9]+', value):
                minimum = int(value)
            else:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None:
                    return None
                minimum = max(0, date.timestamp() - time.time())
        except (AttributeError, TypeError, ValueError, OverflowError):
            return None
        if minimum > RETRY_AFTER_LIMIT_SECONDS:
            return None
        delay = max(delay, minimum)
    return delay


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
        for attempt in range(len(GET_503_BACKOFF_SECONDS) + 1):
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                try:
                    body = json.loads(error.read(8192))
                    message = body.get('message', '') if isinstance(body, dict) else ''
                except (OSError, ValueError, TypeError):
                    message = ''
                finally:
                    error.close()
                headers = error.headers or {}
                rate_limited = headers.get('X-RateLimit-Remaining') == '0' or 'rate limit' in str(message).lower()
                if error.code == 429 or error.code == 403 and rate_limited:
                    raise PolicyError(f'GitHub metadata read failed (HTTP {error.code}: rate limit exceeded)') from None
                if request.get_method() == 'GET' and error.code == 503 and not rate_limited and \
                        attempt < len(GET_503_BACKOFF_SECONDS):
                    delay = metadata_retry_delay(headers.get('Retry-After'), GET_503_BACKOFF_SECONDS[attempt])
                    if delay is not None:
                        print('Contribution policy: GitHub metadata GET failed (HTTP 503); '
                              f'retrying request (attempt {attempt + 2}/{len(GET_503_BACKOFF_SECONDS) + 1}).')
                        time.sleep(delay)
                        continue
                pulls = re.search(r'/repos/[^/]+/[^/]+/(?:pulls(?:/|\?|$)|commits/[0-9a-f]{40}/pulls(?:\?|$))', url)
                queue = isinstance(data, dict) and 'mergeQueue' in data.get('query', '')
                if error.code == 403 and (pulls or queue) and message in {
                        'Resource not accessible by integration', 'Resource not accessible by personal access token'}:
                    raise PolicyError('caller workflow lacks pull-requests: read') from None
                raise PolicyError(f'GitHub metadata read failed (HTTP {error.code})') from None
            except (OSError, ValueError):
                raise PolicyError('GitHub metadata read failed') from None

    def get(self, path):
        repository_url = self.root + '/repos/' + self.repository
        return self.read(repository_url + ('/' + path if path else ''))

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

    def check_runs(self, head):
        """Read every canonical-name run; refuse a partial or moving inventory."""
        runs, total, page = [], None, 1
        while True:
            body = self.get(f'commits/{head}/check-runs?check_name=Sylphx%20PR%20Review&filter=all&per_page=100&page={page}')
            if not isinstance(body, dict):
                raise PolicyError('review Check Run response is invalid')
            count, rows = body.get('total_count'), body.get('check_runs')
            if type(count) is not int or count < 0 or not isinstance(rows, list) or len(rows) > 100 or any(not isinstance(row, dict) for row in rows):
                raise PolicyError('review Check Run pagination is invalid')
            if total is not None and total != count:
                raise PolicyError('review Check Run inventory changed during pagination')
            total = count
            runs.extend(rows)
            ids = [row.get('id') for row in runs]
            if any(type(value) is not int or value <= 0 for value in ids) or len(set(ids)) != len(ids) or len(runs) > total:
                raise PolicyError('review Check Run pagination is incomplete')
            if len(runs) == total:
                return runs
            if len(rows) < 100:
                raise PolicyError('review Check Run pagination is incomplete')
            page += 1

    def queue_entries(self, branch):
        query = '''query($owner:String!, $name:String!, $branch:String!, $cursor:String) {
          repository(owner:$owner, name:$name) { mergeQueue(branch:$branch) {
            entries(first:100, after:$cursor) {
              totalCount nodes { pullRequest { number headRefOid } headCommit { oid } }
              pageInfo { hasNextPage endCursor }
            }
          } }
        }'''
        owner, name = self.repository.split('/')
        cursor, entries, total, seen_cursors = None, [], None, set()
        while True:
            response = self.read(os.environ.get('GITHUB_GRAPHQL_URL', self.root + '/graphql'),
                                 {'query': query, 'variables': {'owner': owner, 'name': name,
                                  'branch': branch, 'cursor': cursor}})
            if response.get('errors'):
                if any(error.get('type') == 'RATE_LIMITED' for error in response['errors']):
                    raise PolicyError('GitHub merge queue metadata read failed: rate limit exceeded')
                if any(error.get('type') == 'FORBIDDEN' and error.get('message') ==
                       'Resource not accessible by integration' for error in response['errors']):
                    raise PolicyError('caller workflow lacks pull-requests: read')
                raise PolicyError('GitHub merge queue metadata is unreadable')
            queue = response['data']['repository']['mergeQueue']
            if queue is None:
                if total is not None:
                    raise PolicyError('GitHub merge queue pagination is incomplete or changed')
                return entries
            connection = queue['entries']
            count, rows = connection['totalCount'], connection['nodes']
            if type(count) is not int or count < 0 or not isinstance(rows, list) or \
                    len(rows) > 100 or total is not None and count != total:
                raise PolicyError('GitHub merge queue pagination is incomplete or changed')
            total = count
            entries.extend(rows)
            page = connection['pageInfo']
            if type(page['hasNextPage']) is not bool or len(entries) > total:
                raise PolicyError('GitHub merge queue pagination is incomplete')
            if not page['hasNextPage']:
                if len(entries) != total:
                    raise PolicyError('GitHub merge queue pagination is incomplete')
                return entries
            if not rows or not page['endCursor'] or page['endCursor'] in seen_cursors:
                raise PolicyError('GitHub merge queue pagination is incomplete')
            cursor = page['endCursor']
            seen_cursors.add(cursor)


def immutable_git_source():
    # Share the existing read-only, authenticated object acquisition path.
    spec = importlib.util.spec_from_file_location('contribution_fetch', ROOT / '.github/actions/secret-scan/secret_scan.py')
    source = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(source)
    return source


def range_commits(base, head, repository, token):
    if not SHA.fullmatch(base) or not SHA.fullmatch(head):
        raise PolicyError('event has no immutable commit range')
    source = immutable_git_source()
    server = os.environ.get('GITHUB_SERVER_URL', 'https://github.com').rstrip('/')
    with tempfile.TemporaryDirectory(prefix='contribution-policy-') as directory:
        repo = Path(directory)
        try:
            source.fetch(repo, server + '/' + repository + '.git', base, head, token, filter_spec='tree:0')
            data = source.git(repo, 'log', '-z', '--no-show-signature',
                              '--format=%H%x00%an%x00%ae%x00%cn%x00%ce%x00%B', base + '..' + head)
            commits = parse_commits(data)
            if commits:
                objects = source.command(['git', '-C', str(repo), 'cat-file', '--batch'],
                                         input=''.join(item['sha'] + '\n' for item in commits).encode())
                attach_commit_objects(commits, objects)
        except source.ScanError:
            raise PolicyError('immutable commit acquisition failed') from None
    return commits


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


def attach_commit_objects(commits, data):
    """Keep exact object bytes; pretty-printed log messages are not signed bytes."""
    offset = 0
    for item in commits:
        end = data.find(b'\n', offset)
        if end < 0:
            raise PolicyError('immutable commit object is incomplete')
        fields = data[offset:end].split()
        if len(fields) != 3 or fields[:2] != [item['sha'].encode(), b'commit'] or not fields[2].isdigit():
            raise PolicyError('immutable commit object is incomplete')
        start, size = end + 1, int(fields[2])
        raw = data[start:start + size]
        if len(raw) != size or data[start + size:start + size + 1] != b'\n' or b'\n\n' not in raw:
            raise PolicyError('immutable commit object is incomplete')
        item['raw_commit'] = raw
        item['commit']['message'] = raw.partition(b'\n\n')[2].decode('utf-8', errors='replace')
        offset = start + size + 1
    if offset != len(data):
        raise PolicyError('immutable commit object response has extra data')


def signing_payload(raw):
    """Git signs the complete commit object with only its gpgsig header removed."""
    if not isinstance(raw, bytes) or b'\n\n' not in raw:
        raise PolicyError('immutable signed commit object is unavailable')
    headers, separator, message = raw.partition(b'\n\n')
    kept, signature_header = [], False
    for line in headers.split(b'\n'):
        if not line.startswith(b' '):
            signature_header = line.startswith(b'gpgsig ')
        if not signature_header:
            kept.append(line)
    return b'\n'.join(kept) + separator + message


def verified_github_commits(api, commits, policy):
    """Bind GitHub's signature verdict and web-flow identity to immutable objects."""
    github = policy['github_generated']
    verified = set()
    for item in commits:
        local = item.get('commit', {})
        if not all((local.get('committer') or {}).get(key) == github['committer'][key]
                   for key in ('name', 'email')):
            continue
        remote = api.get('commits/' + item['sha'])
        metadata = remote.get('commit') or {}
        if remote.get('sha') != item['sha'] or any(
                (metadata.get(role) or {}).get(key) != (local.get(role) or {}).get(key)
                for role in ('author', 'committer') for key in ('name', 'email')):
            raise PolicyError('GitHub provenance does not match immutable commit metadata')
        signature = metadata.get('verification') or {}
        if (remote.get('committer') or {}).get('login') == github['committer']['login'] and \
                signature.get('verified') is True and signature.get('reason') == 'valid' and \
                isinstance(signature.get('signature'), str) and signature['signature'].strip() and \
                isinstance(signature.get('payload'), str) and signature['payload'].strip():
            # REST's display message can omit a terminal LF. The signed payload
            # preserves every byte, including the complete headers and message.
            if signing_payload(item.get('raw_commit')) != signature['payload'].encode('utf-8'):
                raise PolicyError('GitHub provenance does not match immutable commit metadata')
            verified.add(item['sha'])
    return verified


def dispatch_target(api, event, sha):
    """Bind an explicit PR-code dispatch to its immutable merge and current PR."""
    inputs = event.get('inputs') or {}
    merge, head, base = (inputs.get('code-' + part, '') for part in ('merge', 'head', 'base'))
    number, branch = inputs.get('code-pr', ''), inputs.get('code-base-ref', '')
    if not all(isinstance(value, str) and SHA.fullmatch(value) and set(value) != {'0'}
               for value in (merge, head, base)) or head != sha or \
            not isinstance(number, str) or not re.fullmatch(r'[1-9][0-9]*', number) or not branch:
        raise PolicyError('dispatched code evidence has no valid PR identity and immutable range')
    parents = api.get('git/commits/' + merge).get('parents', [])
    if [parent['sha'] for parent in parents] != [base, head]:
        raise PolicyError('dispatched code merge does not match its base and head')
    current = api.get('pulls/' + number)
    if current.get('state') != 'open' or current['head']['sha'] != head or \
            current['base']['sha'] != base or current['base']['ref'] != branch:
        raise PolicyError('dispatched PR target changed; code CI must validate its current base')
    return current, base, head


def pull_snapshot(api, number, expected_head=None, *, check_text=True, expected_base=None):
    before = api.get(f'pulls/{number}')
    head = before['head']['sha']
    if expected_head and head != expected_head:
        raise PolicyError('pull request head changed; run the check on its current head')
    commits = range_commits(before['base']['sha'], head, api.repository, api.token)
    after = api.get(f'pulls/{number}')
    if expected_base and any(pr['base']['sha'] != expected_base[0] or
                             pr['base']['ref'] != expected_base[1] or pr.get('state') != 'open'
                             for pr in (before, after)):
        raise PolicyError('dispatched PR target changed during code validation')
    for key in ('title', 'body') if check_text else ():
        if before[key] != after[key]:
            raise PolicyError('pull request metadata changed during the check; rerun it')
    if before['head']['sha'] != after['head']['sha'] or before['base']['ref'] != after['base']['ref']:
        raise PolicyError('pull request revision changed during the check; rerun it')
    return after, commits


def review_findings(api, pr, commits):
    """Only an exact-head, completed success by the review App admits [review]."""
    number, head = pr['number'], pr['head']['sha']
    if not isinstance(pr.get('title'), str) or not isinstance(head, str) or not SHA.fullmatch(head):
        raise PolicyError('pull request review identity is incomplete')
    if not pr['title'].startswith('[review]'):
        return []
    location = f'PR #{number} head {head}'

    def canonical(sha):
        runs = api.check_runs(sha)
        if any(not isinstance(run.get('app'), dict) for run in runs):
            raise PolicyError('review Check Run App identity is incomplete')
        return sorted((run for run in runs
                       if run.get('name') == 'Sylphx PR Review' and run['app'].get('id') == 1299545),
                      key=lambda run: run['id'], reverse=True)

    try:
        runs = canonical(head)
        state = 'missing'
        if runs:
            latest = runs[0]
            if latest.get('head_sha') != head:
                state = ('success on an older head ' + str(latest.get('head_sha'))
                         if latest.get('conclusion') == 'success' else 'wrong head')
            elif latest.get('status') != 'completed':
                state = 'pending'
            else:
                state = latest.get('conclusion') or 'missing conclusion'
        else:
            # Diagnostic only: an older success can never authorize this head.
            for sha in reversed([item['sha'] for item in commits if item['sha'] != head]):
                older = canonical(sha)
                if older and older[0].get('head_sha') == sha and \
                        older[0].get('status') == 'completed' and older[0].get('conclusion') == 'success':
                    state = 'success on an older head ' + sha
                    break
        after = api.get(f'pulls/{number}')
        if after['head']['sha'] != head or after['title'] != pr['title'] or after['base']['ref'] != pr['base']['ref']:
            state = 'PR head or metadata changed during review validation'
        if state == 'success':
            return []
    except (PolicyError, KeyError, TypeError, ValueError) as error:
        state = 'API read error: ' + (str(error) if isinstance(error, PolicyError) else 'incomplete review metadata')
    return [{'rule': 'exact-head-review: ' + state, 'location': location, 'line': 0}]


def squash_merge_tree(repository, token, parent, head):
    """Reconstruct one position without checkout, retaining auth for lazy blob reads."""
    source = immutable_git_source()
    server = os.environ.get('GITHUB_SERVER_URL', 'https://github.com').rstrip('/')
    with tempfile.TemporaryDirectory(prefix='review-tree-') as directory:
        repo = Path(directory)
        try:
            env = source.fetch(repo, server + '/' + repository + '.git', parent, head, token)
        except (source.ScanError, OSError):
            raise PolicyError('squash tree proof object acquisition failed') from None
        try:
            result = subprocess.run(['git', '-C', str(repo), 'merge-tree', '--write-tree', '--no-messages',
                                     parent, head], env=env, capture_output=True, timeout=60)
        except subprocess.TimeoutExpired:
            raise PolicyError('squash tree proof timed out after 60s') from None
        except OSError:
            raise PolicyError('squash tree proof could not execute Git') from None
        # Neither Git diagnostics nor conflict contents may enter the check output.
        if result.returncode == 1:
            raise PolicyError('squash tree proof conflicts')
        tree = result.stdout.decode('ascii', errors='replace').strip()
        if result.returncode or not SHA.fullmatch(tree):
            raise PolicyError('squash tree proof failed')
        return tree


def bind_review_head(api, pr, synthetic, base, group_shas):
    """Bind the reviewed content to its immutable position, not live queue state."""
    if not isinstance(pr.get('title'), str):
        raise PolicyError('pull request review identity is incomplete')
    if not pr['title'].startswith('[review]'):
        return
    head = pr['head']['sha']
    location = f"PR #{pr['number']} head {head}"
    raw = synthetic.get('raw_commit')
    headers = raw.partition(b'\n\n')[0].decode('ascii', errors='replace').splitlines() if isinstance(raw, bytes) else []
    trees = [line[5:] for line in headers if line.startswith('tree ')]
    parents = [line[7:] for line in headers if line.startswith('parent ')]
    if len(trees) != 1 or len(parents) not in (1, 2) or not all(SHA.fullmatch(sha) for sha in trees + parents):
        raise PolicyError(location + ': immutable merge-group tree/parents are incomplete or unsupported')
    if parents[0] != base and parents[0] not in group_shas:
        raise PolicyError(location + ': immutable merge-group position is incomplete')
    if len(parents) == 2:
        if parents[1] != head:
            raise PolicyError(location + ': membership/head changed during evaluation; '
                              f'merge-group parent pins {parents[1]}')
        return
    try:
        tree = squash_merge_tree(api.repository, api.token, parents[0], head)
    except PolicyError as error:
        raise PolicyError(location + ': ' + str(error)) from None
    if tree != trees[0]:
        raise PolicyError(location + f": squash tree mismatch at merge-group commit {synthetic['sha']}")


def merge_group_snapshot(api, group, *, check_pull_text=True, require_head_binding=False):
    """Resolve every group PR once; bind review heads only on the opted-in path."""
    def snapshot(number, head, synthetic):
        pr, original = (pull_snapshot(api, number, head) if check_pull_text else
                        pull_snapshot(api, number, head, check_text=False))
        if require_head_binding:
            bind_review_head(api, pr, by_sha[synthetic], group['base_sha'], group_shas)
        return pr, original

    commits = range_commits(group['base_sha'], group['head_sha'], api.repository, api.token)
    snapshots, covered = [], set()
    by_sha = {commit['sha']: commit for commit in commits}
    group_shas = set(by_sha)
    if not group_shas:
        raise PolicyError('merge group membership is incomplete: empty commit range')
    branch = group['base_ref'].removeprefix('refs/heads/')
    queued = group_queue_members(api, branch, group_shas)
    for number, (head, synthetic) in queued.items():
        pr, original = snapshot(number, head, synthetic)
        covered.add(synthetic)
        covered.update(commit['sha'] for commit in original)
        snapshots.append((pr, original))
    for commit in commits:
        if commit['sha'] in covered:
            continue
        # Earlier entries can merge while a later group is still building.
        for pr in api.pages(f"commits/{commit['sha']}/pulls"):
            if pr['base']['ref'] == branch and pr.get('merged_at') and \
                    pr.get('merge_commit_sha') == commit['sha']:
                current, original = snapshot(pr['number'], pr['head']['sha'], commit['sha'])
                covered.add(commit['sha'])
                covered.update(item['sha'] for item in original)
                snapshots.append((current, original))
    if group_shas - covered:
        raise PolicyError('merge group membership is incomplete')
    return commits, snapshots, queued


def group_queue_members(api, branch, group_shas):
    members = {}
    synthetic_heads = set()
    for entry in api.queue_entries(branch):
        synthetic = (entry.get('headCommit') or {}).get('oid')
        if synthetic not in group_shas:
            continue
        pr = entry['pullRequest']
        number, head = pr['number'], pr['headRefOid']
        if type(number) is not int or number <= 0 or not isinstance(head, str) or not SHA.fullmatch(head):
            raise PolicyError('merge group member identity is incomplete')
        if number in members or synthetic in synthetic_heads:
            raise PolicyError('merge group membership is duplicated')
        members[number] = (head, synthetic)
        synthetic_heads.add(synthetic)
    return members


def recheck_merge_group(api, group, commits, snapshots, queued):
    """Refuse membership, head or title churn across the review inventory reads."""
    branch = group['base_ref'].removeprefix('refs/heads/')
    after = group_queue_members(api, branch, {commit['sha'] for commit in commits})
    changed = {number for number in queued.keys() | after.keys() if queued.get(number) != after.get(number)}
    expected = {pr['number']: pr for pr, _ in snapshots}
    for number, pr in expected.items():
        current = api.get(f'pulls/{number}')
        if current['head']['sha'] != pr['head']['sha'] or current['title'] != pr['title'] or \
                current['base']['ref'] != pr['base']['ref']:
            changed.add(number)
    return [{'rule': 'exact-head-review: membership/head changed during evaluation',
             'location': f"PR #{number} head " + (expected[number]['head']['sha'] if number in expected else after[number][0]),
             'line': 0} for number in sorted(changed)]


def check_event(api, event_name, event, sha, policy, *, check_pull_text=True,
                require_merge_group_review=False):
    def verdict(pr, commits):
        return evaluate(pr, commits, policy,
                        verified_github_commits=verified_github_commits(api, commits, policy))

    def snapshot(number, head):
        if check_pull_text:
            pr, commits = pull_snapshot(api, number, head)
        else:
            pr, commits = pull_snapshot(api, number, head, check_text=False)
        return pr if check_pull_text else None, commits

    if event_name in {'pull_request', 'pull_request_target'}:
        pr = event.get('pull_request', {})
        return verdict(*snapshot(event.get('number') or pr['number'], pr['head']['sha']))
    if event_name == 'merge_group':
        commits, snapshots, queued = merge_group_snapshot(api, event['merge_group'], check_pull_text=check_pull_text,
                                                          require_head_binding=require_merge_group_review)
        findings = []
        for pr, original in snapshots:
            findings.extend(verdict(pr if check_pull_text else None, original))
            if require_merge_group_review:
                findings.extend(review_findings(api, pr, original))
        findings.extend(verdict(None, commits))
        if require_merge_group_review:
            findings.extend(recheck_merge_group(api, event['merge_group'], commits, snapshots, queued))
        return findings
    if event_name == 'workflow_dispatch':
        inputs = event.get('inputs') or {}
        if any(inputs.get('code-' + part) for part in ('merge', 'head', 'base', 'pr', 'base-ref')):
            target, base, head = dispatch_target(api, event, sha)
            pr, commits = pull_snapshot(api, target['number'], head, check_text=check_pull_text,
                                        expected_base=(base, inputs['code-base-ref']))
            return verdict(pr if check_pull_text else None, commits)
        # A dispatch on a PR head must not replace a failed PR verdict with green.
        pulls = [pr for pr in api.pages('pulls?state=open') if pr['head']['sha'] == sha]
        findings = []
        for pr in pulls:
            findings.extend(verdict(*snapshot(pr['number'], sha)))
        return findings
    return []


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, help='Offline JSON with pull_request and commits fields')
    parser.add_argument('--report', type=Path)
    parser.add_argument('--scope', choices=('all', 'commits'), default='all',
                        help='commits is for code verdicts with a separately required PR metadata producer')
    parser.add_argument('--require-merge-group-review', action='store_true',
                        help='require exact-head review success for every [review] merge-group PR')
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
                                   check_pull_text=args.scope == 'all',
                                   require_merge_group_review=args.require_merge_group_review)
        if args.report:
            args.report.write_text(json.dumps({'passed': not findings, 'findings': findings}, indent=2) + '\n')
        for finding in findings:
            print('::error::Contribution policy: {rule} at {location}:{line}'.format(**finding))
        if any(finding['rule'] == 'unapproved-pr-opener' for finding in findings):
            print('Contribution policy: the merge queue authors its squash commit as the pull request opener; '
                  'reopen this branch as an approved account.')
        print(f'Contribution policy: {len(findings)} finding(s).')
        return int(bool(findings))
    except (PolicyError, KeyError, ValueError, OSError, TypeError) as error:
        message = str(error) if isinstance(error, PolicyError) else 'metadata or policy could not be read'
        if args.report:
            args.report.write_text(json.dumps({'passed': False, 'findings': [], 'error': message}) + '\n')
        print('::error::Contribution policy: ' + message)
        return 2


if __name__ == '__main__':
    sys.exit(main())
