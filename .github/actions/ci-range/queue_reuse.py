"""Opt-in exact-source merge-group reuse; never a post-main verified producer.

The caller names the queue workflow, its aggregate and each lane's exact job
names. Only a native default-branch push can consume a completed successful
latest attempt for the identical commit. Missing evidence means real work.
"""
from __future__ import annotations

import json
import os
import re
from urllib.parse import quote

import post_main as proof


def configuration(workflow, aggregate, text, lanes):
    if re.fullmatch(r"(?:\.github/workflows/)?[A-Za-z0-9_.-]+\.ya?ml", workflow) is None:
        raise ValueError("reuse workflow must be a repository workflow filename")
    if not isinstance(aggregate, str) or not aggregate.strip():
        raise ValueError("reuse aggregate must name an exact job")
    mapping = json.loads(text)
    if not isinstance(mapping, dict) or not mapping or set(mapping) - set(lanes):
        raise ValueError("reuse jobs must map declared lanes to exact job names")
    for names in mapping.values():
        if (not isinstance(names, list) or not names
                or any(not isinstance(name, str) or not name.strip() for name in names)
                or len(set(names)) != len(names) or aggregate in names):
            raise ValueError("reuse lane needs distinct exact jobs separate from the aggregate")
    return proof.workflow_path(workflow), mapping


def origin(run, repo, repo_id, workflow, workflow_id, branch, sha):
    """Validate every returned row: a malformed filtered result is not absence."""
    if not isinstance(run, dict):
        raise ValueError("queue run is malformed")
    for field in ("id", "workflow_id", "check_suite_id", "run_attempt"):
        if not proof.positive_id(run.get(field)):
            raise ValueError("queue run identity unavailable")
    for field in ("repository", "head_repository"):
        identity = run.get(field)
        if (not isinstance(identity, dict) or identity.get("id") != repo_id
                or identity.get("full_name") != repo):
            raise ValueError("queue repository identity mismatch")
    if (run.get("event") != "merge_group" or run.get("path") != workflow
            or run.get("workflow_id") != workflow_id or run.get("head_sha") != sha):
        raise ValueError("queue workflow or exact source mismatch")
    head = run.get("head_branch")
    if not isinstance(head, str) or not head.strip():
        raise ValueError("queue target unavailable")
    prefix = f"gh-readonly-queue/{branch}/"
    return head.startswith(prefix) and len(head) > len(prefix)


def latest(repo, repo_id, workflow, workflow_id, branch, sha):
    rows = proof.paged(
        f"repos/{repo}/actions/workflows/{workflow_id}/runs?event=merge_group&head_sha={sha}",
        "workflow_runs",
    )
    candidates = [run for run in rows if origin(run, repo, repo_id, workflow, workflow_id, branch, sha)]
    return max(candidates, key=lambda run: (run["id"], run["run_attempt"]), default=None)


def successful_jobs(repo, run, names):
    jobs = proof.paged(f"repos/{repo}/actions/runs/{run['id']}/jobs?filter=latest", "jobs")
    selected = {}
    for name in sorted(names):
        matches = [job for job in jobs if job.get("name") == name]
        if len(matches) != 1:
            raise ValueError("queue required job absent or ambiguous")
        job = matches[0]
        if (job.get("status") != "completed" or job.get("conclusion") != "success"
                or not proof.authenticated_job(run, job, repo)):
            raise ValueError("queue required job is not an authenticated success")
        selected[name] = dict(job=job["id"], check=proof.linked_check_id(job, repo))
    return jobs, selected


def receipt(repo, repo_id, branch, sha, workflow, aggregate, mapping):
    meta_path = f"repos/{repo}/actions/workflows/{quote(workflow.rsplit('/', 1)[-1], safe='')}"
    meta = proof.api(meta_path)
    workflow_id = meta.get("id")
    if (not proof.positive_id(workflow_id) or meta.get("path") != workflow
            or meta.get("state") != "active"):
        raise ValueError("reuse workflow identity unavailable")
    run = latest(repo, repo_id, workflow, workflow_id, branch, sha)
    if run is None:
        raise ValueError("no exact-source queue run")
    # The list may have raced a rerun. Read the actual current attempt before
    # inspecting jobs, and fence both this run and the full candidate set later.
    run_path = f"repos/{repo}/actions/runs/{run['id']}"
    if proof.api(run_path) != run:
        raise ValueError("queue attempt changed before proof")
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        raise ValueError("latest queue attempt is not completed successfully")
    names = {aggregate} | {name for jobs in mapping.values() for name in jobs}
    jobs, selected = successful_jobs(repo, run, names)
    # Complete second censuses, including authentication, reject a changing job
    # set, replacement run or rerun. Never fall back to an older passing run.
    if successful_jobs(repo, run, names) != (jobs, selected):
        raise ValueError("queue jobs changed during proof")
    if (proof.api(run_path) != run
            or latest(repo, repo_id, workflow, workflow_id, branch, sha) != run
            or proof.api(meta_path) != meta):
        raise ValueError("queue producer changed during proof")
    return dict(repository=repo, repository_id=repo_id, workflow=workflow,
                workflow_id=workflow_id, branch=branch, sha=sha, run=run["id"],
                attempt=run["run_attempt"], aggregate=aggregate, jobs=selected)


def select(event, payload, branch, sha, run):
    """Return (effective selection, per-lane reused booleans, evidence)."""
    reused = dict.fromkeys(run, False)
    empty = (dict(run), reused, {})
    workflow = os.environ.get("REUSE_WORKFLOW", "")
    aggregate = os.environ.get("REUSE_AGGREGATE", "")
    text = os.environ.get("REUSE_JOBS", "")
    if not any((workflow, aggregate, text)):
        return empty
    # An explicit request is never suppressed, even if `only` names no lane.
    if event != "push" or os.environ.get("ONLY", "").strip():
        return empty
    try:
        workflow, mapping = configuration(workflow, aggregate, text, run)
        repository = payload.get("repository", {})
        repo = os.environ["GITHUB_REPOSITORY"]
        repo_id = repository.get("id")
        if (not proof.positive_id(repo_id) or repository.get("full_name") != repo
                or repository.get("default_branch") != branch
                or payload.get("ref") != f"refs/heads/{branch}"
                or os.environ.get("GITHUB_REF") != payload["ref"]
                or not proof.valid_sha(sha) or os.environ.get("GITHUB_SHA") != sha
                or payload.get("after") != sha or payload.get("deleted") is not False):
            raise ValueError("not an exact native default-branch checkout")
        mapping = {lane: names for lane, names in mapping.items() if run[lane]}
        if not mapping:
            return empty
        evidence = receipt(repo, repo_id, branch, sha, workflow, aggregate, mapping)
        reused.update(dict.fromkeys(mapping, True))
        evidence["lanes"] = mapping
        return {lane: selected and not reused[lane] for lane, selected in run.items()}, reused, evidence
    except Exception as error:
        print(f"::warning::queue reuse unavailable ({error}); ordinary lane selection retained")
        return empty
