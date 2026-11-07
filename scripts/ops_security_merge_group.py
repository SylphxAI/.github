#!/usr/bin/env python3
"""Trusted-main projection of App-bound exact-head operator approvals."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys

# actions/checkout selects SylphxAI/.github main, never the candidate tree.
ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'ops_security_contribution_policy', ROOT / '.github/actions/contribution-policy/contribution_policy.py')
policy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(policy)
PUBLISH_SPEC = importlib.util.spec_from_file_location('operator_check_publisher', ROOT / 'scripts/operator_review_check.py')
publisher_module = importlib.util.module_from_spec(PUBLISH_SPEC)
PUBLISH_SPEC.loader.exec_module(publisher_module)


APP_ID = 1299545
CHECK_NAME = 'ops-security/review'
REPOSITORY = 'SylphxAI/.github'
REPOSITORY_ID = 1091169653


class GitHub(policy.GitHub):
    def operator_checks(self, head):
        """Complete Check Run history; statuses and Actions lookalikes have no authority."""
        runs, page, total = [], 1, None
        while True:
            body = self.get(f'commits/{head}/check-runs?check_name=ops-security%2Freview&filter=all&per_page=100&page={page}')
            rows = body.get('check_runs') if isinstance(body, dict) else None
            count = body.get('total_count') if isinstance(body, dict) else None
            if type(count) is not int or count < 0 or not isinstance(rows, list) or len(rows) > 100 or \
                    any(not isinstance(row, dict) for row in rows) or total is not None and total != count:
                raise policy.PolicyError('operator Check Run inventory is incomplete or changed')
            total = count
            runs.extend(rows)
            ids = [row.get('id') for row in runs]
            if any(type(value) is not int or value <= 0 for value in ids) or len(set(ids)) != len(ids) or len(runs) > total:
                raise policy.PolicyError('operator Check Run pagination is incomplete or changed')
            if len(runs) == total:
                return runs
            if len(rows) < 100:
                raise policy.PolicyError('operator Check Run inventory is incomplete')
            page += 1

    def queue_entries(self, branch):
        # The default-branch workflow_run has no native merge_group event body.
        # Recover its base ONLY from GitHub's queue metadata, never an artifact,
        # candidate-supplied output, branch name suffix or dispatch SHA/base input.
        query = """query($owner:String!, $name:String!, $branch:String!, $cursor:String) {
          repository(owner:$owner, name:$name) { mergeQueue(branch:$branch) {
            entries(first:100, after:$cursor) {
              totalCount nodes { baseCommit { oid } headCommit { oid }
                pullRequest { number headRefOid } }
              pageInfo { hasNextPage endCursor }
            }
          } }
        }"""
        owner, name = self.repository.split('/')
        cursor, entries, total, seen = None, [], None, set()
        while True:
            body = self.read(os.environ.get('GITHUB_GRAPHQL_URL', self.root + '/graphql'),
                             {'query': query, 'variables': {'owner': owner, 'name': name, 'branch': branch, 'cursor': cursor}})
            if body.get('errors'):
                raise policy.PolicyError('GitHub merge queue metadata read failed')
            try:
                page = body['data']['repository']['mergeQueue']['entries']
                count, rows, info = page['totalCount'], page['nodes'], page['pageInfo']
                if type(count) is not int or count < 0 or not isinstance(rows, list) or len(rows) > 100 or \
                        any(not isinstance(row, dict) for row in rows) or total is not None and total != count:
                    raise ValueError()
                entries.extend(rows)
                total = count
                if len(entries) > total or type(info['hasNextPage']) is not bool:
                    raise ValueError()
                if not info['hasNextPage']:
                    if len(entries) != total:
                        raise ValueError()
                    return entries
                cursor = info['endCursor']
                if not rows or not isinstance(cursor, str) or not cursor or cursor in seen:
                    raise ValueError()
                seen.add(cursor)
            except (KeyError, TypeError, ValueError):
                raise policy.PolicyError('GitHub merge queue inventory is incomplete or changed') from None


def trusted_group(api, event_name, event, source_run=None):
    """Identify a live native group through API-owned identities, not candidate data."""
    repository = api.get('')
    if repository.get('full_name') != REPOSITORY or repository.get('id') != REPOSITORY_ID or \
            repository.get('visibility') != 'public' or repository.get('private') is not False or \
            repository.get('default_branch') != 'main':
        raise policy.PolicyError('not the public .github/main repository')
    if event_name == 'workflow_run':
        source_run = (event.get('workflow_run') or {}).get('id')
    elif event_name != 'workflow_dispatch':
        raise policy.PolicyError('operator projection requires trusted workflow_run or dispatch')
    if isinstance(source_run, str) and source_run.isascii() and source_run.isdecimal():
        source_run = int(source_run)
    if type(source_run) is not int or source_run <= 0:
        raise policy.PolicyError('a native source run ID is required')
    run = api.get(f'actions/runs/{source_run}')
    workflow = api.get('actions/workflows/project-control.yml')
    head = run.get('head_sha')
    if run.get('id') != source_run or run.get('event') != 'merge_group' or \
            run.get('workflow_id') != workflow.get('id') or type(workflow.get('id')) is not int or \
            run.get('repository', {}).get('id') != REPOSITORY_ID or \
            run.get('repository', {}).get('full_name') != REPOSITORY or \
            not isinstance(head, str) or not policy.SHA.fullmatch(head) or \
            not isinstance(run.get('head_branch'), str) or \
            not run['head_branch'].startswith('gh-readonly-queue/main/'):
        raise policy.PolicyError('source run is not a native Project control main merge group')
    entries = api.queue_entries('main')
    matches = [entry for entry in entries if (entry.get('headCommit') or {}).get('oid') == head]
    if len(matches) != 1:
        raise policy.PolicyError('source merge group is absent or duplicated in the live queue')
    base = (matches[0].get('baseCommit') or {}).get('oid')
    if not isinstance(base, str) or not policy.SHA.fullmatch(base) or base == head:
        raise policy.PolicyError('merge group has no authoritative base commit')
    return {'repository': repository, 'merge_group': {'base_sha': base, 'head_sha': head,
            'base_ref': 'refs/heads/main', 'head_ref': 'refs/heads/' + run['head_branch']}}, source_run


def bind_head(api, pr, synthetic, base, group_shas):
    """Bind the reviewed content to its immutable position, not live queue state."""
    head = pr['head']['sha']
    location = f"PR #{pr['number']} head {head}"
    raw = synthetic.get('raw_commit')
    headers = raw.partition(b'\n\n')[0].decode('ascii', errors='replace').splitlines() if isinstance(raw, bytes) else []
    trees = [line[5:] for line in headers if line.startswith('tree ')]
    parents = [line[7:] for line in headers if line.startswith('parent ')]
    if len(trees) != 1 or len(parents) not in (1, 2) or not all(policy.SHA.fullmatch(sha) for sha in trees + parents):
        raise policy.PolicyError(location + ': immutable merge-group tree/parents are incomplete or unsupported')
    if parents[0] != base and parents[0] not in group_shas:
        raise policy.PolicyError(location + ': immutable merge-group position is incomplete')
    # The API's parent list must agree with the immutable Git commit object.
    remote = api.get('commits/' + synthetic['sha'])
    if remote.get('sha') != synthetic['sha'] or \
            [item['sha'] for item in remote['parents']] != parents:
        raise policy.PolicyError(location + ': API merge-group parents disagree with immutable source')
    if len(parents) == 2:
        if parents[1] != head:
            raise policy.PolicyError(location + ': membership/head changed during evaluation; '
                              f'merge-group parent pins {parents[1]}')
        return
    try:
        tree = policy.squash_merge_tree(api.repository, api.token, parents[0], head)
    except policy.PolicyError as error:
        raise policy.PolicyError(location + ': ' + str(error)) from None
    if tree != trees[0]:
        raise policy.PolicyError(location + f": squash tree mismatch at merge-group commit {synthetic['sha']}")



def ops_security_review_findings(api, pr):
    """Only the latest canonical App Check Run on this exact head can approve it."""
    number = pr.get('number') if isinstance(pr, dict) else None
    head = (pr.get('head') or {}).get('sha') if isinstance(pr, dict) else None
    location = f'PR #{number} head {head}'
    if type(number) is not int or number <= 0 or not isinstance(head, str) or not policy.SHA.fullmatch(head):
        raise policy.PolicyError('merge-group PR identity is incomplete')
    try:
        rows = api.operator_checks(head)
        matching = [row for row in rows if row.get('name') == CHECK_NAME and
                    (row.get('app') or {}).get('id') == APP_ID]
        if not matching:
            state = 'missing canonical App approval'
        else:
            ids = [row.get('id') for row in matching]
            if any(type(value) is not int or value <= 0 for value in ids) or len(set(ids)) != len(ids):
                raise policy.PolicyError('operator Check Run identity is incomplete')
            latest = max(matching, key=lambda row: row['id'])
            state = latest.get('conclusion') if latest.get('status') == 'completed' else 'pending'
            if latest.get('head_sha') != head:
                state = 'wrong approval head'
            if state == 'success':
                return []
    except (policy.PolicyError, KeyError, TypeError, ValueError) as error:
        state = 'API read error: ' + (str(error) if isinstance(error, policy.PolicyError) else 'incomplete Check Run metadata')
    return [{'rule': CHECK_NAME + ': ' + str(state), 'location': location, 'line': 0}]


def check(api, event, sha):
    repository = event.get('repository') or {}
    group = event.get('merge_group') or {}
    if repository.get('full_name') != 'SylphxAI/.github' or repository.get('visibility') != 'public' or \
            repository.get('private') is not False or group.get('base_ref') != 'refs/heads/main' or \
            not isinstance(sha, str) or not policy.SHA.fullmatch(sha) or group.get('head_sha') != sha:
        raise policy.PolicyError('not the public .github/main merge-group candidate')
    commits, snapshots, queued = policy.merge_group_snapshot(
        api, group, check_pull_text=False)
    if not snapshots:
        raise policy.PolicyError('merge group has no resolved PR members')
    findings, bound = [], set()
    by_sha = {item['sha']: item for item in commits}
    for pr, _ in snapshots:
        position = queued[pr['number']][1] if pr['number'] in queued else pr.get('merge_commit_sha')
        if position not in by_sha or position in bound:
            raise policy.PolicyError('merge-group member position is missing or duplicated')
        bind_head(api, pr, by_sha[position], group['base_sha'], set(by_sha))
        bound.add(position)
        findings.extend(ops_security_review_findings(api, pr))
    findings.extend(policy.recheck_merge_group(api, group, commits, snapshots, queued))
    return findings, len(snapshots)


def main():
    AppGitHub = publisher_module.AppGitHub
    PublicationError = publisher_module.PublicationError
    publish_check = publisher_module.publish_check

    try:
        if os.environ.get('GITHUB_REPOSITORY') != REPOSITORY or \
                os.environ.get('GITHUB_REF') != 'refs/heads/main' or \
                os.environ.get('GITHUB_WORKFLOW_REF') != REPOSITORY + '/.github/workflows/pr-review.yml@refs/heads/main':
            raise policy.PolicyError('projection must execute the protected default-branch PR review wrapper')
        token = os.environ.get('GH_TOKEN', '')
        if not token:
            raise policy.PolicyError('read-only metadata token is unavailable')
        event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
        api = GitHub(REPOSITORY, token)
        group_event, source_run = trusted_group(api, os.environ.get('GITHUB_EVENT_NAME'), event,
                                               (event.get('inputs') or {}).get('merge-group-run'))
        head = group_event['merge_group']['head_sha']
        findings, count = check(api, group_event, head)
        # A retired, changed or replaced queue entry must not acquire a success
        # after its metadata was initially observed.
        after, _ = trusted_group(api, 'workflow_dispatch', {}, source_run)
        if after != group_event:
            raise policy.PolicyError('merge group base/head changed during evaluation')
        for finding in findings:
            print('::error::{rule} at {location}'.format(**finding))
        auth_path = Path(os.environ['RUNNER_TEMP']) / 'pr-review-auth.json'
        auth = json.loads(auth_path.read_text())
        publisher = AppGitHub(REPOSITORY, auth.get('checkToken', ''))
        conclusion = 'failure' if findings else 'success'
        summary = f'Checked {count} exact-head canonical App approvals; {len(findings)} finding(s).'
        if findings:
            summary += '\n' + '\n'.join('{rule} at {location}'.format(**row) for row in findings)
        receipt = publish_check(publisher, head, conclusion, Path(os.environ['RUNNER_TEMP']) / 'operator-projection',
                                summary, f'merge-group/{source_run}/{os.environ.get("GITHUB_RUN_ID")}/{os.environ.get("GITHUB_RUN_ATTEMPT")}')
        print(json.dumps(receipt))
        return int(bool(findings))
    except (policy.PolicyError, PublicationError, KeyError, ValueError, OSError, TypeError) as error:
        message = str(error) if isinstance(error, (policy.PolicyError, PublicationError)) else 'operator projection metadata could not be read'
        print('::error::ops-security/review: ' + message)
        return 2


if __name__ == '__main__':
    sys.exit(main())
