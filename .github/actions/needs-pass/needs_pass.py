#!/usr/bin/env python3
"""Aggregate verdict over `toJSON(needs)`: see action.yml.

A cancelled dependency fails the verdict. A cancelled lane never tested the
head, so passing it lets a superseded or stopped run post a green verdict for
an untested commit (agents#4769: a run cancelled one second after it started
posted ci-ok success while its replacement was still queued). The failure is
labelled CANCELLED so it reads apart from a real FAILED lane; the replacement
run's verdict, which is newer, then decides the head.
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
    """Name why a lane was cancelled, from what the action already has.

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
    A `cancelled` result always fails: the lane never tested the head.
    """
    if event != "merge_group":
        required = required | set(required_unless_merge_group)
    bad = []
    for job in sorted(required - set(needs)):
        bad.append(f"{job}: required but not in needs")
    for job, value in sorted(needs.items()):
        result = (value or {}).get("result", "")
        if result == CANCELLED:
            bad.append(cancel_class(job, event))
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
    bad = verdict(needs, names(os.environ.get("REQUIRED", "")), names(os.environ.get("ADVISORY", "")),
                  event, names(os.environ.get("REQUIRED_UNLESS_MERGE_GROUP", "")))
    for reason in bad:
        print(f"::error::{reason}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
