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
        # An unfinished job is ignored only once the run itself reads back as
        # completed; while the run is active the evidence is incomplete.
        with patch.object(proof, "api", return_value={"id": int(PREVIOUS), "status": "in_progress"}):
            with self.assertRaises(ValueError):
                lanes_of(broken, PREVIOUS)
        with patch.object(proof, "api", return_value={"id": int(PREVIOUS), "status": "completed"}):
            self.assertEqual(lanes_of(broken, PREVIOUS), lanes_of(jobs(PREVIOUS), PREVIOUS))
        broken = copy.deepcopy(jobs(PREVIOUS))
        broken[0]["status"] = "mystery"
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



# Two consecutive failed verify runs on SylphxAI/keel main, recorded with their
# steps and their check runs' annotations. Both failed the job "Web release and
# smoke" for different causes: at 145c958a (run 37242311505) the step "Serve
# the web pack" (a scene() check), at 541d5a72 (run 37250728778, the commit
# that reverted that check) the step "Mobile web smoke" (an iPhone rotate
# expectation). Both steps print only the runner's "Process completed with
# exit code 1." as their error line.
EARLIER, LATER = "37242311505", "37250728778"
WEB = "Web release and smoke"
ANNOTATIONS = json.loads((FIXTURES / "keel-check-annotations.json").read_text())


def annotations(path):
    check = path.split("/check-runs/")[1].split("/")[0]
    page = int(path.rsplit("page=", 1)[1])
    return ANNOTATIONS[check] if page == 1 else []


def units_of(run, rows=None):
    with patch.object(proof, "paged", return_value=jobs(run) if rows is None else rows), \
            patch.object(proof, "api", side_effect=annotations):
        return proof.failed_units(REPO, run)


class SameJobDifferentCauseTest(unittest.TestCase):
    setUp = ConfirmTest.setUp
    script = ConfirmTest.script

    def chain(self, previous, current):
        """The confirm step's chain on two runs: failed-units -> junit.py -> confirm.py."""
        empty = self.root / "junit"
        empty.mkdir(exist_ok=True)
        files = []
        for name, units in (("previous", previous), ("current", current)):
            signatures = self.root / f"{name}-signatures.tsv"
            signatures.write_text(units + "\n")
            lanes = ",".join(line.split("\t")[0] for line in units.splitlines())
            failing = self.root / f"{name}.tsv"
            self.script("JUNIT", str(empty), str(failing), lanes, str(signatures))
            files.append(str(failing))
        confirmed = self.root / "confirmed.tsv"
        answer = self.script("CONFIRM", "failure", *files, str(confirmed))
        return answer, [tuple(line.split("\t")) for line in confirmed.read_text().splitlines()]

    def test_the_current_reader_reads_the_previous_run_the_pinned_one_refused(self):
        # The handler keel pinned (red-main.yml@9c46562) refused any job name
        # with a comma; this run has one in a job that passed.
        self.assertIn("desktop / Speech-to-text bench (macOS arm64, build only)",
                      [job["name"] for job in jobs(EARLIER)])
        self.assertEqual(lanes_of(jobs(EARLIER), EARLIER),
                         f"desktop / Desktop native adapters (windows cross-compiled),{WEB},verified")

    def test_each_failed_job_is_named_by_its_failed_step_and_error_line(self):
        earlier = dict(line.split("\t") for line in units_of(EARLIER).splitlines())
        later = dict(line.split("\t") for line in units_of(LATER).splitlines())
        self.assertEqual(earlier[WEB], "failure at Serve the web pack: Process completed with exit code 1.")
        self.assertEqual(later[WEB], "failure at Mobile web smoke: Process completed with exit code 1.")
        self.assertEqual(
            earlier["desktop / Desktop native adapters (windows cross-compiled)"],
            "failure at Link the speech-to-text bench for Windows and list its DLLs: "
            "Process completed with exit code 101.")
        # Warnings (the Node.js deprecation, a cache fallback) are not errors.
        self.assertNotIn("Node.js", "".join(earlier.values()) + "".join(later.values()))

    def test_the_same_job_failing_for_a_different_cause_confirms_nothing(self):
        answer, confirmed = self.chain(units_of(EARLIER), units_of(LATER))
        self.assertTrue(answer.startswith(f"no {WEB} failed on both runs, but not the same way"), answer)
        # Nothing confirmed: the trace (and so any revert) never starts, and
        # the aggregate job that failed on both runs does not count.
        self.assertEqual(confirmed, [])
        trace = next(s for s in yaml.safe_load((ROOT / ".github/workflows/red-main.yml").read_text())
                     ["jobs"]["red-main"]["steps"] if s.get("id") == "trace")
        self.assertIn("steps.confirm.outputs.confirmed == 'yes'", trace["if"])

    def test_the_same_step_and_error_on_consecutive_runs_confirms_that_unit(self):
        # The next run on main fails the same step with the same error line.
        rows = copy.deepcopy(jobs(LATER))
        for job in rows:
            job["run_id"] = int(LATER) + 1
        answer, confirmed = self.chain(units_of(LATER), units_of(str(int(LATER) + 1), rows))
        self.assertTrue(answer.startswith(f"yes the same job failed the same way on both runs: {WEB}"), answer)
        self.assertEqual(confirmed, [("lane", "failure at Mobile web smoke: Process completed with exit code 1.", WEB)])

    def test_unreadable_failure_detail_fails_closed(self):
        broken = copy.deepcopy(jobs(LATER))
        next(job for job in broken if job["name"] == WEB)["check_run_url"] = "https://evil.invalid/1"
        with self.assertRaises(ValueError):
            units_of(LATER, broken)
        broken = copy.deepcopy(jobs(LATER))
        del next(job for job in broken if job["name"] == WEB)["steps"]
        with self.assertRaises(ValueError):
            units_of(LATER, broken)
        with patch.object(proof, "paged", return_value=jobs(LATER)), \
                patch.object(proof, "api", return_value={"message": "Not Found"}):
            with self.assertRaises(ValueError):
                proof.failed_units(REPO, LATER)


if __name__ == "__main__":
    unittest.main()
