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
            classify(job("a", [], ["The self-hosted runner lost communication with the server."])),
            "infra runner-lost",
        )

    def test_test_failure_is_not_infra(self):
        self.assertEqual(classify(job("a", ["cargo nextest"])), "none")

    def test_mixed_jobs_are_not_infra(self):
        self.assertEqual(classify(job("a", ["Upload artifact"]), job("b", ["cargo nextest"])), "none")

    def test_test_step_beside_infra_step_is_not_infra(self):
        self.assertEqual(classify(job("a", ["cargo nextest", "Upload artifact"])), "none")

    def test_no_failed_jobs(self):
        self.assertEqual(classify(), "none")


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
