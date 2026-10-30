#!/usr/bin/env python3
"""The required gate consumes completed dependency results, never an inventory."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github/actions/ci-ok/ci_ok.py"
SPEC = importlib.util.spec_from_file_location("ci_ok", SCRIPT)
ci_ok = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci_ok)


def results(**jobs: str) -> dict:
    return {job: {"result": result} for job, result in jobs.items()}


class CiOkTest(unittest.TestCase):
    def test_all_required_jobs_succeeded(self) -> None:
        self.assertEqual(ci_ok.evaluate(results(build="success", tests="success"), {"build", "tests"}), [])

    def test_missing_required_job_fails_even_with_another_green_job(self) -> None:
        self.assertEqual(ci_ok.evaluate(results(build="success"), {"build", "tests"}),
                         ["tests: required but not in needs"])

    def test_every_non_success_required_result_fails(self) -> None:
        for result in ("failure", "skipped", "cancelled", "queued", "in_progress", "neutral", "timed_out", ""):
            with self.subTest(result=result):
                self.assertTrue(ci_ok.evaluate(results(build="success", tests=result), {"build", "tests"}))

    def test_no_result_fails(self) -> None:
        self.assertTrue(ci_ok.evaluate({"tests": {}}, {"tests"}))
        self.assertTrue(ci_ok.evaluate({"tests": {"result": None}}, {"tests"}))

    def test_absent_empty_or_malformed_dependency_objects_fail(self) -> None:
        for needs in (None, {}, [], "success", {"tests": None}, {"tests": "success"}, {"": {"result": "success"}}, {"tests": {"result": []}}):
            with self.subTest(needs=needs):
                self.assertTrue(ci_ok.evaluate(needs, {"tests"}))

    def test_expected_required_job_ids_are_mandatory(self) -> None:
        self.assertTrue(ci_ok.evaluate(results(build="success"), set()))

    def test_optional_path_lane_may_skip_but_may_not_fail_or_cancel(self) -> None:
        self.assertEqual(ci_ok.evaluate(results(plan="success", optional="skipped"), {"plan"}), [])
        for result in ("failure", "cancelled", "in_progress", ""):
            self.assertTrue(ci_ok.evaluate(results(plan="success", optional=result), {"plan"}))

    def test_cli_fixtures(self) -> None:
        fixtures = [
            ("all-green", results(build="success", tests="success"), 0),
            ("absent", results(build="success"), 1),
            ("empty", {}, 1),
            ("late-failure", results(build="success", tests="failure"), 1),
            ("skipped-required", results(build="success", tests="skipped"), 1),
            ("cancelled", results(build="success", tests="cancelled"), 1),
            ("still-running", results(build="success", tests="in_progress"), 1),
        ]
        for name, needs, expected in fixtures:
            with self.subTest(name=name):
                result = subprocess.run([sys.executable, str(SCRIPT)],
                                        env={**os.environ, "NEEDS": json.dumps(needs), "REQUIRED": "build,tests"},
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                self.assertIn("::error::" if expected else "all required jobs succeeded:", result.stdout)

    def test_missing_or_invalid_json_fails(self) -> None:
        for value in ("", "not json", "null", "[]"):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"NEEDS": value, "REQUIRED": "tests"}), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(ci_ok.main(), 1)

    def test_action_manifests_parse(self) -> None:
        import yaml
        for manifest in (ROOT / ".github/actions").glob("*/action.yml"):
            self.assertIsInstance(yaml.safe_load(manifest.read_text()), dict, manifest)


if __name__ == "__main__":
    unittest.main()
