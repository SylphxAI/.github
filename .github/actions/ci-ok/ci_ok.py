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
  check-run list alone would read green. Only a pull_request, merge_group or
  pull_request_target workflow counts (read through `actions: read`); when the
  event cannot be read, every such suite counts;
- no other check run at all on the commit fails: a gate over nothing is not
  a pass (a repository whose every workflow is path-filtered may allow that
  on pull_request only, CI_OK_ALLOW_NONE_ON_PR; its merge group still fails);
- every name in CI_OK_REQUIRED must be present and have succeeded (skipped,
  neutral or missing fails).

Each check is judged by its latest run: the check-runs list keeps the latest
run per check suite, so a run cancelled by a newer run of the same workflow on
the same commit (concurrency) is still listed beside the newer one. A run in a
superseded check suite does not count; without `actions: read`, an older
cancelled or stale run yields to a newer run of the same name.

It never depends on API budget it does not have: when the token's rate limit
is spent until after the deadline, the gate fails at once and says so, rather
than reading green or polling until it times out; a low budget slows polling.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

class RateLimited(Exception):
    """The token's API budget is spent; `reset` is when it returns (epoch seconds)."""

    def __init__(self, reset: float) -> None:
        super().__init__(f"GitHub API rate limit exhausted until {time.strftime('%H:%M:%SZ', time.gmtime(reset))}")
        self.reset = reset


BAD = {"failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale"}
ACTIONS_APP_ID = 15368  # GitHub Actions; the app every required ci-ok check is pinned to
# Events whose workflows gate a change. A workflow_run or push workflow that
# fails to start on the same commit (a broken red-main.yml on main lands on the
# merge-group SHA) is not a check this gate waits for, and counting it would
# fail every merge group, including the one that fixes it.
GATING_EVENTS = {"pull_request", "pull_request_target", "merge_group"}


def evaluate(runs: list[dict], ignore: set[str], actions_only: bool = True,
             suites: list[dict] | None = None,
             required: frozenset[str] | set[str] = frozenset(),
             allow_none: bool = False,
             suite_events: dict[int, str] | None = None) -> tuple[str, list[str]]:
    """Return ('pending'|'fail'|'pass', details) for the given check runs.

    By default only GitHub Actions check runs count: other apps post deploy
    and preview statuses (for example sylphx/deploy, sylphx/preview) that are
    not source checks and can stay in progress for a long time.

    `suites` are the commit's GitHub Actions check suites: one that completed
    with a bad conclusion and zero check runs is a workflow that never started
    and fails the gate. `required` names must be present and have succeeded.
    No other check run fails unless `allow_none` (pull_request opt-in only).
    `suite_events` maps a check suite id to its workflow run's event; a suite
    whose event is known and not a gating event is ignored. An unfinished empty
    suite with a known gating event waits for its jobs, including replacements
    of superseded suites.
    """
    relevant = latest_runs([r for r in runs if r.get("name") not in ignore
                            and (not actions_only or (r.get("app") or {}).get("slug", "github-actions") == "github-actions")],
                           suite_events)
    pending = [r["name"] for r in relevant if r.get("status") != "completed"]
    pending += [f'workflow has no jobs yet (check suite {s.get("id")})'
                for s in suites or []
                if s.get("status") != "completed" and not s.get("latest_check_runs_count")
                and (suite_events or {}).get(s.get("id")) in GATING_EVENTS]
    if pending:
        return "pending", sorted(pending)
    bad = [f'{r["name"]}={r.get("conclusion")}' for r in relevant if r.get("conclusion") in BAD]
    for suite in suites or []:
        event = (suite_events or {}).get(suite.get("id"))
        if (suite.get("status") == "completed" and suite.get("conclusion") in BAD
                and not suite.get("latest_check_runs_count")
                and (event is None or event in GATING_EVENTS)):
            bad.append(f'workflow failed to start (check suite {suite.get("id")}, '
                       f'conclusion {suite.get("conclusion")}, 0 jobs)')
    concluded = {r["name"]: r.get("conclusion") for r in relevant}
    for name in sorted(required):
        if concluded.get(name) != "success":
            bad.append(f'{name}={concluded.get(name) or "missing"} (required)')
    if not relevant and not allow_none:
        bad.append("no other check ran on this commit")
    return ("fail", sorted(bad)) if bad else ("pass", sorted(r["name"] for r in relevant))


def _started(r: dict) -> tuple[str, int]:
    return (r.get("started_at") or "", r.get("id") or 0)


def latest_runs(runs: list[dict], suite_events: dict[int, str] | None) -> list[dict]:
    """Drop runs that a newer run of the same check replaced on this commit.

    A run whose check suite is superseded (a later run of the same workflow
    and event) never counts. Without workflow-run data, an older cancelled or
    stale run yields to a newer run of the same name; any other older run still
    counts, since two workflows may each have a job of that name.
    """
    if suite_events:
        runs = [r for r in runs if suite_events.get((r.get("check_suite") or {}).get("id")) != "superseded"]
    newest: dict[str, dict] = {}
    for r in runs:
        if r.get("name") not in newest or _started(r) > _started(newest[r["name"]]):
            newest[r["name"]] = r
    return [r for r in runs if newest[r["name"]] is r
            or r.get("conclusion") not in ("cancelled", "stale")]


def needs_suite_events(runs: list[dict], ignore: set[str]) -> bool:
    """True when a check has more than one run and one of them is bad: only
    the workflow runs tell a superseded run from a second workflow's job."""
    names: dict[str, int] = {}
    for r in runs:
        if r.get("name") not in ignore:
            names[r["name"]] = names.get(r["name"], 0) + 1
    return any(names.get(r.get("name"), 0) > 1 and r.get("conclusion") in BAD for r in runs)


def rate_limited(exc: urllib.error.HTTPError, now: float | None = None) -> float | None:
    """When `exc` is a rate-limit response, the epoch second the budget returns."""
    now = time.time() if now is None else now
    headers = exc.headers or {}
    if exc.code in (403, 429) and headers.get("retry-after"):
        return now + float(headers["retry-after"])
    if exc.code in (403, 429) and headers.get("x-ratelimit-remaining") == "0":
        return float(headers.get("x-ratelimit-reset") or now + 60)
    return None


def poll_interval(base: int, remaining: int | None) -> int:
    """Poll at `base` seconds, and no faster than once a minute on a low budget
    (the repository's GITHUB_TOKEN budget is shared by every concurrent job)."""
    return max(base, 60) if remaining is not None and remaining < 200 else base


_budget: dict[str, int | None] = {"remaining": None}


def _get(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    try:
        resp = urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as exc:
        reset = rate_limited(exc)
        if reset is not None:
            raise RateLimited(reset) from exc
        raise
    remaining = resp.headers.get("x-ratelimit-remaining")
    _budget["remaining"] = int(remaining) if remaining and remaining.isdigit() else None
    return json.load(resp)


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


def needs_workflow_events(suites: list[dict]) -> bool:
    """Identify empty suites whose pending or failed workflow may gate the change."""
    return any(not s.get("latest_check_runs_count")
               and (s.get("status") != "completed" or s.get("conclusion") in BAD)
               for s in suites)


def fetch_suite_events(repo: str, sha: str, token: str) -> dict[int, str] | None:
    """check_suite_id -> event of the commit's workflow runs; None when unreadable
    (no `actions: read`), so every failed empty suite then counts."""
    try:
        data = _get(f"https://api.github.com/repos/{repo}/actions/runs?head_sha={sha}&per_page=100", token)
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 404):
            print("::warning::cannot read workflow runs (grant actions: read); "
                  "every workflow that failed to start counts", flush=True)
            return None
        raise
    return suite_events(data.get("workflow_runs", []))


def suite_events(workflow_runs: list[dict]) -> dict[int, str]:
    """check_suite_id -> event. A run superseded by a later run of the same
    workflow and event on the commit maps to "superseded": a workflow that
    failed to start once (a transient reusable-workflow or permission fault)
    and then ran on a re-trigger is judged by the later run's check runs."""
    latest: dict[tuple[str, str], str] = {}
    for r in workflow_runs:
        key = (r.get("path", ""), r.get("event", ""))
        latest[key] = max(latest.get(key, ""), r.get("created_at", ""))
    return {r["check_suite_id"]: (r.get("event", "") if r.get("created_at", "") >= latest[(r.get("path", ""), r.get("event", ""))]
                                  else "superseded")
            for r in workflow_runs}


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
            suites = fetch_suites(repo, sha, token)
            runs = fetch(repo, sha, token)
            events = (fetch_suite_events(repo, sha, token)
                      if needs_workflow_events(suites) or needs_suite_events(runs, ignore) else None)
            state, detail = evaluate(runs, ignore,
                                     os.environ.get("CI_OK_ALL_APPS", "false") != "true",
                                     suites, required,
                                     os.environ.get("CI_OK_ALLOW_NONE_ON_PR", "false") == "true"
                                     and os.environ.get("EVENT_NAME") == "pull_request",
                                     events)
        except RateLimited as exc:
            if exc.reset >= deadline:
                print(f"::error::{exc}, past this gate's deadline: ci-ok cannot read the checks "
                      "and fails closed; re-run it after the reset")
                return 1
            print(f"{exc}; waiting for the reset", flush=True)
            time.sleep(max(0.0, exc.reset - time.time()) + 5)
            continue
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
        time.sleep(poll_interval(interval, _budget["remaining"]))
    print("::error::timed out waiting for checks")
    return 1


if __name__ == "__main__":
    sys.exit(main())
