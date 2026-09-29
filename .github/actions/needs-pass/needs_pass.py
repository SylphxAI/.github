#!/usr/bin/env python3
"""Aggregate verdict over `toJSON(needs)`: see action.yml."""
from __future__ import annotations

import json
import os
import sys

PASS = {"success", "skipped"}


def names(value: str) -> set[str]:
    return {x.strip() for x in value.split(",") if x.strip()}


def verdict(needs: dict, required: set[str], advisory: set[str],
            event: str = "", required_unless_merge_group: frozenset[str] | set[str] = frozenset()) -> list[str]:
    """The reasons to fail; empty means pass.

    Jobs in `required_unless_merge_group` must succeed on every event except
    merge_group, where the queue by design does not run them and skipped is fine.
    """
    if event != "merge_group":
        required = required | set(required_unless_merge_group)
    bad = []
    for job in sorted(required - set(needs)):
        bad.append(f"{job}: required but not in needs")
    for job, value in sorted(needs.items()):
        result = (value or {}).get("result", "")
        if job in required and result != "success":
            bad.append(f"{job}: {result or 'no result'} (must succeed)")
        elif job not in advisory and result not in PASS:
            bad.append(f"{job}: {result or 'no result'}")
    return bad


def main() -> int:
    needs = json.loads(os.environ["NEEDS"] or "{}")
    print(json.dumps({job: (value or {}).get("result") for job, value in needs.items()}))
    bad = verdict(needs, names(os.environ.get("REQUIRED", "")), names(os.environ.get("ADVISORY", "")),
                  os.environ.get("EVENT_NAME", ""), names(os.environ.get("REQUIRED_UNLESS_MERGE_GROUP", "")))
    for reason in bad:
        print(f"::error::{reason}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
