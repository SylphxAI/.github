#!/usr/bin/env python3
"""Stop the line: while the trunk is red, the merge queue admits only the revert or the fix.

Andon for the merge queue (docs/optimistic-merge.md). It runs in a merge group
only. The trunk is red when the newest conclusive push run of the verify
workflow on the trunk failed; a later success clears it. A cancelled, skipped
or superseded run is not an answer and is passed over (owner standards/dx.md:
a red trunk counts only a completed failure of the verifying run).

While red, a merge group is admitted only when its pull request is the way back
to green: a revert (branch `auto-revert/*`, a title starting `Revert`, or the
red-main handler's `auto-revert` / `queue-jump:red-main` labels) or a pull
request labelled as the fix. Everything else fails this check and leaves the
queue; it is re-enqueued once the trunk is green.

Fail open: a state that cannot be read (API error, no verify run in sight)
admits, with a warning. A gate that jams the queue on its own fault is worse
than the red it guards against.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request

FAILED = {"failure", "timed_out", "action_required", "startup_failure"}
REVERT_LABELS = {"auto-revert", "queue-jump:red-main"}
MERGE_GROUP_PR = re.compile(r"^(?:refs/heads/)?gh-readonly-queue/[^/].*?/pr-(\d+)-[0-9a-f]{7,40}$")


def trunk_state(runs: list[dict], branch: str) -> tuple[str, dict | None]:
    """('red'|'green'|'unknown', the deciding run) from push runs, newest first."""
    for run in runs:
        if run.get("event") != "push" or run.get("head_branch") != branch or run.get("status") != "completed":
            continue
        conclusion = run.get("conclusion")
        if conclusion in FAILED:
            return "red", run
        if conclusion == "success":
            return "green", run
        # cancelled, skipped, neutral, stale: superseded or not an answer
    return "unknown", None


def pr_number(head_ref: str) -> int | None:
    match = MERGE_GROUP_PR.match(head_ref or "")
    return int(match.group(1)) if match else None


def is_way_back(pull: dict, fix_label: str) -> str | None:
    """Why this pull request is a revert or the fix, or None."""
    labels = {(label.get("name") or "") for label in pull.get("labels", [])}
    head = (pull.get("head") or {}).get("ref") or ""
    title = (pull.get("title") or "").strip()
    if head.startswith("auto-revert/") or labels & REVERT_LABELS:
        return "revert from the red-main handler"
    if title.lower().startswith("revert"):
        return "revert"
    if fix_label and fix_label in labels:
        return f"labelled {fix_label}"
    return None


def decide(state: str, pull: dict | None, mode: str, fix_label: str) -> tuple[bool, str]:
    """(admit, message). `pull` None means the pull request could not be read."""
    if state != "red":
        return True, f"trunk is {state}: admitted"
    if pull is None:
        return True, "::warning::trunk is red but the pull request could not be read: admitted"
    why = is_way_back(pull, fix_label)
    if why:
        return True, f"trunk is red; this pull request is a way back to green ({why}): admitted"
    message = (f"trunk is red: only a revert or a pull request labelled `{fix_label}` is admitted until "
               f"Verify is green again; this pull request is neither. Re-enqueue it once the trunk is green.")
    if mode != "enforce":
        return True, f"::warning::observe mode, would have refused: {message}"
    return False, f"::error::{message}"


def _get(url: str, token: str) -> dict | list:
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    return json.load(urllib.request.urlopen(req, timeout=30))


def main() -> int:
    env = os.environ
    if env.get("EVENT_NAME") != "merge_group":
        print("not a merge group: nothing to gate")
        return 0
    repo, token, branch = env["REPO"], env["TOKEN"], env.get("BASE_BRANCH", "main").removeprefix("refs/heads/")
    mode, fix_label = env.get("MODE", "enforce"), env.get("FIX_LABEL", "main-red-fix")
    api = f"https://api.github.com/repos/{repo}"
    try:
        data = _get(f"{api}/actions/workflows/{env['VERIFY_WORKFLOW']}/runs"
                    f"?event=push&branch={branch}&status=completed&per_page=30", token)
        state, run = trunk_state(data.get("workflow_runs", []), branch)
    except Exception as exc:
        print(f"::warning::cannot read {env['VERIFY_WORKFLOW']} runs ({exc}): admitted")
        return 0
    if state != "red":
        print(f"trunk is {state}" + (f" (run {run['id']})" if run else "") + ": admitted")
        return 0
    print(f"trunk is red: {run.get('html_url')} on {run.get('head_sha', '')[:9]} concluded {run.get('conclusion')}")
    number = pr_number(env.get("MERGE_GROUP_REF", ""))
    pull = None
    if number is not None:
        try:
            pull = _get(f"{api}/pulls/{number}", token)
        except Exception as exc:
            print(f"::warning::cannot read pull request {number} ({exc})")
    admit, message = decide(state, pull, mode, fix_label)
    print(message)
    return 0 if admit else 1


if __name__ == "__main__":
    sys.exit(main())
