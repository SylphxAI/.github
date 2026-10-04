#!/usr/bin/env python3
"""The red-main confirmation step reaches a verdict on recorded keel runs.

tests/fixtures/red-main holds the jobs of two consecutive failed verify runs on
SylphxAI/keel main, recorded while every handler run stopped at "previous-run
failed lanes unavailable": the job "desktop / Desktop native adapters
(windows, cross-compiled)" passed, but its comma made the lane reader reject
the whole run. The scripts run here are the ones the workflow ships; the
provider is a local fixture, so no network is used.
"""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/red-main"
ENV = yaml.safe_load((ROOT / ".github/workflows/red-main.yml").read_text())["jobs"]["red-main"]["env"]
REPO = "SylphxAI/keel"
CURRENT, PREVIOUS = "37172490395", "37172043559"

spec = importlib.util.spec_from_file_location("post_main", ROOT / ".github/actions/ci-range/post_main.py")
proof = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proof)


def jobs(run):
    return json.loads((FIXTURES / f"keel-verify-jobs-{run}.json").read_text())["jobs"]


def lanes_of(rows, run):
    with patch.object(proof, "paged", return_value=rows):
        return proof.failed_lanes(REPO, run)


class ConfirmTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for name in ("JUNIT_PY", "CONFIRM_PY", "PREV_PY"):
            (self.root / f"{name[:-3]}.py").write_text(ENV[name])

    def script(self, name, *args, stdin=None):
        done = subprocess.run([sys.executable, str(self.root / f"{name}.py"), *args], input=stdin,
                              capture_output=True, text=True, check=True)
        return done.stdout.strip()

    def verdict(self, prev_conclusion, prev_lanes, cur_lanes):
        """The same chain the confirm step runs: lanes -> junit.py -> confirm.py."""
        empty = self.root / "junit"
        empty.mkdir(exist_ok=True)
        prev, cur = self.root / "previous.tsv", self.root / "current.tsv"
        self.script("JUNIT", str(empty), str(prev), prev_lanes)
        self.script("JUNIT", str(empty), str(cur), cur_lanes)
        return self.script("CONFIRM", prev_conclusion, str(prev), str(cur))

    def test_the_recorded_previous_run_no_longer_aborts_the_step(self):
        # Before the fix this raised "previous-run lane evidence unavailable".
        names = [job["name"] for job in jobs(PREVIOUS)]
        self.assertIn("desktop / Desktop native adapters (windows, cross-compiled)", names)
        self.assertEqual(lanes_of(jobs(PREVIOUS), PREVIOUS), "Features (gpu),Web release and smoke,verified")

    def test_two_consecutive_failures_of_the_same_lane_confirm_and_reach_the_revert_trace(self):
        previous = lanes_of(jobs(PREVIOUS), PREVIOUS)
        current = lanes_of(jobs(CURRENT), CURRENT)
        answer = self.verdict("failure", previous, current)
        self.assertTrue(answer.startswith("yes "), answer)
        # The workflow starts the culprit trace only on confirmed=yes.
        trace = next(s for s in yaml.safe_load((ROOT / ".github/workflows/red-main.yml").read_text())
                     ["jobs"]["red-main"]["steps"] if s.get("id") == "trace")
        self.assertIn("steps.confirm.outputs.confirmed == 'yes'", trace["if"])

    def test_a_one_off_failure_does_not_confirm(self):
        previous = copy.deepcopy(jobs(PREVIOUS))
        for job in previous:
            if job["name"] in ("Features (gpu)", "Web release and smoke", "verified"):
                job["conclusion"] = "success"
        previous[0]["conclusion"] = "failure"  # a different lane failed last time
        previous_lanes = lanes_of(previous, PREVIOUS)
        self.assertNotIn("Features (gpu)", previous_lanes)
        current = lanes_of(jobs(CURRENT), CURRENT)
        self.assertTrue(self.verdict("failure", previous_lanes, current).startswith("no "))
        # The previous run passed or is unknown: nothing to confirm against.
        for conclusion in ("success", "cancelled", ""):
            self.assertTrue(self.verdict(conclusion, "", current).startswith("no "), conclusion)

    def test_a_failing_lane_with_a_comma_matches_across_runs(self):
        rows = copy.deepcopy(jobs(PREVIOUS))
        for job in rows:
            if "cross-compiled" in job["name"]:
                job["conclusion"] = "failure"
        lane = "desktop / Desktop native adapters (windows; cross-compiled)"
        previous = lanes_of(rows, PREVIOUS)
        self.assertIn(lane, previous.split(","))
        # The handler lists the current run's lanes with jq; its gsub matches.
        self.assertTrue(self.verdict("failure", previous, previous).startswith("yes "))

    def test_unusable_lane_evidence_still_fails_closed(self):
        broken = copy.deepcopy(jobs(PREVIOUS))
        broken[0]["status"] = "in_progress"
        with self.assertRaises(ValueError):
            lanes_of(broken, PREVIOUS)
        broken = copy.deepcopy(jobs(PREVIOUS))
        broken[0]["run_id"] = 1
        with self.assertRaises(ValueError):
            lanes_of(broken, PREVIOUS)

    def history(self, *rows):
        body = {"total_count": len(rows), "workflow_runs": [
            dict(id=i, status=status, conclusion=conclusion, created_at=at, html_url=f"u/{i}")
            for i, status, conclusion, at in rows]}
        return self.script("PREV", "2026-10-04T02:54:48Z", stdin=json.dumps(body))

    def test_the_previous_run_skips_cancelled_and_unfinished_runs(self):
        out = self.history(
            (3, "completed", "cancelled", "2026-10-04T02:50:00Z"),
            (4, "in_progress", None, "2026-10-04T02:49:00Z"),
            (5, "completed", "skipped", "2026-10-04T02:48:00Z"),
            (37172043559, "completed", "failure", "2026-10-04T02:45:47Z"),
            (6, "completed", "success", "2026-10-04T02:30:00Z"))
        self.assertEqual(out.split("\t")[:2], ["37172043559", "failure"])

    def test_no_previous_completed_run_confirms_nothing(self):
        self.assertEqual(self.history((3, "completed", "cancelled", "2026-10-04T02:50:00Z")), "")
        self.assertEqual(self.history(), "")


if __name__ == "__main__":
    unittest.main()
