#!/usr/bin/env python3
"""Authenticated post-main proof selection, shared by CI range and red-main.

The workflow-run conclusion is not proof: optional publication can fail after
verified passed. Likewise a newer recording/dispatch/merge_group check cannot
mask a full post-main failure. Unknown or incomplete reads are never green.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from urllib.parse import urlencode

ACTIONS_APP_ID = 15368
PAGE_LIMIT = 5
WALK_LIMIT = 200


def api(path):
    env = dict(os.environ)
    env["GH_TOKEN"] = env.get("ACTIONS_TOKEN") or env.get("TOKEN") or env.get("GH_TOKEN", "")
    result = subprocess.run(["gh", "api", path], env=env, check=True, capture_output=True,
                            text=True, timeout=35)
    return json.loads(result.stdout)


def workflow_path(workflow):
    return workflow if workflow.startswith(".github/workflows/") else f".github/workflows/{workflow}"


def full_push(run, repo, branch, workflow, sha=None):
    return (
        run.get("event") == "push"
        and run.get("head_branch") == branch
        and run.get("path") == workflow_path(workflow)
        and run.get("repository", {}).get("full_name") == repo
        and run.get("head_repository", {}).get("full_name") == repo
        and isinstance(run.get("repository", {}).get("id"), int)
        and run["repository"]["id"] > 0
        and run["repository"]["id"] == run.get("head_repository", {}).get("id")
        and isinstance(run.get("workflow_id"), int) and run["workflow_id"] > 0
        and isinstance(run.get("id"), int) and run["id"] > 0
        and isinstance(run.get("check_suite_id"), int) and run["check_suite_id"] > 0
        and re.fullmatch(r"[a-f0-9]{40}", run.get("head_sha", "")) is not None
        and (sha is None or run["head_sha"] == sha)
    )


def paged(path, key, require_complete=True):
    rows = []
    for page in range(1, PAGE_LIMIT + 1):
        body = api(f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}")
        batch = body.get(key)
        if not isinstance(batch, list):
            raise ValueError(f"missing {key} array")
        rows.extend(batch)
        if len(batch) < 100:
            return rows
    if require_complete:
        raise ValueError(f"{key} pagination exhausted")
    return rows


def linked_check_id(job, repo):
    """Extract the check identity from a strict provider URL, never the job id."""
    url = job.get("check_run_url")
    if not isinstance(url, str):
        return None
    match = re.fullmatch(
        rf"https://api\.github\.com/repos/{re.escape(repo)}/check-runs/([1-9][0-9]*)", url,
    )
    return int(match[1]) if match else None


def checked_verdict(run, repo, branch, workflow, name="verified"):
    """Read the latest attempt's aggregate job AND its authenticated check."""
    if not full_push(run, repo, branch, workflow):
        # The provider read succeeded and identifies a non-proof producer.
        # Distinguish this from an eligible run whose proof is unavailable.
        return "ineligible"
    jobs = paged(f"repos/{repo}/actions/runs/{run['id']}/jobs?filter=latest", "jobs")
    jobs = [job for job in jobs if job.get("name") == name]
    if len(jobs) != 1:
        return "unknown"
    job = jobs[0]
    check_id = linked_check_id(job, repo)
    if (type(job.get("id")) is not int or job["id"] < 1 or check_id is None
            or job.get("run_id") != run["id"] or job.get("run_attempt") != run.get("run_attempt")
            or not isinstance(run.get("run_attempt"), int) or run["run_attempt"] < 1
            or job.get("head_sha") != run["head_sha"]):
        return "unknown"
    # Run, job and check ids are distinct identities. Fetch the locally built
    # path for the linked check; its details URL must name this actual job.
    check = api(f"repos/{repo}/check-runs/{check_id}")
    if (check.get("id") != check_id or check.get("app", {}).get("id") != ACTIONS_APP_ID
            or check.get("app", {}).get("slug") != "github-actions"
            or check.get("head_sha") != run["head_sha"] or check.get("name") != name
            or check.get("check_suite", {}).get("id") != run["check_suite_id"]
            or check.get("details_url") != f"https://github.com/{repo}/actions/runs/{run['id']}/job/{job['id']}"
            or check.get("status") != job.get("status")
            or check.get("conclusion") != job.get("conclusion")):
        return "unknown"
    if check.get("status") != "completed":
        return "pending"
    return check.get("conclusion") or "unknown"


def push_runs(repo, branch, workflow, sha=None):
    query = {"event": "push", "branch": branch}
    if sha:
        query["head_sha"] = sha
    # No whole-run status filter: a failed optional publish is not a failed
    # verified proof, and a newer genuine failure must not be hidden.
    rows = paged(f"repos/{repo}/actions/workflows/{workflow.rsplit('/', 1)[-1]}/runs?{urlencode(query)}",
                 "workflow_runs", require_complete=False)
    return [r for r in rows if full_push(r, repo, branch, workflow, sha)]


def latest_run(rows, sha):
    candidates = [row for row in rows if row.get("head_sha") == sha]
    return max(candidates, key=lambda r: (r["id"], r.get("run_attempt", 0)), default=None)


def sha_verdict(repo, branch, workflow, sha, name="verified"):
    run = latest_run(push_runs(repo, branch, workflow, sha), sha)
    return checked_verdict(run, repo, branch, workflow, name) if run else "unknown"


def last_verified(repo, branch, workflow, head="HEAD", name="verified", remote=False):
    rows = push_runs(repo, branch, workflow)
    if remote:
        sha = head
        ancestors = []
        for _ in range(WALK_LIMIT):
            parents = api(f"repos/{repo}/commits/{sha}").get("parents", [])
            if not parents:
                break
            sha = parents[0]["sha"]
            ancestors.append(sha)
            # Stop at the first authenticated success. No current-HEAD base.
            run = latest_run(rows, sha)
            if run and checked_verdict(run, repo, branch, workflow, name) == "success":
                return sha
        return ""
    ancestors = subprocess.run(["git", "rev-list", "--first-parent", f"--max-count={WALK_LIMIT}",
                                f"{head}^"], capture_output=True, text=True, check=False)
    if ancestors.returncode:
        return ""
    for sha in ancestors.stdout.split():
        run = latest_run(rows, sha)
        if run and checked_verdict(run, repo, branch, workflow, name) == "success":
            return sha
    return ""


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "check-id":
        check_id = linked_check_id(json.load(sys.stdin), sys.argv[2])
        if check_id is None:
            raise ValueError("invalid job check_run_url")
        print(check_id)
        return
    command, repo, branch, workflow, identity, *rest = sys.argv[1:]
    name = rest[0] if rest else "verified"
    if command == "base":
        print(last_verified(repo, branch, workflow, identity, name))
    elif command == "remote-base":
        print(last_verified(repo, branch, workflow, identity, name, remote=True))
    elif command == "sha":
        print(sha_verdict(repo, branch, workflow, identity, name))
    elif command == "run":
        run = api(f"repos/{repo}/actions/runs/{identity}")
        print(checked_verdict(run, repo, branch, workflow, name))
    else:
        raise ValueError("unknown proof selection command")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"post-main proof unavailable: {error}", file=sys.stderr)
        sys.exit(1)
