import contextlib
from datetime import datetime, timezone
from http.client import RemoteDisconnected
from io import StringIO
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import prune_actions_caches as prune

NOW = datetime(2026, 11, 24, tzinfo=timezone.utc)
OLD = '2026-11-20T00:00:00Z'
FRESH = '2026-11-23T00:00:00Z'
BOUNDARY = '2026-11-21T00:00:00Z'
ROOT = '/repos/SylphxAI/example'
META = dict(full_name='SylphxAI/example', id=42, default_branch='trunk')
QUERY = dict(per_page=100, sort='created_at', direction='asc')


def cache(i, ref, at=OLD):
    return dict(id=i, ref=ref, last_accessed_at=at, size_in_bytes=1000 + i, key='build ' + str(i))


def caches_path(page=1, **filters):
    return ROOT + '/actions/caches?' + urlencode(dict(QUERY, **filters, page=page))


def pull(number, state):
    return ('GET', ROOT + f'/pulls/{number}', 200,
            dict(number=number, state=state, base=dict(repo=dict(id=META['id']))), {})


def cache_page(rows, page=1, headers=None, total=None, **filters):
    return ('GET', caches_path(page, **filters), 200,
            dict(total_count=len(rows) if total is None else total, actions_caches=rows), headers or {})


def repo_meta(meta=META):
    return ('GET', ROOT, 200, meta, {})


class Response:
    def __init__(self, status, payload, headers):
        self.status = status
        self.payload = payload
        self.headers = {'x-ratelimit-remaining': '500', **headers}

    def getheader(self, key, default=None):
        return self.headers.get(key, default)

    def read(self, _amount):
        return json.dumps(self.payload).encode()


class Wire:
    def __init__(self, test, steps):
        self.test, self.steps = test, list(steps)
        self.writes = []
        self.time = 0
        self.waits = []

    def connect(self, host, timeout):
        self.test.assertEqual(host, 'api.github.com')
        self.test.assertGreater(timeout, 0)
        return self

    def request(self, method, path, headers):
        self.test.assertEqual(headers['Authorization'], 'Bearer fixture')
        self.test.assertTrue(self.steps, f'unexpected {method} {path}')
        expected_method, expected_path, *self.reply = self.steps.pop(0)
        self.test.assertEqual((method, path), (expected_method, expected_path))
        if method == 'DELETE':
            self.writes.append(path)

    def getresponse(self):
        if self.reply[0] is None:
            raise RemoteDisconnected()
        return Response(*self.reply)

    def close(self):
        pass

    def wait(self, delay):
        self.waits.append(delay)
        self.time += delay


class CacheRetentionTests(unittest.TestCase):
    def run_cli(self, steps, *args):
        wire = Wire(self, steps)
        api = prune.GitHub('fixture', connect=wire.connect, clock=lambda: wire.time, wait=wire.wait)
        output, errors = StringIO(), StringIO()
        with patch.object(prune, 'GitHub', return_value=api), patch.dict('os.environ', GH_TOKEN='fixture'), \
             patch.object(prune, 'datetime', wraps=datetime) as clock, \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            clock.now.return_value = NOW
            status = prune.main(['SylphxAI/example', *args])
        self.assertFalse(wire.steps, 'expected requests were not reached')
        return status, [json.loads(line) for line in output.getvalue().splitlines()], errors.getvalue(), wire

    def test_dry_run_protects_main_master_true_default_open_pr_queue_and_tags(self):
        refs = ['refs/heads/main', 'refs/heads/master', 'refs/heads/trunk',
                'refs/pull/7/merge', 'refs/heads/gh-readonly-queue/main/pr-7-abc',
                'refs/tags/v1', 'refs/pull/8/head']
        protected = [cache(i, ref) for i, ref in enumerate(refs, 1)]
        eligible = cache(9, 'refs/pull/9/merge')
        status, events, _, wire = self.run_cli([
            repo_meta(), cache_page([*protected, eligible]), pull(9, 'closed'), pull(7, 'open')])
        self.assertEqual(status, 0)
        self.assertEqual(events[0]['reasons'], {'closed-pr': 1, 'keep': 7})
        self.assertEqual([event['id'] for event in events[1:]], [9])
        self.assertEqual(wire.writes, [])

    def test_closed_pr_and_stale_branch_delete_only_exact_ids_and_pace(self):
        pr = cache(2, 'refs/pull/2/merge', FRESH)
        stale = cache(1, 'refs/heads/old/topic')
        close = pull(2, 'closed')
        a = '/repositories/42/actions/caches/2'
        b = '/repositories/42/actions/caches/1'
        status, events, _, wire = self.run_cli([
            repo_meta(), cache_page([stale, pr]), close,
            cache_page([pr], ref=pr['ref'], key=pr['key']), repo_meta(), close,
            ('DELETE', a, 204, None, {'x-github-request-id': 'A'}),
            cache_page([stale], ref=stale['ref'], key=stale['key']), repo_meta(),
            ('DELETE', b, 204, None, {'x-github-request-id': 'B'}),
        ], '--apply')
        self.assertEqual(status, 0)
        self.assertEqual(wire.writes, [a, b])
        self.assertEqual(wire.waits, [0, 1])
        self.assertEqual([e['request_id'] for e in events if e['event'] == 'deleted'], ['A', 'B'])
        self.assertEqual(events[-1]['bytes'], 2003)

    def test_recent_branch_requires_positive_absence_and_exact_age_boundary_is_kept(self):
        active = cache(2, 'refs/heads/active', BOUNDARY)
        gone = cache(1, 'refs/heads/gone', FRESH)
        status, events, _, wire = self.run_cli([
            repo_meta(), cache_page([active, gone]),
            ('GET', ROOT + '/git/matching-refs/heads/active', 200, [{'ref': active['ref']}], {}),
            ('GET', ROOT + '/git/matching-refs/heads/gone', 200, [{'ref': 'refs/heads/gone-other'}], {}),
        ])
        self.assertEqual(status, 0)
        self.assertEqual(events[0]['reasons'], {'keep': 1, 'absent-branch': 1})
        self.assertEqual(wire.writes, [])

    def test_reopened_pr_default_branch_change_and_access_change_keep_candidates(self):
        pr = cache(3, 'refs/pull/3/merge')
        renamed = cache(2, 'refs/heads/new-default')
        accessed = cache(1, 'refs/heads/recently-used')
        status, events, _, wire = self.run_cli([
            repo_meta(), cache_page([accessed, renamed, pr]), pull(3, 'closed'),
            cache_page([pr], ref=pr['ref'], key=pr['key']), repo_meta(), pull(3, 'open'),
            cache_page([renamed], ref=renamed['ref'], key=renamed['key']),
            repo_meta(dict(META, default_branch='new-default')),
            cache_page([dict(accessed, last_accessed_at='2026-11-20T10:00:00Z')],
                       ref=accessed['ref'], key=accessed['key']),
            repo_meta(dict(META, default_branch='new-default')),
        ], '--apply')
        self.assertEqual(status, 0)
        self.assertEqual(wire.writes, [])
        self.assertEqual([e['id'] for e in events if e['event'] == 'skip'], [3, 2, 1])

    def test_pagination_follows_github_repo_id_next_and_plans_largest(self):
        url = 'https://api.github.com/repositories/42/actions/caches?' + urlencode(dict(QUERY, page=2))
        a, b = cache(1, 'refs/heads/topic'), cache(2, 'refs/heads/topic')
        status, events, _, wire = self.run_cli([
            repo_meta(), cache_page([a], headers={'link': f'<{url}>; rel="next"'}, total=2),
            cache_page([b], page=2, total=2),
        ], '--limit', '1')
        self.assertEqual(status, 0)
        self.assertEqual(events[0]['inspected'], 2)
        self.assertEqual(events[1]['id'], 2)
        self.assertEqual(wire.writes, [])

    def test_duplicate_or_foreign_next_link_stops_before_any_delete(self):
        a = cache(1, 'refs/heads/topic')
        for host, message in [('api.github.com', 'duplicate cache ID'), ('example.org', 'pagination destination')]:
            with self.subTest(host=host):
                link = 'https://' + host + caches_path(2)
                steps = [repo_meta(), cache_page([a], headers={'link': f'<{link}>; rel="next"'})]
                if host == 'api.github.com':
                    steps.append(cache_page([a], page=2))
                status, _, errors, wire = self.run_cli(steps, '--apply')
                self.assertEqual(status, 1)
                self.assertIn(message, errors)
                self.assertEqual(wire.writes, [])

    def test_incomplete_listing_stops_before_any_delete(self):
        a = cache(1, 'refs/heads/topic')
        status, _, errors, wire = self.run_cli([repo_meta(), cache_page([a], total=2)], '--apply')
        self.assertEqual(status, 1)
        self.assertIn('pagination is incomplete', errors)
        self.assertEqual(wire.writes, [])

    def test_primary_quota_reserve_stops_before_delete(self):
        a = cache(1, 'refs/heads/topic')
        nearly_out = ('GET', ROOT, 200, META, {'x-ratelimit-remaining': str(prune.RESERVE)})
        status, errors, wire = self.rate_or_loss(a, nearly_out)
        self.assertEqual(status, 1)
        self.assertEqual(wire.writes, [])
        self.assertIn('quota reserve', errors)

    def rate_or_loss(self, a, *end):
        steps = [repo_meta(), cache_page([a]), cache_page([a], ref=a['ref'], key=a['key'])]
        status, _, errors, wire = self.run_cli([*steps, *end], '--apply')
        return status, errors, wire

    def test_secondary_rate_limit_or_lost_ack_never_retries_delete(self):
        a = cache(1, 'refs/heads/topic')
        path = '/repositories/42/actions/caches/1'
        for response, message in [(429, 'retry-after=61'), (403, 'retry-after=61'), (None, 'RemoteDisconnected')]:
            with self.subTest(response=response):
                status, errors, wire = self.rate_or_loss(
                    a, repo_meta(), ('DELETE', path, response, None, {'retry-after': '61'}))
                self.assertEqual(status, 1)
                self.assertIn(message, errors)
                self.assertEqual(wire.writes, [path])

    def test_branch_404_is_not_evidence_of_absence(self):
        a = cache(1, 'refs/heads/topic', FRESH)
        status, _, errors, wire = self.run_cli([
            repo_meta(), cache_page([a]), ('GET', ROOT + '/git/matching-refs/heads/topic', 404, None, {})
        ], '--apply')
        self.assertEqual(status, 1)
        self.assertIn('HTTP 404', errors)
        self.assertEqual(wire.writes, [])


if __name__ == '__main__':
    unittest.main()
