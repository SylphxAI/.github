#!/usr/bin/env python3
"""Tests for the main-red-gate decision: a red trunk admits only the revert or the fix."""

from __future__ import annotations

import importlib.util
import pathlib
import unittest

SPEC = importlib.util.spec_from_file_location(
    "main_red_gate",
    pathlib.Path(__file__).resolve().parents[1] / ".github" / "actions" / "main-red-gate" / "main_red_gate.py")
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def run(conclusion: str, status: str = "completed", event: str = "push", branch: str = "main") -> dict:
    return {"id": 1, "event": event, "head_branch": branch, "status": status, "conclusion": conclusion,
            "html_url": "u", "head_sha": "a" * 40}


def pull(title: str = "feat: x", head: str = "feat/x", labels: tuple[str, ...] = ()) -> dict:
    return {"title": title, "head": {"ref": head}, "labels": [{"name": n} for n in labels]}


class TrunkStateTest(unittest.TestCase):
    def test_newest_conclusive_run_decides(self) -> None:
        self.assertEqual(gate.trunk_state([run("failure"), run("success")], "main")[0], "red")
        self.assertEqual(gate.trunk_state([run("success"), run("failure")], "main")[0], "green")

    def test_cancelled_and_skipped_are_passed_over(self) -> None:
        runs = [run("cancelled"), run("skipped"), run("failure"), run("success")]
        self.assertEqual(gate.trunk_state(runs, "main")[0], "red")
        self.assertEqual(gate.trunk_state([run("cancelled"), run("success")], "main")[0], "green")

    def test_failure_kinds_are_red(self) -> None:
        for kind in ("failure", "timed_out", "action_required", "startup_failure"):
            self.assertEqual(gate.trunk_state([run(kind)], "main")[0], "red", kind)

    def test_other_branches_events_and_unfinished_runs_do_not_count(self) -> None:
        runs = [run("failure", branch="sylphx-verify/abc"), run("failure", event="workflow_dispatch"),
                run("failure", status="in_progress")]
        self.assertEqual(gate.trunk_state(runs, "main"), ("unknown", None))

    def test_no_runs_is_unknown(self) -> None:
        self.assertEqual(gate.trunk_state([], "main"), ("unknown", None))


class MergeGroupRefTest(unittest.TestCase):
    def test_pull_request_number(self) -> None:
        self.assertEqual(gate.pr_number("gh-readonly-queue/main/pr-4321-" + "0" * 40), 4321)
        self.assertEqual(gate.pr_number("refs/heads/gh-readonly-queue/release/x/pr-7-abcdef1"), 7)
        self.assertIsNone(gate.pr_number("refs/heads/main"))
        self.assertIsNone(gate.pr_number(""))


class DecideTest(unittest.TestCase):
    def test_green_and_unknown_admit_anything(self) -> None:
        for state in ("green", "unknown"):
            self.assertTrue(gate.decide(state, pull(), "enforce", "main-red-fix")[0])

    def test_red_refuses_an_ordinary_change(self) -> None:
        admit, message = gate.decide("red", pull(), "enforce", "main-red-fix")
        self.assertFalse(admit)
        self.assertIn("main-red-fix", message)

    def test_red_admits_reverts(self) -> None:
        for p in (pull(head="auto-revert/abcdef123"), pull(title="Revert \"feat: x\""),
                  pull(labels=("auto-revert",)), pull(labels=("queue-jump:red-main",))):
            self.assertTrue(gate.decide("red", p, "enforce", "main-red-fix")[0], p)

    def test_red_admits_the_labelled_fix_only(self) -> None:
        self.assertTrue(gate.decide("red", pull(labels=("main-red-fix",)), "enforce", "main-red-fix")[0])
        self.assertFalse(gate.decide("red", pull(labels=("main-red-fix",)), "enforce", "other")[0])
        self.assertFalse(gate.decide("red", pull(title="revisit docs"), "enforce", "main-red-fix")[0])

    def test_unreadable_pull_request_admits_with_a_warning(self) -> None:
        admit, message = gate.decide("red", None, "enforce", "main-red-fix")
        self.assertTrue(admit)
        self.assertIn("::warning::", message)

    def test_observe_mode_never_refuses(self) -> None:
        admit, message = gate.decide("red", pull(), "observe", "main-red-fix")
        self.assertTrue(admit)
        self.assertIn("would have refused", message)


if __name__ == "__main__":
    unittest.main()
