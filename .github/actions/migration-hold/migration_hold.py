#!/usr/bin/env python3
"""Queue hold for red-main migration culprits (see action.yml)."""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request


def pr_number(head_ref: str) -> int | None:
    """`refs/heads/gh-readonly-queue/main/pr-123-<sha>` -> 123."""
    m = re.search(r"/pr-(\d+)-[0-9a-f]+$", head_ref or "")
    return int(m.group(1)) if m else None


def decide(event: str, open_holds: list[int], pr_labels: list[str], fix_label: str) -> tuple[str, str]:
    """Return ('pass'|'fail', message)."""
    if event != "merge_group":
        return "pass", "not a merge-queue group: the hold applies to the queue only"
    if not open_holds:
        return "pass", "no open migration hold"
    if fix_label in pr_labels:
        return "pass", f"held by {holds(open_holds)}, but this pull request carries {fix_label}"
    return "fail", (
        f"a migration hold is open ({holds(open_holds)}): only a pull request labelled {fix_label} "
        "may merge until it is resolved"
    )


def holds(numbers: list[int]) -> str:
    return ", ".join(f"#{n}" for n in sorted(numbers))


def api(path: str, token: str) -> object:
    req = urllib.request.Request(
        f"https://api.github.com/{path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    return json.load(urllib.request.urlopen(req, timeout=30))


def main() -> int:
    env = os.environ
    event = env.get("EVENT", "")
    if event != "merge_group":
        print(decide(event, [], [], "")[1])
        return 0
    repo, token = env["REPO"], env["TOKEN"]
    label, fix = env.get("HOLD_LABEL", "red-main-migration-hold"), env.get("FIX_LABEL", "red-main-forward-fix")
    try:
        issues = api(f"repos/{repo}/issues?state=open&labels={label}&per_page=100", token)
        open_holds = [i["number"] for i in issues if "pull_request" not in i]
        number = pr_number(env.get("HEAD_REF", ""))
        labels: list[str] = []
        if open_holds and number is not None:
            labels = [l["name"] for l in api(f"repos/{repo}/issues/{number}", token).get("labels", [])]
        elif open_holds:
            print("::warning::the pull request of this group could not be identified")
    except Exception as exc:  # a stuck queue is worse than a missed hold
        print(f"::warning::migration hold not checked: {exc}")
        return 0
    verdict, message = decide(event, open_holds, labels, fix)
    if verdict == "fail":
        print(f"::error::{message}")
        return 1
    print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
