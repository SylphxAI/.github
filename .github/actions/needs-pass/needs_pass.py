#!/usr/bin/env python3
"""Aggregate verdict over `toJSON(needs)`: see action.yml."""
from __future__ import annotations

import json
import os
import sys

PASS = {"success", "skipped"}


def names(value: str) -> set[str]:
    return {x.strip() for x in value.split(",") if x.strip()}


def verdict(needs: dict, required: set[str], advisory: set[str]) -> list[str]:
    """The reasons to fail; empty means pass."""
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
    bad = verdict(needs, names(os.environ.get("REQUIRED", "")), names(os.environ.get("ADVISORY", "")))
    for reason in bad:
        print(f"::error::{reason}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
