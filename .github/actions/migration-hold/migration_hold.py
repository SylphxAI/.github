#!/usr/bin/env python3
"""Queue hold for red-main migration culprits (see action.yml).

On `merge_group` it fails CLOSED: a token that cannot read issues would
otherwise switch the hold off for good, and nobody reads warnings on a green
queue. The way out of an error is to fix the token or re-enqueue once the API
is back; the way out of a hold is to merge the forward-fix and close the issue.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

RETRIES = 3


def pr_number(head_ref: str) -> int | None:
    """`refs/heads/gh-readonly-queue/main/pr-123-<sha>` -> 123."""
    m = re.search(r"/pr-(\d+)-[0-9a-f]+$", head_ref or "")
    return int(m.group(1)) if m else None


def holds(numbers: list[int]) -> str:
    return ", ".join(f"#{n}" for n in sorted(numbers))


def decide(event: str, open_holds: list[int], group: dict[int, list[str]], fix_label: str) -> tuple[str, str]:
    """Return ('pass'|'fail', message). `group` maps each pull request in the
    merge group to its labels; an empty group with an open hold is unidentified
    and fails."""
    if event != "merge_group":
        return "pass", "not a merge-queue group: the hold applies to the queue only"
    if not open_holds:
        return "pass", "no open migration hold"
    way_out = (
        f"merge the forward-fix labelled {fix_label}, then close {holds(open_holds)}; "
        "every pull request in the group must carry the label"
    )
    if not group:
        return "fail", f"a migration hold is open ({holds(open_holds)}) and the pull requests of this group could not be identified: {way_out}"
    missing = sorted(n for n, labels in group.items() if fix_label not in labels)
    if missing:
        return "fail", f"a migration hold is open ({holds(open_holds)}); {holds(missing)} lack {fix_label}: {way_out}"
    return "pass", f"held by {holds(open_holds)}, but every pull request in this group carries {fix_label}"


def http_api(path: str, token: str) -> object:
    req = urllib.request.Request(
        f"https://api.github.com/{path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    return json.load(urllib.request.urlopen(req, timeout=30))


def with_retries(fn, sleep=time.sleep):
    last: Exception | None = None
    for attempt in range(RETRIES):
        try:
            return fn()
        except GroupError:
            raise
        except Exception as exc:  # 403, timeout, 5xx
            last = exc
            if attempt < RETRIES - 1:
                sleep(2 ** (attempt + 1))
    raise last  # type: ignore[misc]


class GroupError(Exception):
    """The group's pull requests cannot be fully identified (fails closed)."""


SUBJECT_PR = re.compile(r"\(#(\d+)\)\s*$")


def commit_pr(sha: str, subject: str, pulls) -> int | None:
    """A squash-queue commit has no associated pull request in the API, so the
    trailing `(#N)` of its subject names it; the API answer wins when present."""
    if pulls:
        return pulls[0]["number"]
    m = SUBJECT_PR.search(subject or "")
    return int(m.group(1)) if m else None


def group_labels(repo, token, base_sha, head_sha, head_ref, api) -> dict[int, list[str]]:
    """Every pull request in the group: the head ref's, and one for every
    commit in base...head (a batched group holds the entries ahead of it).
    A commit that maps to no pull request, or a compare that was capped,
    raises GroupError: an unidentified entry could be the unlabelled one."""
    numbers: set[int] = set()
    n = pr_number(head_ref)
    if n is not None:
        numbers.add(n)
    if base_sha and head_sha:
        cmp = api(f"repos/{repo}/compare/{base_sha}...{head_sha}", token)
        commits = cmp.get("commits", [])
        total = cmp.get("total_commits", len(commits))
        if total != len(commits):
            raise GroupError(f"compare listed {len(commits)} of {total} commits (capped)")
        for commit in commits:
            subject = (commit.get("commit", {}).get("message") or "").split("\n", 1)[0]
            number = commit_pr(commit["sha"], subject, api(f"repos/{repo}/commits/{commit['sha']}/pulls", token))
            if number is None:
                raise GroupError(f"commit {commit['sha'][:9]} maps to no pull request")
            numbers.add(number)
    return {
        num: [l["name"] for l in api(f"repos/{repo}/issues/{num}", token).get("labels", [])]
        for num in sorted(numbers)
    }


def main(env=None, api=http_api, sleep=time.sleep) -> int:
    env = os.environ if env is None else env
    event = env.get("EVENT", "")
    if event != "merge_group":
        print(decide(event, [], {}, "")[1])
        return 0
    repo, token = env["REPO"], env["TOKEN"]
    label = env.get("HOLD_LABEL", "red-main-migration-hold")
    fix = env.get("FIX_LABEL", "red-main-forward-fix")
    try:
        query = urllib.parse.quote(label, safe="")
        issues = with_retries(lambda: api(f"repos/{repo}/issues?state=open&labels={query}&per_page=100", token), sleep)
        open_holds = [i["number"] for i in issues if "pull_request" not in i]
        group: dict[int, list[str]] = {}
        if open_holds:
            group = with_retries(
                lambda: group_labels(repo, token, env.get("BASE_SHA", ""), env.get("HEAD_SHA", ""),
                                     env.get("HEAD_REF", ""), api),
                sleep,
            )
    except Exception as exc:
        print(f"::error::migration hold could not be checked ({exc}): give the job issues: read and "
              "pull-requests: read, or re-enqueue once the API is back")
        return 1
    verdict, message = decide(event, open_holds, group, fix)
    if verdict == "fail":
        print(f"::error::{message}")
        return 1
    print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
