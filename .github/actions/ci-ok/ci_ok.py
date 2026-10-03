#!/usr/bin/env python3
"""Aggregate gate: wait for every other GitHub Actions check run on a commit, then pass or fail.

Used where a repository's checks live in several workflows (or path-filtered
ones), so one `ci-ok` job cannot `needs:` them all. It lists the latest check
runs on the commit through the REST API, ignores its own job and any names in
CI_OK_IGNORE, and waits until the set is complete and stable. Any failure,
cancellation, timeout, action_required or startup failure fails the gate;
success, skipped and neutral pass.

It fails closed when a check never ran:
- a GitHub Actions check suite that completed with a bad conclusion and no
  check runs is a workflow that failed to start (invalid YAML, a reusable
  workflow or permission it cannot get); it posts no check run, so the
  check-run list alone would read green;
- no other check run at all on the commit fails: a gate over nothing is not
  a pass (a repository whose every workflow is path-filtered may allow that
  on pull_request only, CI_OK_ALLOW_NONE_ON_PR; its merge group still fails);
- every name in CI_OK_REQUIRED must be present and have succeeded (skipped,
  neutral or missing fails).
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

BAD = {"failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale"}
ACTIONS_APP_ID = 15368  # GitHub Actions; the app every required ci-ok check is pinned to


def evaluate(runs: list[dict], ignore: set[str], actions_only: bool = True,
             suites: list[dict] | None = None,
             required: frozenset[str] | set[str] = frozenset(),
             allow_none: bool = False) -> tuple[str, list[str]]:
    """Return ('pending'|'fail'|'pass', details) for the given check runs.

    By default only GitHub Actions check runs count: other apps post deploy
    and preview statuses (for example sylphx/deploy, sylphx/preview) that are
    not source checks and can stay in progress for a long time.

    `suites` are the commit's GitHub Actions check suites: one that completed
    with a bad conclusion and zero check runs is a workflow that never started
    and fails the gate. `required` names must be present and have succeeded.
    No other check run fails unless `allow_none` (pull_request opt-in only).
    """
    relevant = [r for r in runs if r.get("name") not in ignore
                and (not actions_only or (r.get("app") or {}).get("slug", "github-actions") == "github-actions")]
    pending = [r["name"] for r in relevant if r.get("status") != "completed"]
    if pending:
        return "pending", sorted(pending)
    bad = [f'{r["name"]}={r.get("conclusion")}' for r in relevant if r.get("conclusion") in BAD]
    for suite in suites or []:
        if (suite.get("status") == "completed" and suite.get("conclusion") in BAD
                and not suite.get("latest_check_runs_count")):
            bad.append(f'workflow failed to start (check suite {suite.get("id")}, '
                       f'conclusion {suite.get("conclusion")}, 0 jobs)')
    concluded = {r["name"]: r.get("conclusion") for r in relevant}
    for name in sorted(required):
        if concluded.get(name) != "success":
            bad.append(f'{name}={concluded.get(name) or "missing"} (required)')
    if not relevant and not allow_none:
        bad.append("no other check ran on this commit")
    return ("fail", sorted(bad)) if bad else ("pass", sorted(r["name"] for r in relevant))


def _get(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    return json.load(urllib.request.urlopen(req, timeout=30))


def fetch(repo: str, sha: str, token: str) -> list[dict]:
    runs, page = [], 1
    while True:
        data = _get(f"https://api.github.com/repos/{repo}/commits/{sha}/check-runs"
                    f"?filter=latest&per_page=100&page={page}", token)
        runs += data.get("check_runs", [])
        if len(runs) >= data.get("total_count", 0) or not data.get("check_runs"):
            return runs
        page += 1


def fetch_suites(repo: str, sha: str, token: str) -> list[dict]:
    """GitHub Actions check suites on the commit (needs only `checks: read`)."""
    suites, page = [], 1
    while True:
        data = _get(f"https://api.github.com/repos/{repo}/commits/{sha}/check-suites"
                    f"?app_id={ACTIONS_APP_ID}&per_page=100&page={page}", token)
        suites += data.get("check_suites", [])
        if len(suites) >= data.get("total_count", 0) or not data.get("check_suites"):
            return suites
        page += 1


def main() -> int:
    repo, sha, token = os.environ["REPO"], os.environ["SHA"], os.environ["TOKEN"]
    ignore = {os.environ.get("SELF_NAME", "ci-ok")} | {
        n.strip() for n in os.environ.get("CI_OK_IGNORE", "").split(",") if n.strip()}
    required = {n.strip() for n in os.environ.get("CI_OK_REQUIRED", "").split(",") if n.strip()}
    interval = int(os.environ.get("CI_OK_INTERVAL", "20"))
    deadline = time.time() + 60 * int(os.environ.get("CI_OK_TIMEOUT_MINUTES", "110"))
    time.sleep(int(os.environ.get("CI_OK_SETTLE", "45")))  # let every workflow register its runs
    last, stable = None, 0
    while time.time() < deadline:
        try:
            state, detail = evaluate(fetch(repo, sha, token), ignore,
                                     os.environ.get("CI_OK_ALL_APPS", "false") != "true",
                                     fetch_suites(repo, sha, token), required,
                                     os.environ.get("CI_OK_ALLOW_NONE_ON_PR", "false") == "true"
                                     and os.environ.get("EVENT_NAME") == "pull_request")
        except Exception as exc:  # transient API error: keep waiting
            print(f"check-runs read failed: {exc}", flush=True)
            time.sleep(interval)
            continue
        if state == "pending":
            print(f"waiting on: {', '.join(detail)}", flush=True)
            last, stable = None, 0
        else:
            stable = stable + 1 if detail == last else 1
            last = detail
            if stable >= 2:  # same complete result twice in a row: nothing late-registered
                if state == "fail":
                    print("::error::failed checks: " + ", ".join(detail))
                    return 1
                print("all checks passed: " + (", ".join(detail) or "(none)"))
                return 0
        time.sleep(interval)
    print("::error::timed out waiting for checks")
    return 1


if __name__ == "__main__":
    sys.exit(main())
