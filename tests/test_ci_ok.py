#!/usr/bin/env python3
"""Tests for the ci-ok aggregate gate's decision."""

from __future__ import annotations

import importlib.util
import pathlib
import unittest

SPEC = importlib.util.spec_from_file_location(
    "ci_ok", pathlib.Path(__file__).resolve().parents[1] / ".github" / "actions" / "ci-ok" / "ci_ok.py")
ci_ok = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci_ok)


def run(name: str, status: str = "completed", conclusion: str | None = "success") -> dict:
    return {"name": name, "status": status, "conclusion": conclusion}


class CiOkTest(unittest.TestCase):
    def test_action_manifests_parse(self) -> None:
        import yaml  # every shared action.yml must be valid YAML for the runner
        root = pathlib.Path(__file__).resolve().parents[1] / ".github" / "actions"
        for manifest in root.glob("*/action.yml"):
            self.assertIsInstance(yaml.safe_load(manifest.read_text()), dict, manifest)

    def test_pending_until_all_complete(self) -> None:
        state, detail = ci_ok.evaluate([run("a"), run("b", "in_progress", None)], {"ci-ok"})
        self.assertEqual((state, detail), ("pending", ["b"]))

    def test_passes_with_success_skipped_neutral(self) -> None:
        runs = [run("a"), run("b", conclusion="skipped"), run("c", conclusion="neutral")]
        self.assertEqual(ci_ok.evaluate(runs, set())[0], "pass")

    def test_fails_on_failure_or_cancel(self) -> None:
        for bad in ("failure", "cancelled", "timed_out", "action_required", "startup_failure"):
            state, detail = ci_ok.evaluate([run("a"), run("b", conclusion=bad)], set())
            self.assertEqual(state, "fail", bad)
            self.assertEqual(detail, [f"b={bad}"])

    def test_ignores_itself_and_listed_checks(self) -> None:
        runs = [run("ci-ok", "in_progress", None), run("plain-language", conclusion="failure"), run("a")]
        self.assertEqual(ci_ok.evaluate(runs, {"ci-ok", "plain-language"})[0], "pass")

    def test_other_apps_do_not_gate_by_default(self) -> None:
        deploy = {"name": "sylphx/deploy", "status": "in_progress", "conclusion": None, "app": {"slug": "sylphx-ai"}}
        mine = {"name": "a", "status": "completed", "conclusion": "success", "app": {"slug": "github-actions"}}
        self.assertEqual(ci_ok.evaluate([mine, deploy], set())[0], "pass")
        self.assertEqual(ci_ok.evaluate([mine, deploy], set(), actions_only=False)[0], "pending")

    def test_zero_other_checks_fails(self) -> None:
        state, detail = ci_ok.evaluate([run("ci-ok", "in_progress", None)], {"ci-ok"})
        self.assertEqual((state, detail), ("fail", ["no other check ran on this commit"]))
        self.assertEqual(ci_ok.evaluate([], {"ci-ok"}, allow_none=True), ("pass", []))

    def test_workflow_that_failed_to_start_fails(self) -> None:
        # Cubeage/voidbite-keel#1: ci.yml failed to start (a suite concluded
        # failure with 0 check runs) while the other workflows passed.
        runs = [run("plain-language"), run("game-standard")]
        own = {"id": 1, "status": "in_progress", "conclusion": None, "latest_check_runs_count": 1}
        ok = {"id": 2, "status": "completed", "conclusion": "success", "latest_check_runs_count": 1}
        for bad in ("failure", "startup_failure", "cancelled", "action_required", "timed_out"):
            broken = {"id": 3, "status": "completed", "conclusion": bad, "latest_check_runs_count": 0}
            state, detail = ci_ok.evaluate(runs, {"ci-ok"}, suites=[own, ok, broken])
            self.assertEqual(state, "fail", bad)
            self.assertIn("workflow failed to start", detail[0])
        self.assertEqual(ci_ok.evaluate(runs, {"ci-ok"}, suites=[own, ok])[0], "pass")

    def test_queued_empty_suite_neither_blocks_nor_fails(self) -> None:
        queued = {"id": 4, "status": "queued", "conclusion": None, "latest_check_runs_count": 0}
        self.assertEqual(ci_ok.evaluate([run("a")], set(), suites=[queued])[0], "pass")

    def test_failed_suite_with_jobs_is_judged_by_its_check_runs(self) -> None:
        # A re-run that passed leaves the suite's latest check runs green.
        rerun = {"id": 5, "status": "completed", "conclusion": "failure", "latest_check_runs_count": 2}
        self.assertEqual(ci_ok.evaluate([run("a")], set(), suites=[rerun])[0], "pass")

    def test_required_checks_must_succeed(self) -> None:
        runs = [run("a"), run("b", conclusion="skipped")]
        self.assertEqual(ci_ok.evaluate(runs, set(), required={"a"})[0], "pass")
        state, detail = ci_ok.evaluate(runs, set(), required={"a", "b", "c"})
        self.assertEqual((state, detail), ("fail", ["b=skipped (required)", "c=missing (required)"]))


if __name__ == "__main__":
    unittest.main()
