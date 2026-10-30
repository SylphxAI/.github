#!/usr/bin/env python3
"""Fail-closed verdict over a workflow's explicit job dependencies.

The caller lists its required jobs in both `needs:` and `required`, and runs
its verdict job with `if: always()`. GitHub schedules the verdict only after
those dependencies finish; a check-run inventory cannot establish that fact.
The shared needs-pass verdict permits optional path-filtered lanes to skip,
but every explicitly required job must be present and have succeeded.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "needs-pass"))
from needs_pass import names, verdict


def evaluate(needs: object, required: set[str]) -> list[str]:
    """Return every reason the declared dependencies cannot pass the gate."""
    if not isinstance(needs, dict) or not needs:
        return ["needs must be a nonempty object from toJSON(needs)"]
    if not required:
        return ["required must name the jobs that must run and succeed"]
    for job, value in needs.items():
        if (not isinstance(job, str) or not job or not isinstance(value, dict)
                or ("result" in value and not isinstance(value["result"], (str, type(None))))):
            return ["needs must map job IDs to their result objects"]
    return verdict(needs, required, set(), os.environ.get("EVENT_NAME", ""))


def main() -> int:
    try:
        needs = json.loads(os.environ.get("NEEDS", ""))
    except (json.JSONDecodeError, TypeError):
        print("::error::needs must be the calling job's toJSON(needs)")
        return 1
    bad = evaluate(needs, names(os.environ.get("REQUIRED", "")))
    for reason in bad:
        print(f"::error::{reason}")
    if not bad:
        print("all required jobs succeeded: " + ", ".join(sorted(names(os.environ["REQUIRED"]))))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
