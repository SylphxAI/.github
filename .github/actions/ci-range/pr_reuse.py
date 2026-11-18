"""Reuse an authenticated native PR check for an identical merge-queue tree."""
from __future__ import annotations

import json
import os
import signal
import re
from urllib.parse import quote

import post_main as proof
from queue_reuse import snapshot, successful_jobs


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def from_repo(identity, repo_id):
    return proof.positive_id(identity.get('id')) and identity['id'] == repo_id


def source(payload, sha, tree, env):
    repo = env['GITHUB_REPOSITORY']
    repository = payload['repository']
    repo_id, branch = repository['id'], repository['default_branch']
    require(proof.positive_id(repo_id) and repository['full_name'] == repo and branch,
            'repository identity unavailable')
    group = payload['merge_group']
    ref = group['head_ref']
    # The queue ref only locates a PR. Provider PR/run/commit identities below
    # decide eligibility; branch naming alone never authorizes reuse.
    candidate = re.fullmatch(r'pr-([1-9][0-9]*)-[0-9a-f]{40}',
                             ref.removeprefix(f'refs/heads/gh-readonly-queue/{branch}/'))
    workflow_ref, at, workflow_target = env['GITHUB_WORKFLOW_REF'].partition('@')
    workflow = workflow_ref.removeprefix(f'{repo}/')
    require(payload['action'] == 'checks_requested' and at and workflow_ref == f'{repo}/{workflow}' and workflow_target == ref
            and re.fullmatch(r'\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml', workflow)
            and ref.startswith(f'refs/heads/gh-readonly-queue/{branch}/') and candidate
            and group['base_ref'] == f'refs/heads/{branch}'
            and proof.valid_sha(sha) and proof.valid_sha(tree)
            and sha == group['head_sha'] == env['GITHUB_SHA'] == env['GITHUB_WORKFLOW_SHA']
            and ref == env['GITHUB_REF'], 'not the exact native default-branch queue checkout')
    return repo, repo_id, branch, workflow, int(candidate[1])


def receipt(payload, sha, tree, env):
    repo, repo_id, branch, workflow, number = source(payload, sha, tree, env)
    called = env['PR_REUSE_LOCAL_WORKFLOW']
    job, aggregate = env['PR_REUSE_JOB'], env['PR_REUSE_AGGREGATE']
    require(re.fullmatch(r'[A-Za-z0-9_.-]+\.ya?ml', called)
            and ' / ' in job and all(part.strip() for part in job.split(' / '))
            and aggregate.strip() and aggregate != job, 'invalid reuse contract')
    root = f'repos/{repo}'
    pull_path = f'{root}/pulls/{number}'

    def pull_identity():
        pr = proof.api(pull_path)
        head, base = pr['head'], pr['base']
        require(proof.positive_id(pr['id']) and proof.positive_id(pr['number'])
                and pr['number'] == number and pr['state'] == 'open'
                and from_repo(head['repo'], repo_id) and from_repo(base['repo'], repo_id)
                and base['ref'] == branch and proof.valid_sha(head['sha']) and head['ref'],
                'not an open same-repository PR against the queue target')
        return pr['id'], head['sha'], head['ref']

    pr_id, head_sha, head_ref = pull_identity()
    workflow_path = f"{root}/actions/workflows/{quote(workflow.rsplit('/', 1)[-1], safe='')}"
    meta = proof.api(workflow_path)
    workflow_id = meta['id']
    require(proof.positive_id(workflow_id) and meta['path'] == workflow and meta['state'] == 'active',
            'consumer workflow registration unavailable')

    def latest():
        rows = proof.paged(f'{root}/actions/workflows/{workflow_id}/runs?event=pull_request&head_sha={head_sha}',
                           'workflow_runs', limit=1)
        for row in rows:
            require(all(proof.positive_id(row.get(field)) for field in
                        ('id', 'check_suite_id', 'run_attempt', 'workflow_id'))
                    and row['workflow_id'] == workflow_id and row['path'] == workflow
                    and row['event'] == 'pull_request' and row['head_branch'] == head_ref
                    and row['head_sha'] == head_sha
                    and all(from_repo(row[field], repo_id) and row[field]['full_name'] == repo
                            for field in ('repository', 'head_repository')),
                    'filtered PR run identity mismatch')
        return max(rows, key=lambda row: row['id'], default=None)

    run = latest()
    require(run and run['status'] == 'completed'
            and run['conclusion'] == 'success', 'latest exact-head native PR run is not a success')
    associated = run['pull_requests']
    require(len(associated) == 1, 'PR association unavailable or ambiguous')
    pr = associated[0]
    require(proof.positive_id(pr['id']) and pr['id'] == pr_id and proof.positive_id(pr['number'])
            and pr['number'] == number and from_repo(pr['head']['repo'], repo_id)
            and from_repo(pr['base']['repo'], repo_id) and pr['head']['sha'] == head_sha
            and pr['head']['ref'] == head_ref and pr['base']['ref'] == branch
            and proof.valid_sha(pr['base']['sha']), 'run PR association differs from queue target')
    run_path = f"{root}/actions/runs/{run['id']}"
    require(snapshot(proof.api(run_path)) == snapshot(run), 'PR attempt changed before proof')

    # A PR run's head_sha identifies the branch tip, NOT the synthetic merge
    # checkout. GitHub records the commit resolving a local reusable call.
    prefix = f'{repo}/.github/workflows/{called}@'
    bindings = [row for row in run['referenced_workflows'] if row['path'].startswith(prefix)]
    require(len(bindings) == 1, 'local reusable workflow source unavailable or ambiguous')
    binding = bindings[0]
    checkout = binding['sha']
    require(proof.valid_sha(checkout) and binding['path'] == prefix + checkout
            and binding['ref'] == f'refs/pull/{number}/merge', 'not a native PR merge checkout')
    commit = proof.api(f'{root}/git/commits/{checkout}')
    require(commit['sha'] == checkout and [parent['sha'] for parent in commit['parents']]
            == [pr['base']['sha'], head_sha] and commit['tree']['sha'] == tree,
            'PR checked tree differs from the queue tree or PR merge parents')

    names = {aggregate, job}
    jobs, selected = successful_jobs(repo, run, names)
    require(snapshot(successful_jobs(repo, run, names)) == snapshot((jobs, selected))
            and snapshot(proof.api(run_path)) == snapshot(run)
            and snapshot(latest()) == snapshot(run)
            and pull_identity() == (pr_id, head_sha, head_ref)
            and snapshot(proof.api(workflow_path)) == snapshot(meta), 'PR evidence changed during proof')
    return dict(repository=repo, workflow=workflow, tree=tree, queue=sha, pull=number,
                source=checkout, run=run['id'], attempt=run['run_attempt'], jobs=selected)


def select(git, env):
    if env.get('GITHUB_EVENT_NAME') != 'merge_group':
        return {}
    # This is a latency optimization. API delays must not indefinitely hold
    # the fallback build in the merge queue. Never publish a partial proof.
    def timed_out(*_):
        raise TimeoutError('PR proof budget exhausted')

    previous = signal.signal(signal.SIGALRM, timed_out)
    try:
        signal.alarm(30)
        with open(env['GITHUB_EVENT_PATH']) as event:
            return receipt(json.load(event), git('rev-parse', 'HEAD'),
                           git('rev-parse', 'HEAD^{tree}'), env)
    except Exception as error:
        print(f'::notice::PR tree reuse unavailable; running normally ({error!r})')
        return {}
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
