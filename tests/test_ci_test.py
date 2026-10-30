"""Observable guard behavior: fresh reports, actual execution, exact exceptions."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / ".github/actions/setup-ci-test"
SCRIPT = ACTION / "ci_test.py"


def libtest(passed=0, failed=0, ignored=0, filtered=0):
    return (
        f"running {passed + failed + ignored} tests\n"
        f"test result: {'FAILED' if failed else 'ok'}. {passed} passed; {failed} failed; "
        f"{ignored} ignored; 0 measured; {filtered} filtered out; finished in 0.01s\n"
    )


def vitest(*statuses, success=True, suite_status="passed"):
    return json.dumps({
        "success": success,
        "numTotalTests": 999,
        "testResults": [{"status": suite_status, "assertionResults": [{"status": s} for s in statuses]}],
    })


class CiTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ci-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.report = self.root / "report with spaces"
        self.exceptions = self.root / "exceptions.json"

    def run_guard(self, format="libtest", text=None, status=0, exception=None, command=None):
        if command is None:
            code = "import pathlib, sys\n"
            if text is not None:
                if format == "libtest":
                    code += f"print({text!r}, end='')\n"
                else:
                    code += f"pathlib.Path({str(self.report)!r}).write_text({text!r})\n"
            code += f"sys.exit({status})\n"
            command = [sys.executable, "-c", code]
        args = [sys.executable, str(SCRIPT), "--format", format, "--shard", "unit/1"]
        if format != "libtest":
            args += ["--report", str(self.report)]
        if exception is not None:
            entry = {"format": format, "shard": "unit/1", "argv": command, "reason": "No cases assigned to this shard"}
            entry.update(exception)
            self.exceptions.write_text(json.dumps([entry]))
            args += ["--exceptions", str(self.exceptions)]
        return subprocess.run(args + ["--"] + command, capture_output=True, text=True, cwd=self.root)

    def assert_guard(self, expected, **kwargs):
        result = self.run_guard(**kwargs)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def test_libtest_counts_execution_and_aggregates_empty_targets(self):
        result = self.assert_guard(0, text=libtest(ignored=2) + libtest(passed=1, filtered=5) + libtest())
        self.assertIn("1 executed tests", result.stdout)

    def test_ignored_filtered_and_measured_are_not_execution(self):
        for text in (libtest(), libtest(ignored=5), libtest(filtered=4),
                     "running 1 test\ntest result: ok. 0 passed; 0 failed; 0 ignored; 1 measured; 0 filtered out\n"):
            with self.subTest(text=text):
                self.assert_guard(1, text=text)

    def test_incomplete_and_misleading_libtest_fail_even_with_exception(self):
        for text in (None, "", "1 passed", "test result: ok. 1 passed\n", "running 1 test\n",
                     libtest(passed=1) + "running 2 tests\n", "prefix " + libtest(passed=1),
                     libtest(passed=1).replace("running 1", "running 2"),
                     libtest(passed=1).split("\n", 1)[1],
                     libtest(passed=1).replace("0 failed", "1 failed")):
            with self.subTest(text=text):
                self.assert_guard(1, text=text, exception={})

    def test_ansi_color_is_supported(self):
        self.assert_guard(0, text=libtest(passed=1).replace("ok.", "\x1b[32mok\x1b[0m."))

    def test_runner_failure_is_preserved_regardless_of_evidence_or_exception(self):
        for format, text in (("libtest", libtest()), ("junit", "<testsuite/>"),
                             ("vitest", vitest()), ("junit", "malformed")):
            for status in (1, 2, 5, 23):
                with self.subTest(format=format, status=status):
                    self.assert_guard(status, format=format, text=text, status=status, exception={})

    def test_signal_status_uses_shell_convention(self):
        self.assert_guard(143, command=[sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"])

    def test_zero_exit_cannot_hide_failures(self):
        for format, text in (
            ("libtest", libtest(passed=1, failed=1)),
            ("junit", '<testsuite><testcase name="x"><failure/></testcase></testsuite>'),
            ("junit", '<testsuites><testsuite><testcase name="x"/></testsuite><testsuite><error/></testsuite></testsuites>'),
            ("vitest", vitest("passed", "failed")),
            ("vitest", vitest("passed", suite_status="failed")),
            ("vitest", vitest("passed", success=False)),
        ):
            with self.subTest(format=format, text=text):
                self.assert_guard(1, format=format, text=text, exception={})

    def test_junit_counts_nodes_not_summary_attributes_and_supports_nested_suites(self):
        result = self.assert_guard(0, format="junit", text='''<testsuites xmlns="urn:junit" tests="2" skipped="1">
          <testsuite><testsuite><testcase name="pass"/><testcase name="skip"><skipped/></testcase>
          </testsuite></testsuite></testsuites>''')
        self.assertIn("1 executed tests", result.stdout)
        for text in ('<testsuite tests="999"/>', '<testsuite><testcase name="skip"><skipped/></testcase></testsuite>'):
            self.assert_guard(1, format="junit", text=text)

    def test_vitest_counts_only_assertion_outcomes(self):
        result = self.assert_guard(0, format="vitest", text=vitest("passed", "skipped", "pending", "todo", "disabled"))
        self.assertIn("1 executed tests", result.stdout)
        for text in (vitest(), vitest("skipped", "pending", "todo", "disabled")):
            self.assert_guard(1, format="vitest", text=text)

    def test_bad_structured_evidence_never_gets_an_exception(self):
        for format, text in (
            ("junit", None), ("junit", ""), ("junit", "<testsuite>"), ("junit", "<html/>"),
            ("junit", '<testsuite><testcase/></testsuite>'),
            ("junit", '<testsuites><testcase name="orphan"/></testsuites>'),
            ("junit", '<testsuite tests="4"/>'),
            ("junit", '<testsuite tests="-1"/>'),
            ("junit", '<testsuite tests="0" errors="1"/>'),
            ("junit", '<testsuite tests="1" skipped="0"><testcase name="x"><skipped/></testcase></testsuite>'),
            ("junit", '<testsuite tests="2"><testcase name="x"/></testsuite>'),
            ("junit", '<testsuite><testcase name="x"><skipped/><failure/></testcase></testsuite>'),
            ("vitest", None), ("vitest", ""), ("vitest", "{"), ("vitest", "{}"),
            ("vitest", '{"success":true,"testResults":[{"status":"passed"}]}'),
            ("vitest", vitest("unknown")),
            ("vitest", vitest(["passed"])),
        ):
            with self.subTest(format=format, text=text):
                self.assert_guard(1, format=format, text=text, exception={})

    def test_stale_report_is_deleted_before_command_starts(self):
        for format, text in (("junit", '<testsuite><testcase name="old"/></testsuite>'), ("vitest", vitest("passed"))):
            self.report.write_text(text)
            self.assert_guard(1, format=format, exception={})
            self.assertFalse(self.report.exists())

    def test_exact_empty_exception_and_reason_are_visible(self):
        for format, text in (("libtest", libtest(ignored=1)), ("junit", "<testsuite/>"), ("vitest", vitest("skipped"))):
            result = self.assert_guard(0, format=format, text=text, exception={})
            self.assertIn("No cases assigned to this shard", result.stdout)

    def test_exceptions_do_not_match_other_invocations_or_wildcards(self):
        for exception in ({"format": "junit"}, {"shard": "unit/2"}, {"argv": ["python", "-m", "unittest"]},
                          {"shard": "*"}, {"argv": ["*"]}, {"reason": " "}, {"all": True}):
            with self.subTest(exception=exception):
                self.assert_guard(1, text=libtest(), exception=exception)

    def test_report_option_is_mandatory_for_structured_formats(self):
        for format in ("junit", "vitest"):
            result = subprocess.run([sys.executable, str(SCRIPT), "--format", format, "--shard", "x", "--", "true"], capture_output=True)
            self.assertEqual(result.returncode, 2)

    def test_command_argv_is_not_interpreted_by_shell(self):
        result = self.assert_guard(0, text=None, command=[sys.executable, "-c", "import sys; print(sys.argv[1])", libtest(passed=1), "$(touch unexpected)"])
        self.assertIn("1 executed tests", result.stdout)
        self.assertFalse((self.root / "unexpected").exists())

    def test_command_not_found(self):
        self.assert_guard(127, command=[str(self.root / "missing")])

    def test_installation_is_independent_of_action_source_and_callable_from_path(self):
        paths = self.root / "paths"
        output = self.root / "outputs"
        run = yaml.safe_load((ACTION / "action.yml").read_text())["runs"]["steps"][0]["run"]
        env = {**os.environ, "RUNNER_TEMP": str(self.root), "GITHUB_ACTION_PATH": str(ACTION),
               "GITHUB_PATH": str(paths), "GITHUB_OUTPUT": str(output)}
        subprocess.run(["bash", "-e", "-c", run], env=env, check=True, capture_output=True)
        script = Path(output.read_text().strip().removeprefix("script="))
        self.assertTrue(script.is_absolute())
        self.assertNotEqual(script.parent, ACTION)
        self.assertEqual(script.parent, Path(paths.read_text().strip()))
        result = subprocess.run(["ci-test", "--format", "libtest", "--shard", "install", "--", sys.executable,
                                 "-c", f"print({libtest(passed=1)!r})"],
                                env={**os.environ, "PATH": str(script.parent) + os.pathsep + os.environ["PATH"]}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
