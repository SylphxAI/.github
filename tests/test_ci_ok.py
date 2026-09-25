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


if __name__ == "__main__":
    unittest.main()
