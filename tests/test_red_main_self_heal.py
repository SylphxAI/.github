#!/usr/bin/env python3
"""Tests for the red-main self-heal rules (INFRA_PY and CONFIRM_PY in red-main.yml)."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/red-main.yml"


def embedded(name: str) -> str:
    lines = WORKFLOW.read_text().splitlines()
    start = lines.index(f"      {name}: |") + 1
    body = []
    for line in lines[start:]:
        if line.strip() and not line.startswith("        "):
            break
        body.append(line[8:])
    return "\n".join(body) + "\n"


INFRA = embedded("INFRA_PY")
CONFIRM = embedded("CONFIRM_PY")
VERDICT = embedded("VERDICT_PY")
PREV = embedded("PREV_PY")


def job(name, failed_steps=(), annotations=(), conclusion="failure"):
    steps = [{"name": "Checkout", "conclusion": "success"}]
    steps += [{"name": s, "conclusion": "failure"} for s in failed_steps]
    return {"name": name, "conclusion": conclusion, "steps": steps, "annotations": list(annotations)}


def classify(*jobs) -> str:
    return subprocess.run(
        [sys.executable, "-c", INFRA],
        input=json.dumps({"jobs": list(jobs)}),
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def confirm(prev_conclusion: str, prev_rows: str, cur_rows: str) -> str:
    with tempfile.TemporaryDirectory() as d:
        prev, cur = Path(d, "prev.tsv"), Path(d, "cur.tsv")
        prev.write_text(prev_rows)
        cur.write_text(cur_rows)
        return subprocess.run(
            [sys.executable, "-c", CONFIRM, prev_conclusion, str(prev), str(cur)],
            capture_output=True, text=True, check=True,
        ).stdout.strip()


class InfraTest(unittest.TestCase):
    def test_token_mint(self):
        self.assertEqual(classify(job("a", ["Mint installation token"])), "infra installation-token-mint")

    def test_upload_artifact(self):
        self.assertEqual(classify(job("a", ["Upload artifact"])), "infra upload-artifact")

    def test_cache_needs_403(self):
        self.assertEqual(classify(job("a", ["Restore cache"])), "none")
        self.assertEqual(
            classify(job("a", ["Restore cache"], ["Failed to restore: 403 Forbidden"])), "infra cache-403"
        )

    def test_runner_lost(self):
        self.assertEqual(
            classify(job("a", [], ["The hosted runner lost communication with the server. Verify the machine is running."])),
            "infra runner-lost",
        )
        self.assertEqual(
            classify(job("a", [], ["The self-hosted runner: sylphx-1 lost communication with the server. Verify."])),
            "infra runner-lost",
        )
        self.assertEqual(classify(job("a", [], ["The runner has received a shutdown signal."])), "infra runner-lost")

    def test_runner_lost_like_text_is_not_enough(self):
        self.assertEqual(classify(job("a", [], ["runner foo went offline"])), "none")
        self.assertEqual(classify(job("a", [], ["the test lost communication with the server"])), "none")

    def test_failed_test_step_plus_runner_lost_text_is_not_infra(self):
        for note in (
            "The runner has received a shutdown signal.",
            "The hosted runner lost communication with the server.",
            "runner x went offline",
        ):
            self.assertEqual(classify(job("a", ["cargo nextest"], [note])), "none")

    def test_test_failure_is_not_infra(self):
        self.assertEqual(classify(job("a", ["cargo nextest"])), "none")

    def test_mixed_jobs_are_not_infra(self):
        self.assertEqual(classify(job("a", ["Upload artifact"]), job("b", ["cargo nextest"])), "none")

    def test_test_step_beside_infra_step_is_not_infra(self):
        self.assertEqual(classify(job("a", ["cargo nextest", "Upload artifact"])), "none")

    def test_no_failed_jobs(self):
        self.assertEqual(classify(), "none")


def verdict(infra, state, conclusion="") -> str:
    return subprocess.run(
        [sys.executable, "-c", VERDICT, infra, state, conclusion], capture_output=True, text=True, check=True
    ).stdout.strip()


def previous(runs, created="2026-09-30T10:00:00Z") -> str:
    return subprocess.run(
        [sys.executable, "-c", PREV, created],
        input=json.dumps({"workflow_runs": runs}), capture_output=True, text=True, check=True,
    ).stdout.strip()


def run(id, conclusion, created, status="completed"):
    return {"id": id, "conclusion": conclusion, "created_at": created, "status": status, "html_url": f"u{id}"}


class VerdictTest(unittest.TestCase):
    def test_infra_rerun_passes_means_no_flake_and_no_revert(self):
        self.assertEqual(verdict("yes", "completed", "success"), "infra")

    def test_infra_rerun_fails_means_stop(self):
        self.assertEqual(verdict("yes", "completed", "failure"), "unknown")

    def test_infra_rerun_refused_or_timeout_means_stop(self):
        self.assertEqual(verdict("yes", "refused"), "unknown")
        self.assertEqual(verdict("yes", "timeout"), "unknown")

    def test_code_failure_paths_unchanged(self):
        self.assertEqual(verdict("no", "completed", "success"), "flake")
        self.assertEqual(verdict("no", "completed", "failure"), "real")
        self.assertEqual(verdict("no", "refused"), "real")
        self.assertEqual(verdict("no", "timeout"), "unknown")


class PreviousRunTest(unittest.TestCase):
    def test_cancelled_and_skipped_are_ignored(self):
        runs = [
            run(3, "cancelled", "2026-09-30T09:50:00Z"),
            run(2, "skipped", "2026-09-30T09:40:00Z"),
            run(1, "failure", "2026-09-30T09:00:00Z"),
        ]
        self.assertEqual(previous(runs), "1\tfailure\tu1")

    def test_newest_completed_before_this_run(self):
        runs = [
            run(4, "failure", "2026-09-30T10:30:00Z"),
            run(2, "success", "2026-09-30T09:40:00Z"),
            run(1, "failure", "2026-09-30T09:00:00Z"),
        ]
        self.assertEqual(previous(runs), "2\tsuccess\tu2")

    def test_unfinished_run_is_ignored(self):
        self.assertEqual(previous([run(2, None, "2026-09-30T09:40:00Z", "in_progress")]), "")

    def test_none_found(self):
        self.assertEqual(previous([]), "")


class ConfirmTest(unittest.TestCase):
    def test_previous_success(self):
        self.assertTrue(confirm("success", "", "test\tt1\tj\n").startswith("no "))

    def test_no_previous_run(self):
        self.assertTrue(confirm("", "", "test\tt1\tj\n").startswith("no "))

    def test_same_test_same_job(self):
        self.assertTrue(confirm("failure", "test\tt1\tj\n", "test\tt1\tj\n").startswith("yes "))

    def test_different_test(self):
        self.assertTrue(confirm("failure", "test\tt1\tj\n", "test\tt2\tj\n").startswith("no "))

    def test_same_test_different_job(self):
        self.assertTrue(confirm("failure", "test\tt1\tj1\n", "test\tt1\tj2\n").startswith("no "))

    def test_lane_row_matches_same_job(self):
        self.assertTrue(confirm("failure", "lane\tj\tj\n", "test\tt1\tj\n").startswith("yes "))

    def test_lane_rows_different_job(self):
        self.assertTrue(confirm("failure", "lane\tj1\tj1\n", "lane\tj2\tj2\n").startswith("no "))


if __name__ == "__main__":
    unittest.main()
