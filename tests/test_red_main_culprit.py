#!/usr/bin/env python3
"""Tests for the red-main culprit rule (CULPRIT_PY in red-main.yml).

Every candidate is verified at once; the culprit is the oldest failure whose
older candidates all passed. A cancelled, timed-out or unfinished run is
inconclusive and is never blamed.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/red-main.yml"
SCRIPT = yaml.safe_load(WORKFLOW.read_text())["jobs"]["red-main"]["env"]["CULPRIT_PY"]


def decide(*conclusions: str) -> list[str]:
    """Candidates c1..cN, oldest first, with the given conclusions."""
    rows = "".join(f"c{i + 1}\t{c}\n" for i, c in enumerate(conclusions))
    out = subprocess.run(
        [sys.executable, "-c", SCRIPT], input=rows, capture_output=True, text=True, check=True
    ).stdout
    return out.splitlines()


class CulpritRuleTest(unittest.TestCase):
    def test_all_pass(self) -> None:
        self.assertEqual(decide("success", "success", "success"), ["none"])

    def test_oldest_failure_with_passing_ancestors(self) -> None:
        self.assertEqual(decide("success", "failure", "failure")[0], "culprit c2")

    def test_first_candidate_fails(self) -> None:
        self.assertEqual(decide("failure", "success")[0], "culprit c1")

    def test_cancelled_before_first_failure_reverts_window(self) -> None:
        self.assertEqual(decide("success", "cancelled", "failure")[0], "window")

    def test_startup_failure_before_first_failure_reverts_window(self) -> None:
        self.assertEqual(decide("startup_failure", "failure")[0], "window")

    def test_timeout_reverts_window(self) -> None:
        self.assertEqual(decide("success", "timeout", "success")[0], "window")

    def test_timed_out_conclusion_reverts_window(self) -> None:
        self.assertEqual(decide("timed_out", "success")[0], "window")

    def test_failure_then_cancelled_after_it_names_the_failure(self) -> None:
        self.assertEqual(decide("success", "failure", "cancelled")[0], "culprit c2")

    def test_window_says_why(self) -> None:
        out = decide("success", "cancelled")
        self.assertEqual(out[0], "window")
        self.assertIn("cancelled", out[1])


if __name__ == "__main__":
    unittest.main()
