#!/usr/bin/env python3
"""Aggregate verdict over `toJSON(needs)`: see action.yml.

A cancelled dependency is not a failure: a pull-request push supersedes
the run before it (`concurrency.cancel-in-progress`), so its lanes read
`cancelled` while nothing was wrong. The aggregate reports them as CANCELLED,
distinct from FAILED, and does not fail on them; a rerun produces the lanes'
real results, which then decide.
"""
from __future__ import annotations

import json
import os
import sys

PASS = {"success", "skipped"}
CANCELLED = "cancelled"


def names(value: str) -> set[str]:
    return {x.strip() for x in value.split(",") if x.strip()}


def cancel_class(job: str, event: str) -> str:
    """Why a `cancelled` lane is not a failure, from what the action already has.

    The org's workflows cancel in progress only on `pull_request`
    (`cancel-in-progress: ${{ github.event_name == 'pull_request' }}`), so a
    cancelled lane there is a superseded push; on any other event nothing
    cancels the run by concurrency, so the run was stopped.
    """
    return f"{job}: CANCELLED (superseded push)" if event == "pull_request" \
        else f"{job}: CANCELLED (stopped run)"


def verdict(needs: dict, required: set[str], advisory: set[str],
            event: str = "", required_unless_merge_group: frozenset[str] | set[str] = frozenset()) -> list[str]:
    """The reasons to fail; empty means pass.

    Jobs in `required_unless_merge_group` must succeed on every event except
    merge_group, where the queue by design does not run them and skipped is fine.
    A `cancelled` result never fails, even in `required`/`required_unless_merge_group`:
    a superseded or stopped run has no verdict of its own, and the rerun settles it.
    """
    if event != "merge_group":
        required = required | set(required_unless_merge_group)
    bad = []
    for job in sorted(required - set(needs)):
        bad.append(f"{job}: required but not in needs")
    for job, value in sorted(needs.items()):
        result = (value or {}).get("result", "")
        if result == CANCELLED:
            continue
        if job in required and result != "success":
            bad.append(f"{job}: {result or 'no result'} (must succeed)")
        elif job not in advisory and result not in PASS:
            bad.append(f"{job}: {result or 'no result'}")
    return bad


def main() -> int:
    needs = json.loads(os.environ["NEEDS"] or "{}")
    event = os.environ.get("EVENT_NAME", "")
    print(json.dumps({job: (value or {}).get("result") for job, value in needs.items()}))
    for job in sorted(j for j, v in needs.items() if (v or {}).get("result") == CANCELLED):
        print(f"::warning::{cancel_class(job, event)} - not a failure; re-run to settle it")
    bad = verdict(needs, names(os.environ.get("REQUIRED", "")), names(os.environ.get("ADVISORY", "")),
                  event, names(os.environ.get("REQUIRED_UNLESS_MERGE_GROUP", "")))
    for reason in bad:
        print(f"::error::{reason}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
