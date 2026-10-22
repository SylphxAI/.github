#!/usr/bin/env python3
"""The red-main classifier: a real code fault in a failing step is NEVER infrastructure.

The classifier reads a run's failing jobs and answers `infra <signature>` or
`none`. It once answered `infra` for keel main's `Web release and smoke`
failure, whose failing step died of an `AssertionError`, because the SAME job
also logged ENOSPC noise; the handler then emitted `verdict=infra`, skipped its
confirmation, trace and revert, and exited 0 while the trunk stayed red for
three days. The rule is now per failing step's TERMINAL ERROR, never job-level
co-occurrence.

The programs run here are the checked-in ones (.github/red_main/infra.py); the
inputs are the recorded shape of the keel verify runs and derived fixtures, so
no network is used.
"""
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
INFRA = ROOT / ".github" / "red_main" / "infra.py"
FIXTURES = ROOT / "tests/fixtures/red-main"


def classify(jobs):
    done = subprocess.run([sys.executable, str(INFRA)], input=json.dumps({"jobs": jobs}),
                          capture_output=True, text=True, check=True)
    return done.stdout.strip()


def job(name, conclusion, steps, annotations=(), jid=1):
    return {"id": jid, "name": name, "conclusion": conclusion,
            "steps": [{"name": n, "conclusion": c} for n, c in steps],
            "annotations": [str(a) for a in annotations]}


def failed(name, step_name, notes=(), jid=1):
    return job(name, "failure", [("Set up job", "success"), (step_name, "failure")], notes, jid)


class RealCaseTest(unittest.TestCase):
    """The recorded case: keel main 2026-10-12..14, run 38198243221 and siblings."""

    def test_assertion_plus_enospc_noise_in_one_job_is_code(self):
        # REGRESSION: the failing step's terminal error is an AssertionError; the
        # ENOSPC line is the same job's unrelated noise. Job-level co-occurrence
        # once hid the assertion behind it, so nothing was reverted or alerted.
        jobs = [
            failed("Web release and smoke", "Mobile web smoke",
                   ["No space left on device (ENOSPC)",
                    "AssertionError: expected the mobile web smoke to pass",
                    "Process completed with exit code 1."]),
            failed("verified", "Run # The native app builds skip on their own",
                   ["Process completed with exit code 1."], jid=2),
        ]
        self.assertEqual(classify(jobs), "none")

    def test_the_recorded_jobs_fixture_is_not_infrastructure(self):
        # The keel verify jobs as recorded: a Web release and smoke failure and a
        # verified aggregate failure, no step named for infrastructure.
        body = json.loads((FIXTURES / "keel-verify-jobs-37250728778.json").read_text())
        jobs = [{"id": i, "name": j["name"], "conclusion": j["conclusion"],
                 "steps": j.get("steps", []), "annotations": []}
                for i, j in enumerate(body["jobs"])]
        self.assertEqual(classify(jobs), "none")


class CodeFaultTest(unittest.TestCase):
    def test_a_test_assertion_is_code_whatever_else_the_job_logged(self):
        for noise in ([], ["No space left on device"], ["Cache restore failed: 403 Forbidden"],
                      ["The self-hosted runner: sylphx-1 lost communication with the server."]):
            notes = ["AssertionError: nope", "Process completed with exit code 1.", *noise]
            self.assertEqual(classify([failed("Test", "Run tests", notes)]), "none", noise)

    def test_a_rust_panic_and_a_pytest_failure_are_code(self):
        self.assertEqual(classify([failed("Test", "Run cargo test",
                                          ["thread 'main' panicked at src/lib.rs:3:1",
                                           "Process completed with exit code 101."])]), "none")
        self.assertEqual(classify([failed("Test", "Run pytest",
                                          ["FAILED tests/test_x.py::test_y - AssertionError",
                                           "Process completed with exit code 1."])]), "none")

    def test_a_code_fault_in_one_job_makes_the_whole_run_code(self):
        jobs = [
            failed("Release build and smoke", "Run actions/upload-artifact@v4",
                   ["Failed to CreateArtifact: an error occurred while uploading the artifact",
                    "Process completed with exit code 1."]),
            failed("Web release and smoke", "Mobile web smoke",
                   ["AssertionError: nope", "Process completed with exit code 1."], jid=2),
        ]
        self.assertEqual(classify(jobs), "none")


class InfrastructureTest(unittest.TestCase):
    """The infrastructure signatures still classify as infrastructure."""

    def test_an_upload_failure_a_cache_403_and_a_lost_runner_are_infra(self):
        self.assertTrue(classify([failed("Release", "Run actions/upload-artifact@v4",
                                         ["Failed to upload the artifact",
                                          "Process completed with exit code 1."])]).startswith("infra "))
        self.assertTrue(classify([failed("Test", "Restore cache",
                                         ["Failed to restore: 403 Forbidden",
                                          "Process completed with exit code 1."])]).startswith("infra "))
        self.assertTrue(classify([job("Test", "failure", [("Set up job", "success"), ("Run tests", "success")],
                                      ["The hosted runner lost communication with the server."])]).startswith("infra "))

    def test_set_up_job_and_checkout_failures_are_infra(self):
        self.assertTrue(classify([job("Test", "failure", [("Set up job", "failure")],
                                      ["The runner has received a shutdown signal."])]).startswith("infra "))
        self.assertTrue(classify([job("Test", "failure", [("Run actions/checkout@v5", "failure")],
                                      ["fatal: could not resolve host: github.com"])]).startswith("infra "))

    def test_an_infra_named_step_whose_error_is_a_code_fault_is_code(self):
        self.assertEqual(classify([failed("Release", "Run actions/upload-artifact@v4",
                                          ["AssertionError: the pack is missing",
                                           "Process completed with exit code 1."])]), "none")


if __name__ == "__main__":
    unittest.main()
