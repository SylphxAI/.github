#!/usr/bin/env python3
"""Aggregate gate: wait for every other GitHub Actions check run on a commit, then pass or fail.

Used where a repository's checks live in several workflows (or path-filtered
ones), so one `ci-ok` job cannot `needs:` them all. It lists the latest check
runs on the commit through the REST API, ignores its own job and any names in
CI_OK_IGNORE, and waits until the set is complete and stable. Any failure,
cancellation, timeout, action_required or startup failure fails the gate;
success, skipped and neutral pass.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

BAD = {"failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale"}


def evaluate(runs: list[dict], ignore: set[str], actions_only: bool = True) -> tuple[str, list[str]]:
    """Return ('pending'|'fail'|'pass', details) for the given check runs.

    By default only GitHub Actions check runs count: other apps post deploy
    and preview statuses (for example sylphx/deploy, sylphx/preview) that are
    not source checks and can stay in progress for a long time.
    """
    relevant = [r for r in runs if r.get("name") not in ignore
                and (not actions_only or (r.get("app") or {}).get("slug", "github-actions") == "github-actions")]
    pending = [r["name"] for r in relevant if r.get("status") != "completed"]
    if pending:
        return "pending", sorted(pending)
    bad = [f'{r["name"]}={r.get("conclusion")}' for r in relevant if r.get("conclusion") in BAD]
    return ("fail", sorted(bad)) if bad else ("pass", sorted(r["name"] for r in relevant))


def fetch(repo: str, sha: str, token: str) -> list[dict]:
    runs, page = [], 1
    while True:
        url = f"https://api.github.com/repos/{repo}/commits/{sha}/check-runs?filter=latest&per_page=100&page={page}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
        data = json.load(urllib.request.urlopen(req, timeout=30))
        runs += data.get("check_runs", [])
        if len(runs) >= data.get("total_count", 0) or not data.get("check_runs"):
            return runs
        page += 1


def main() -> int:
    repo, sha, token = os.environ["REPO"], os.environ["SHA"], os.environ["TOKEN"]
    ignore = {os.environ.get("SELF_NAME", "ci-ok")} | {
        n.strip() for n in os.environ.get("CI_OK_IGNORE", "").split(",") if n.strip()}
    interval = int(os.environ.get("CI_OK_INTERVAL", "20"))
    deadline = time.time() + 60 * int(os.environ.get("CI_OK_TIMEOUT_MINUTES", "110"))
    time.sleep(int(os.environ.get("CI_OK_SETTLE", "45")))  # let every workflow register its runs
    last, stable = None, 0
    while time.time() < deadline:
        try:
            state, detail = evaluate(fetch(repo, sha, token), ignore,
                                     os.environ.get("CI_OK_ALL_APPS", "false") != "true")
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
