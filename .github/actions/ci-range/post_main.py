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


def positive_id(value):
    return type(value) is int and value > 0


def valid_sha(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{40}", value) is not None


def producer_origin(run, repo, branch, workflow, sha=None):
    """Only complete, valid producer evidence can establish ineligibility."""
    if not isinstance(run, dict):
        return "unknown"
    for field in ("id", "workflow_id", "check_suite_id", "run_attempt"):
        if not positive_id(run.get(field)):
            return "unknown"
    for field in ("repository", "head_repository"):
        identity = run.get(field)
        if (not isinstance(identity, dict) or not positive_id(identity.get("id"))
                or not isinstance(identity.get("full_name"), str)
                or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", identity["full_name"]) is None):
            return "unknown"
    event, tracked, path = run.get("event"), run.get("head_branch"), run.get("path")
    if (not isinstance(event, str) or re.fullmatch(r"[a-z][a-z0-9_]*", event) is None
            or not isinstance(tracked, str) or not tracked.strip()
            or not isinstance(path, str) or re.fullmatch(r"\.github/workflows/[^/]+\.ya?ml", path) is None
            or not valid_sha(run.get("head_sha"))):
        return "unknown"
    eligible = (
        event == "push" and tracked == branch and path == workflow_path(workflow)
        and run["repository"]["full_name"] == repo and run["head_repository"]["full_name"] == repo
        and run["repository"]["id"] == run["head_repository"]["id"]
        and (sha is None or run["head_sha"] == sha)
    )
    return "eligible" if eligible else "ineligible"


def full_push(run, repo, branch, workflow, sha=None):
    return producer_origin(run, repo, branch, workflow, sha) == "eligible"


def paged(path, key, reader=None):
    """Require complete counted coverage; adapters may supply a counted reader."""
    reader = api if reader is None else reader
    rows = []
    total = None
    identities = set()
    for page in range(1, PAGE_LIMIT + 1):
        body = reader(f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}")
        count = body.get("total_count") if isinstance(body, dict) else None
        batch = body.get(key) if isinstance(body, dict) else None
        if type(count) is not int or count < 0 or not isinstance(batch, list):
            raise ValueError(f"{key} history envelope unavailable")
        if total is None:
            total = count
        if count != total or len(batch) != min(100, max(0, total - len(rows))):
            raise ValueError(f"{key} history count/page inconsistent")
        for row in batch:
            if not isinstance(row, dict) or not positive_id(row.get("id")) or row["id"] in identities:
                raise ValueError(f"{key} history identity unavailable or repeated")
            identities.add(row["id"])
        rows.extend(batch)
        if len(rows) == total:
            return rows
    raise ValueError(f"{key} pagination exhausted")


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
    origin = producer_origin(run, repo, branch, workflow)
    if origin != "eligible":
        return origin
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
                 "workflow_runs")
    candidates = []
    for row in rows:
        origin = producer_origin(row, repo, branch, workflow, sha)
        if origin == "ineligible":
            continue
        # Missing origin evidence must not erase the newest possible producer
        # and expose an older passing/failing run. Unorderable history is an
        # unavailable proof set, not an empty one.
        if (not isinstance(row, dict) or not positive_id(row.get("id"))
                or not positive_id(row.get("run_attempt")) or not valid_sha(row.get("head_sha"))
                or (sha is not None and row["head_sha"] != sha)):
            raise ValueError("workflow history identity unavailable")
        candidates.append(row)
    return candidates


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


def failed_lanes(repo, identity):
    """Only complete, valid jobs can establish the previous run's failing lanes."""
    if not isinstance(identity, str) or re.fullmatch(r"[1-9][0-9]*", identity) is None:
        raise ValueError("invalid workflow run identity")
    jobs = paged(f"repos/{repo}/actions/runs/{identity}/jobs?filter=latest", "jobs")
    lanes = []
    for job in jobs:
        name = job.get("name")
        if (not isinstance(name, str) or not name.strip()
                or type(job.get("run_id")) is not int or job["run_id"] != int(identity)
                or job.get("status") != "completed"
                or job.get("conclusion") not in
                ("success", "failure", "timed_out", "cancelled", "skipped", "neutral",
                 "action_required", "startup_failure", "stale")):
            raise ValueError("previous-run lane evidence unavailable or invalid")
        if job["conclusion"] in ("failure", "timed_out"):
            # Only a joined name must survive the comma-separated list. A comma
            # in a lane that passed (or was skipped) cannot break the join, so
            # it never blocks the confirmation; one in a failing lane is written
            # as ';', the same way the current run's lanes are written.
            if re.search(r"[\x00-\x1f\x7f]", name):
                raise ValueError(f"failed lane name has a control character: {name!r}")
            lanes.append(name.replace(",", ";"))
    return ",".join(lanes)


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "check-id":
        check_id = linked_check_id(json.load(sys.stdin), sys.argv[2])
        if check_id is None:
            raise ValueError("invalid job check_run_url")
        print(check_id)
        return
    command, repo, branch, workflow, identity, *rest = sys.argv[1:]
    name = rest[0] if rest else "verified"
    if command == "history":
        # Require complete history: no undocumented provider ordering can
        # establish the predecessor from a partial set. Exhausted bounds fail.
        rows = paged(f"repos/{repo}/actions/workflows/{workflow.rsplit('/', 1)[-1]}/runs?"
                     + urlencode({"event": "push", "branch": branch}), "workflow_runs")
        print(json.dumps({"total_count": len(rows), "workflow_runs": rows}))
    elif command == "failed-lanes":
        print(failed_lanes(repo, identity))
    elif command == "base":
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
