#!/usr/bin/env python3
"""Stop the line: while the trunk is red and its way back is in motion, the merge queue admits only the revert or the fix.

Andon for the merge queue (docs/optimistic-merge.md). It runs in a merge group
only. The trunk is red when the newest conclusive push run of the verify
workflow on the trunk failed; a later success clears it. A cancelled, skipped
or superseded run is not an answer and is passed over (owner standards/dx.md:
a red trunk counts only a completed failure of the verifying run).

While red, a merge group is admitted when its pull request is the way back to
green: a revert (branch `auto-revert/*`, a title starting `Revert`, or the
red-main handler's `auto-revert` / `queue-jump:red-main` labels), a live-outage
fix (`queue-jump:outage`, which the handler never reverts either) or a pull
request labelled as the fix.

Any other pull request is refused only while the way back is in motion: a newer
run of the verify workflow on the trunk is still running, or a revert or fix
pull request is open. Otherwise it is admitted with a warning. The red-main
handler reverts only after the same failure repeats on a second completed run,
and never reverts an infrastructure failure, a timed-out run or a run that did
not start; a queue that stayed shut on the first red would wait for a run that
nothing can start. The next change landing is what lets the handler decide. A
person can always pass a change with the fix label.

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
OUTAGE_LABEL = "queue-jump:outage"
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


def newer_unfinished_run(runs: list[dict], branch: str, deciding: dict | None) -> dict | None:
    """The newest push run on the trunk that has not finished and is newer than `deciding` (runs are newest first)."""
    for run in runs:
        if run is deciding:
            return None
        if run.get("event") == "push" and run.get("head_branch") == branch and run.get("status") != "completed":
            return run
    return None


def pr_number(head_ref: str) -> int | None:
    match = MERGE_GROUP_PR.match(head_ref or "")
    return int(match.group(1)) if match else None


def is_way_back(pull: dict, fix_label: str) -> str | None:
    """Why this pull request is a revert, an outage fix or the fix, or None."""
    labels = {(label.get("name") or "") for label in pull.get("labels", [])}
    head = (pull.get("head") or {}).get("ref") or ""
    title = (pull.get("title") or "").strip()
    if head.startswith("auto-revert/") or labels & REVERT_LABELS:
        return "revert from the red-main handler"
    if title.lower().startswith("revert"):
        return "revert"
    if OUTAGE_LABEL in labels:
        return "live-outage fix"
    if fix_label and fix_label in labels:
        return f"labelled {fix_label}"
    return None


def open_way_back(pulls: list[dict], fix_label: str) -> str | None:
    """Describe an open, non-draft revert or fix pull request (a draft cannot be queued), or None."""
    for pull in pulls:
        why = None if pull.get("draft") else is_way_back(pull, fix_label)
        if why:
            return f"pull request #{pull.get('number')} ({why}) is open"
    return None


def decide(state: str, pull: dict | None, mode: str, fix_label: str, motion: str | None) -> tuple[bool, str]:
    """(admit, message). `pull` None means the pull request could not be read; `motion` says what is already
    on its way to green (a running verify run, an open revert or fix), or None when nothing is."""
    if state != "red":
        return True, f"trunk is {state}: admitted"
    if pull is None:
        return True, "::warning::trunk is red but the pull request could not be read: admitted"
    why = is_way_back(pull, fix_label)
    if why:
        return True, f"trunk is red; this pull request is a way back to green ({why}): admitted"
    if motion is None:
        return True, ("::warning::trunk is red but nothing is on its way to green (no newer Verify run is running "
                      f"and no revert or `{fix_label}` pull request is open): admitted, so that the next completed "
                      "run can confirm the failure and let the red-main handler act")
    message = (f"trunk is red and {motion}: only a revert or a pull request labelled `{fix_label}` is admitted until "
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
                    f"?event=push&branch={branch}&per_page=30", token)
        runs = data.get("workflow_runs", [])
        state, run = trunk_state(runs, branch)
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
    motion = None
    if pull is not None and not is_way_back(pull, fix_label):
        running = newer_unfinished_run(runs, branch, run)
        if running:
            motion = f"a newer {env['VERIFY_WORKFLOW']} run is still running ({running.get('html_url')})"
        else:
            try:
                motion = open_way_back(_get(f"{api}/pulls?state=open&per_page=100", token), fix_label)
            except Exception as exc:
                print(f"::warning::cannot list open pull requests ({exc}): admitted")
                return 0
    admit, message = decide(state, pull, mode, fix_label, motion)
    print(message)
    return 0 if admit else 1


if __name__ == "__main__":
    sys.exit(main())
