#!/usr/bin/env python3
"""Tests for the needs-pass aggregate verdict."""

from __future__ import annotations

import importlib.util
import pathlib
import unittest

SPEC = importlib.util.spec_from_file_location(
    "needs_pass",
    pathlib.Path(__file__).resolve().parents[1] / ".github" / "actions" / "needs-pass" / "needs_pass.py")
needs_pass = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(needs_pass)


def needs(**results: str) -> dict:
    return {job: {"result": result} for job, result in results.items()}


class NeedsPassTest(unittest.TestCase):
    def test_success_and_skipped_pass(self) -> None:
        self.assertEqual(needs_pass.verdict(needs(a="success", b="skipped"), set(), set()), [])

    def test_failure_fails(self) -> None:
        self.assertEqual(needs_pass.verdict(needs(a="success", b="failure"), set(), set()), ["b: failure"])

    def test_every_lane_cancelled_fails(self) -> None:
        # A run superseded before any lane ran never tested the head.
        bad = needs_pass.verdict(needs(plan="cancelled", suite="cancelled"), set(), set(), "pull_request")
        self.assertEqual(bad, ["plan: CANCELLED (superseded push)", "suite: CANCELLED (superseded push)"])

    def test_one_cancelled_lane_among_successes_fails(self) -> None:
        bad = needs_pass.verdict(needs(plan="success", suite="cancelled"), set(), set(), "merge_group")
        self.assertEqual(bad, ["suite: CANCELLED (stopped run)"])

    def test_cancelled_fails_even_when_advisory(self) -> None:
        bad = needs_pass.verdict(needs(evals="cancelled"), set(), {"evals"}, "push")
        self.assertEqual(bad, ["evals: CANCELLED (stopped run)"])

    def test_required_must_succeed(self) -> None:
        self.assertEqual(needs_pass.verdict(needs(plan="skipped"), {"plan"}, set()),
                         ["plan: skipped (must succeed)"])

    def test_required_unless_merge_group_skips_only_in_merge_group(self) -> None:
        jobs = needs(plan="success", suite="skipped")
        self.assertEqual(needs_pass.verdict(jobs, {"plan"}, set(), "merge_group", {"suite"}), [])
        self.assertEqual(needs_pass.verdict(jobs, {"plan"}, set(), "pull_request", {"suite"}),
                         ["suite: skipped (must succeed)"])


if __name__ == "__main__":
    unittest.main()
