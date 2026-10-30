"""Real libtest, pytest JUnit and Vitest reporters through the shipped CLI."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(os.environ.get("CI_TEST_SCRIPT", Path(__file__).resolve().parents[1] / ".github/actions/setup-ci-test/ci_test.py"))


class RealRunners(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ci-test-runners-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def require(self, available, name):
        if not available:
            if os.environ.get("CI_TEST_REQUIRE_RUNNERS") == "1":
                self.fail(f"CI must provide the real {name} runner")
            self.skipTest(f"{name} unavailable; mandatory in CI")

    def guard(self, format, command, expected, exception=False):
        report = self.root / ("result.json" if format == "vitest" else "result.xml")
        args = [sys.executable, str(SCRIPT), "--format", format, "--shard", "native"]
        if format != "libtest":
            args += ["--report", str(report)]
        if exception:
            declarations = self.root / "empty.json"
            declarations.write_text(json.dumps([{"format": format, "shard": "native", "argv": command,
                                                  "reason": "Intentional empty native-runner control"}]))
            args += ["--exceptions", str(declarations)]
        result = subprocess.run(args + ["--"] + command, cwd=self.root, capture_output=True, text=True,
                                env={**os.environ, "NO_COLOR": "1"}, timeout=60)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def test_real_libtest(self):
        self.require(shutil.which("rustc"), "rustc")
        source = self.root / "suite.rs"
        source.write_text('''
#[test] fn passing() { assert_eq!(2 + 2, 4); }
#[test] fn failing() { panic!("negative control"); }
#[test] #[ignore = "native guard control"] fn ignored() {}
''')
        binary = self.root / "suite"
        subprocess.run(["rustc", "--test", str(source), "-o", str(binary)], check=True, capture_output=True, timeout=60)
        self.guard("libtest", [str(binary), "passing"], 0)
        self.guard("libtest", [str(binary), "passing", "--format", "terse"], 0)
        self.guard("libtest", [str(binary), "ignored"], 1)
        self.guard("libtest", [str(binary), "missing-filter"], 1)
        self.guard("libtest", [str(binary), "--list"], 1)
        self.guard("libtest", [str(binary), "failing"], 101, exception=True)
        self.guard("libtest", [str(binary), "missing-filter"], 0, exception=True)

    def test_real_pytest_junit(self):
        probe = subprocess.run([sys.executable, "-m", "pytest", "--version"], capture_output=True)
        self.require(probe.returncode == 0, "pytest")
        (self.root / "test_sample.py").write_text('''
import pytest
def test_passing(): assert 2 + 2 == 4
def test_failing(): assert False, "negative control"
@pytest.mark.skip(reason="native guard control")
def test_skipped(): pass
''')
        base = [sys.executable, "-m", "pytest", "-q", "--junitxml=result.xml"]
        self.guard("junit", base + ["-k", "passing"], 0)
        self.guard("junit", base + ["-k", "skipped"], 1)
        self.guard("junit", base + ["-k", "missing_filter"], 5, exception=True)
        self.guard("junit", base + ["-k", "failing"], 1, exception=True)
        (self.root / "test_sample.py").write_text("raise RuntimeError('collection failure')\n")
        self.guard("junit", base, 2, exception=True)

    def test_real_vitest_json(self):
        executable = os.environ.get("CI_TEST_VITEST_BIN") or shutil.which("vitest")
        self.require(executable, "Vitest")
        # Globals avoid resolving an import from this isolated consumer directory.
        (self.root / "package.json").write_text('{"type":"module"}')
        (self.root / "sample.test.js").write_text('''
test('passing', () => { expect(2 + 2).toBe(4) })
test('failing', () => { expect(1).toBe(2) })
test.skip('ignored', () => {})
''')
        base = [str(executable), "run", "--globals", "--maxWorkers=1", "--reporter=json", "--outputFile=result.json"]
        self.guard("vitest", base + ["-t", "passing"], 0)
        self.guard("vitest", base + ["-t", "ignored"], 1)
        self.guard("vitest", base + ["-t", "missing-filter"], 1)
        self.guard("vitest", base + ["-t", "failing"], 1, exception=True)
        self.guard("vitest", base + ["not-present.test.js", "--passWithNoTests"], 1)


if __name__ == "__main__":
    unittest.main()
