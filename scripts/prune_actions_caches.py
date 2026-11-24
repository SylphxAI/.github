#!/usr/bin/env python3
"""Plan or apply bounded retention for GitHub Actions caches in one repository."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
from http.client import HTTPSConnection, HTTPException
import json
import os
import re
import sys
from time import monotonic, sleep
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

REPO = re.compile(r'SylphxAI/(?!\.{1,2}$)[a-zA-Z0-9_.-]+', re.IGNORECASE)
PR = re.compile(r'refs/pull/([1-9][0-9]*)/merge')
QUEUE = re.compile(r'refs/heads/gh-readonly-queue/.+/pr-([1-9][0-9]*)-[0-9a-f]+')
HEADS = 'refs/heads/'
API = 'api.github.com'
RESERVE = 100
MAX_DELETE = 250
MAX_PAGES = 500


class Stop(RuntimeError):
    """Unknown or unsafe state: issue no more writes."""


def emit(event, **fields):
    print(json.dumps(dict(event=event, **fields)), flush=True)


class GitHub:
    def __init__(self, token, connect=HTTPSConnection, clock=monotonic, wait=sleep):
        self.headers = {'Authorization': f'Bearer {token}', 'User-Agent': 'actions-cache-pruner',
                        'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'}
        self.connect = connect
        self.clock = clock
        self.wait = wait
        self.deadline = clock() + 900
        self.remaining = None
        self.last_write = float('-inf')

    def request(self, path, *, delete=False):
        if not path.startswith('/') or path.startswith('//'):
            raise Stop('API path is not origin-relative')
        if delete:
            self.wait(max(0, 1 - (self.clock() - self.last_write)))
        timeout = min(15, self.deadline - self.clock())
        if timeout <= 0 or (self.remaining is not None and self.remaining <= RESERVE):
            raise Stop('request time budget or GitHub API quota reserve reached; no retry')
        method = 'DELETE' if delete else 'GET'
        connection = self.connect(API, timeout=timeout)
        try:
            if delete:
                self.last_write = self.clock()
            connection.request(method, path, headers=self.headers)
            response = connection.getresponse()
            status = response.status
            receipt = response.getheader('x-github-request-id')
            remaining = response.getheader('x-ratelimit-remaining', '')
            retry_after = response.getheader('retry-after')
            if delete and status == 204:
                # Record acknowledgement even if quota metadata prevents further requests.
                self.remaining = int(remaining) if (retry_after is None and remaining.isascii()
                                                    and remaining.isdecimal()) else 0
                return receipt
            if status in (403, 429) or retry_after is not None:
                raise Stop(f'{method} {path}: HTTP {status} (refused/throttled); '
                           f'remaining={remaining}, retry-after={retry_after}, request-id={receipt}; no retry')
            if status != 200 or delete:
                raise Stop(f'{method} {path}: unexpected HTTP {status}, request-id={receipt}; no retry')
            if not remaining.isascii() or not remaining.isdecimal():
                raise Stop(f'{method} {path}: missing or invalid quota header; no retry')
            self.remaining = int(remaining)
            body = response.read((8 << 20) + 1)
            if len(body) > 8 << 20:
                raise Stop(f'{method} {path}: response exceeds size bound')
            return json.loads(body), response.getheader('link', '')
        except (OSError, HTTPException, ValueError) as error:
            raise Stop(f'{method} {path}: {type(error).__name__}; reconcile exact ID before retry') from None
        finally:
            connection.close()


def timestamp(value):
    if not isinstance(value, str):
        raise Stop('invalid cache last_accessed_at')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise Stop('missing timezone on cache last_accessed_at')
    return result


def cache_rows(body):
    rows = body['actions_caches']
    if not isinstance(rows, list):
        raise Stop('invalid cache page')
    for row in rows:
        if (type(row['id']) is not int or row['id'] < 1 or type(row['size_in_bytes']) is not int
                or row['size_in_bytes'] < 0 or not isinstance(row['ref'], str)
                or not isinstance(row['key'], str) or not row['key']):
            raise Stop('invalid cache identity or size')
        timestamp(row['last_accessed_at'])
    return rows


class Repository:
    def __init__(self, api, name):
        if not REPO.fullmatch(name):
            raise Stop('target is not an approved SylphxAI repository')
        self.api = api
        self.name = name
        self.path = '/repos/' + name
        self.id = None

    def default_branch(self):
        meta, _ = self.api.request(self.path)
        if (meta['full_name'].lower() != self.name.lower() or type(meta['id']) is not int
                or meta['id'] < 1 or (self.id is not None and meta['id'] != self.id)
                or not isinstance(meta['default_branch'], str) or not meta['default_branch']):
            raise Stop('repository identity or default branch could not be verified')
        self.id = meta['id']
        return meta['default_branch']

    def caches(self, **filters):
        base = self.path + '/actions/caches'
        query = dict(per_page=100, sort='created_at', direction='asc', **filters)
        stable_paths = {base.lower(), f'/repositories/{self.id}/actions/caches'}
        seen = set()
        rows = []
        for page in range(1, MAX_PAGES + 1):
            body, link = self.api.request(f'{base}?{urlencode(dict(query, page=page))}')
            for row in cache_rows(body):
                if row['id'] in seen:
                    raise Stop('duplicate cache ID across pages; inventory changed, take a fresh snapshot')
                seen.add(row['id'])
                rows.append(row)
            next_links = re.findall(r'<([^>]+)>;\s*rel="next"', link)
            if not next_links:
                count = body['total_count']
                if type(count) is not int or count != len(rows):
                    raise Stop('cache inventory count changed or pagination is incomplete')
                return rows
            if len(next_links) != 1:
                raise Stop('ambiguous pagination')
            next_url = urlsplit(next_links[0])
            # GitHub commonly switches /repos/name to /repositories/id. Never follow
            # foreign destinations or changed filters; construct the next query ourselves.
            if (next_url.scheme != 'https' or next_url.netloc != API
                    or next_url.path.lower() not in stable_paths or next_url.fragment
                    or sorted(parse_qsl(next_url.query)) != sorted(
                        (k, str(v)) for k, v in dict(query, page=page + 1).items())):
                raise Stop('unexpected GitHub cache pagination destination')
        raise Stop('cache page limit reached')

    def pr_state(self, number):
        pull, _ = self.api.request(self.path + f'/pulls/{number}')
        if (pull['number'] != int(number) or pull['base']['repo']['id'] != self.id
                or pull['state'] not in ('open', 'closed')):
            raise Stop('pull request identity or state could not be verified')
        return pull['state']

    def branch_exists(self, ref):
        # A 404 can hide missing Contents access. Only a successful matching-refs
        # response (which can contain prefix matches) establishes absence.
        rows, link = self.api.request(self.path + '/git/matching-refs/' + quote(ref[5:], safe='/'))
        if link or not isinstance(rows, list) or any(not isinstance(row['ref'], str) or
                                                    not row['ref'].startswith(ref) for row in rows):
            raise Stop('branch lookup is not authoritative')
        return any(row['ref'] == ref for row in rows)


def eligible(row, default, cutoff, pr_state, branch_exists):
    ref = row['ref']
    pull = PR.fullmatch(ref)
    if pull:
        return 'closed-pr' if pr_state(pull[1]) == 'closed' else None
    if not ref.startswith(HEADS) or ref == HEADS or ref[len(HEADS):] in ('main', 'master', default):
        return None
    # Queue refs are branches, but an open PR may still depend on them.
    queue = QUEUE.fullmatch(ref)
    if ref.startswith(HEADS + 'gh-readonly-queue/') and (not queue or pr_state(queue[1]) != 'closed'):
        return None
    if timestamp(row['last_accessed_at']) < cutoff:
        return 'stale-branch'
    if not branch_exists(ref):
        return 'absent-branch'
    return None


def plan(repo, default, rows, cutoff, limit):
    states, branches = {}, {}

    def once(cache, read, key):
        if key not in cache:
            cache[key] = read(key)
        return cache[key]

    reasons = Counter()
    chosen = []
    for row in sorted(rows, key=lambda row: (-row['size_in_bytes'], row['id'])):
        reason = eligible(row, default, cutoff,
                          lambda n: once(states, repo.pr_state, n),
                          lambda r: once(branches, repo.branch_exists, r))
        reasons[reason or 'keep'] += 1
        if reason and len(chosen) < limit:
            chosen.append((row, reason))
    emit('plan', repo=repo.name, default_branch=default, inspected=len(rows), reasons=reasons,
         candidates=len(chosen), bytes=sum(row['size_in_bytes'] for row, _ in chosen))
    return chosen


def apply(repo, candidates, cutoff):
    deleted = total_bytes = 0
    for planned, _ in candidates:
        ref, key, cache_id = planned['ref'], planned['key'], planned['id']
        rows = [row for row in repo.caches(ref=ref, key=key) if row['id'] == cache_id]
        if not rows:
            emit('skip', repo=repo.name, id=cache_id, reason='already absent from exact ref+key')
            continue
        row = rows[0]
        if (row['ref'], row['key'], row['size_in_bytes']) != (ref, key, planned['size_in_bytes']):
            raise Stop('planned cache identity or size changed')
        default = repo.default_branch()
        reason = eligible(row, default, cutoff, repo.pr_state, repo.branch_exists)
        if not reason or row['last_accessed_at'] != planned['last_accessed_at']:
            emit('skip', repo=repo.name, id=cache_id, reason='retention eligibility or access time changed')
            continue
        path = f'/repositories/{repo.id}/actions/caches/{cache_id}'
        emit('delete-attempt', repo=repo.name, id=cache_id, ref=ref, bytes=row['size_in_bytes'],
             last_accessed_at=row['last_accessed_at'], reason=reason, path=path)
        receipt = repo.api.request(path, delete=True)
        emit('deleted', repo=repo.name, id=cache_id, ref=ref, bytes=row['size_in_bytes'], request_id=receipt)
        deleted += 1
        total_bytes += row['size_in_bytes']
    emit('applied', repo=repo.name, deleted=deleted, bytes=total_bytes)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('repository', help='explicit SylphxAI/name; no org-wide discovery')
    parser.add_argument('--apply', action='store_true', help='perform deletes (default: dry-run)')
    parser.add_argument('--limit', type=int, default=50, help=f'delete at most this many caches (max {MAX_DELETE})')
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= MAX_DELETE:
        parser.error('limit out of bounds')
    token = os.environ.get('GH_TOKEN')
    if not token:
        parser.error('GH_TOKEN is required')
    try:
        repo = Repository(GitHub(token), args.repository)
        cutoff = datetime.now(timezone.utc) - timedelta(days=3)
        default = repo.default_branch()
        candidates = plan(repo, default, repo.caches(), cutoff, args.limit)
        for row, reason in candidates:
            emit('candidate', repo=repo.name, id=row['id'], ref=row['ref'], bytes=row['size_in_bytes'],
                 last_accessed_at=row['last_accessed_at'], reason=reason)
        if args.apply:
            apply(repo, candidates, cutoff)
    except (Stop, KeyError, ValueError, TypeError, AttributeError) as error:
        print(f'Cache prune stopped: {error}', file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
