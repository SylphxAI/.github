"""PR → queue consumes only a stable, authenticated success on the exact tree."""
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import signal
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / '.github/actions/ci-range'
REPO, REPO_ID, NUMBER = 'o/r', 7, 3
WORKFLOW = '.github/workflows/gate.yml'
LOCAL, LEAF, GATE = 'rust.yml', 'rust / Rust', 'ci-ok'
BASE, HEAD, MERGE = 'a' * 40, 'b' * 40, 'c' * 40
REF = f'refs/heads/gh-readonly-queue/main/pr-{NUMBER}-{BASE}'


@pytest.fixture
def proof_case(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ACTION))
    import ci_range
    import post_main

    # Real queue checkout identity and tree, but a different PR merge commit.
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=tmp_path, text=True).strip()

    git('init', '-q')
    (tmp_path / 'source').write_text('code')
    git('add', 'source')
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'queue')
    sha, tree = git('rev-parse', 'HEAD'), git('rev-parse', 'HEAD^{tree}')
    monkeypatch.chdir(tmp_path)
    event = tmp_path / 'event.json'
    out = tmp_path / 'out'
    for key, value in dict(PROOF_MODE='merge-group', GITHUB_EVENT_NAME='merge_group',
                           GITHUB_EVENT_PATH=str(event), GITHUB_OUTPUT=str(out),
                           GITHUB_SHA=sha, GITHUB_WORKFLOW_SHA=sha, GITHUB_REF=REF,
                           GITHUB_REPOSITORY=REPO, GITHUB_WORKFLOW_REF=f'{REPO}/{WORKFLOW}@{REF}',
                           PR_REUSE_LOCAL_WORKFLOW=LOCAL, PR_REUSE_JOB=LEAF,
                           PR_REUSE_AGGREGATE=GATE).items():
        monkeypatch.setenv(key, value)
    repo = dict(id=REPO_ID, full_name=REPO)
    payload = dict(action='checks_requested', repository=dict(repo, default_branch='main'),
                   merge_group=dict(head_sha=sha, head_ref=REF, base_ref='refs/heads/main'))
    assoc = dict(id=30, number=NUMBER, head=dict(ref='feature', sha=HEAD, repo=dict(id=REPO_ID)),
                 base=dict(ref='main', sha=BASE, repo=dict(id=REPO_ID)))
    pull = dict(deepcopy(assoc), state='open')
    run = dict(id=40, run_attempt=1, workflow_id=4, check_suite_id=8, path=WORKFLOW,
               repository=dict(repo), head_repository=dict(repo), event='pull_request',
               head_sha=HEAD, head_branch='feature', status='completed', conclusion='success',
               pull_requests=[assoc], referenced_workflows=[dict(
                   path=f'{REPO}/.github/workflows/{LOCAL}@{MERGE}', sha=MERGE,
                   ref=f'refs/pull/{NUMBER}/merge')])
    jobs, checks = [], {}
    for i, name in enumerate((LEAF, GATE), 1):
        jobs.append(dict(id=100+i, run_id=40, run_attempt=1, head_sha=HEAD, name=name,
                         status='completed', conclusion='success',
                         check_run_url=f'https://api.github.com/repos/{REPO}/check-runs/{200+i}'))
        checks[f'check-runs/{200+i}'] = dict(
            id=200+i, app=dict(id=15368, slug='github-actions'), head_sha=HEAD, name=name,
            check_suite=dict(id=8), details_url=f'https://github.com/{REPO}/actions/runs/40/job/{100+i}',
            status='completed', conclusion='success')
    commit = dict(sha=MERGE, parents=[dict(sha=BASE), dict(sha=HEAD)], tree=dict(sha=tree))
    paths = {
        f'pulls/{NUMBER}': pull,
        'actions/workflows/gate.yml': dict(id=4, path=WORKFLOW, state='active'),
        'actions/runs/40': run,
        f'actions/workflows/4/runs?event=pull_request&head_sha={HEAD}&per_page=100&page=1':
            dict(total_count=1, workflow_runs=[run]),
        'actions/runs/40/jobs?filter=latest&per_page=100&page=1': dict(total_count=2, jobs=jobs),
        f'git/commits/{MERGE}': commit,
        **checks,
    }
    calls = Counter()

    def execute(transform=lambda path, data, count: data):
        def api(path):
            assert path.startswith(f'repos/{REPO}/')
            path = path.removeprefix(f'repos/{REPO}/')
            calls[path] += 1
            return transform(path, deepcopy(paths[path]), calls[path])

        monkeypatch.setattr(post_main, 'api', api)
        event.write_text(json.dumps(payload))
        assert ci_range.main() == 0
        return dict(line.split('=', 1) for line in out.read_text().splitlines())

    return execute, payload, paths, calls, dict(repository=REPO, workflow=WORKFLOW,
                                                queue=sha, tree=tree, source=MERGE, pull=NUMBER,
                                                run=40, attempt=1,
                                                jobs={LEAF: dict(job=101, check=201),
                                                      GATE: dict(job=102, check=202)})


def test_same_tree_different_commit_authenticates_through_action_entrypoint(proof_case):
    execute, _, _, calls, expected = proof_case
    assert expected['queue'] != expected['source'] != HEAD
    result = execute()
    assert result['pr-reused'] == 'true'
    assert json.loads(result['pr-reuse-proof']) == expected
    assert calls['actions/runs/40'] == 2
    assert calls['check-runs/201'] == calls['check-runs/202'] == 2
    assert 'verdict' not in result and 'reused' not in result


@pytest.mark.parametrize('scenario', [
    'tree', 'parents', 'binding', 'contract', 'ambiguous_binding', 'closed', 'fork', 'missing_runs',
    'incomplete_runs', 'pending', 'newer_failure', 'event', 'workflow_sha', 'wrong_base',
    'wrong_run', 'wrong_pr', 'check_app', 'leaf_failure', 'aggregate_skipped',
    'wrong_attempt', 'jobs_drift', 'attempt_drift', 'newer_drift', 'provider_error', 'timeout',
])
def test_unproven_tree_falls_back(proof_case, monkeypatch, scenario):
    execute, payload, paths, calls, _ = proof_case
    run = paths['actions/runs/40']
    jobs_path = 'actions/runs/40/jobs?filter=latest&per_page=100&page=1'
    listing = next(p for p in paths if '/workflows/4/runs?' in p)
    if scenario == 'event':
        monkeypatch.setenv('GITHUB_EVENT_NAME', 'workflow_dispatch')
    elif scenario == 'workflow_sha':
        monkeypatch.setenv('GITHUB_WORKFLOW_SHA', 'f' * 40)
    elif scenario == 'contract':
        monkeypatch.setenv('PR_REUSE_JOB', 'wrong / job')
    elif scenario == 'wrong_base':
        payload['merge_group']['base_ref'] = 'refs/heads/other'
    elif scenario == 'tree':
        paths[f'git/commits/{MERGE}']['tree']['sha'] = 'f' * 40
    elif scenario == 'parents':
        paths[f'git/commits/{MERGE}']['parents'].reverse()
    elif scenario == 'binding':
        run['referenced_workflows'][0]['ref'] = 'refs/heads/main'
    elif scenario == 'ambiguous_binding':
        run['referenced_workflows'] *= 2
    elif scenario == 'closed':
        paths[f'pulls/{NUMBER}']['state'] = 'closed'
    elif scenario == 'fork':
        paths[f'pulls/{NUMBER}']['head']['repo']['id'] += 1
    elif scenario == 'missing_runs':
        paths[listing] = dict(total_count=0, workflow_runs=[])
    elif scenario == 'incomplete_runs':
        paths[listing]['total_count'] += 1
    elif scenario == 'pending':
        run.update(status='in_progress', conclusion=None)
    elif scenario == 'newer_failure':
        paths[listing] = dict(total_count=2, workflow_runs=[run, dict(run, id=41, conclusion='failure')])
    elif scenario == 'wrong_run':
        run['workflow_id'] = 5
    elif scenario == 'wrong_pr':
        run['pull_requests'][0]['id'] += 1
    elif scenario == 'check_app':
        paths['check-runs/201']['app']['id'] = 9999
    elif scenario == 'leaf_failure':
        paths[jobs_path]['jobs'][0]['conclusion'] = 'failure'
    elif scenario == 'aggregate_skipped':
        paths[jobs_path]['jobs'][1]['conclusion'] = 'skipped'
    elif scenario == 'wrong_attempt':
        paths[jobs_path]['jobs'][0]['run_attempt'] += 1

    def transform(path, data, count):
        if scenario == 'provider_error':
            raise OSError('API unreadable')
        if scenario == 'timeout':
            signal.raise_signal(signal.SIGALRM)
        if count == 2 and scenario == 'jobs_drift' and path == jobs_path:
            data['jobs'][0]['completed_at'] = 'changed'
        if count == 2 and scenario == 'attempt_drift' and path == 'actions/runs/40':
            data['run_attempt'] += 1
        if count == 2 and scenario == 'newer_drift' and path == listing:
            data['workflow_runs'][0]['id'] += 1
        return data

    assert execute(transform) == {'pr-reused': 'false', 'pr-reuse-proof': '{}'}
    if scenario in ('event', 'workflow_sha', 'wrong_base'):
        assert not calls


def test_pilot_wires_only_the_exact_leaf_between_native_events():
    jobs = yaml.safe_load((ROOT / '.github/workflows/project-control.yml').read_text())['jobs']
    leaf, plan, gate = (jobs[name] for name in ('rust-node-consumer', 'template-plan', 'ci-ok'))
    step = next(step for step in plan['steps'] if step.get('id') == 'pr-reuse')
    called = leaf['uses'].rsplit('/', 1)[-1]
    called_job = yaml.safe_load((ROOT / '.github/workflows' / called).read_text())['jobs']['rust']
    assert step['with'] == {'mode': 'merge-group', 'pr-reuse-local-workflow': called,
                            'pr-reuse-job': f'rust-node-consumer / {called_job["name"]}',
                            'pr-reuse-aggregate': 'ci-ok'}
    assert step['if'] == "github.event_name == 'merge_group'" and step['continue-on-error'] is True
    assert plan['permissions'] == dict(actions='read', checks='read', contents='read', **{'pull-requests': 'read'})
    checkout = next(s for s in called_job['steps'] if s.get('name') == 'Checkout')
    assert checkout['with']['ref'].endswith(' || github.sha }}')
    assert leaf['with']['toolchain'].count('.') == 2
    assert leaf['with']['node-version'].count('.') == 2
    assert leaf['needs'] == 'template-plan'
    assert all(name in gate['needs'] for name in ('template-plan', 'rust-node-consumer'))
    assert 'always()' in gate['if']
    positive = "github.event_name == 'merge_group' && needs.template-plan.result == 'success' && needs.template-plan.outputs.pr-reused == 'true'"
    assert leaf['if'] == '$' + '{{ !cancelled() && !(' + positive + ') }}'
    assert '$' + "{{ !(" + positive + ") && 'rust-node-consumer' || '' }}" in gate['steps'][-1]['with']['required']
