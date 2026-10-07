#!/usr/bin/env python3
"""The App token must be live for every write after the trace.

An installation token lives 60 minutes, and the trace step may run 70. A
slow trace once left the revert step on an expired token: every read and
comment got HTTP 401, the error body was read as a pull request number, and
the step still concluded success. The handler now mints a fresh token after
the trace, a failed commit-to-pull-request read stops the step, and a write
answered with HTTP 401 fails it.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/red-main.yml"
TEXT = WORKFLOW.read_text()


def embedded(name: str) -> str:
    lines = TEXT.splitlines()
    start = lines.index(f"      {name}: |") + 1
    body = []
    for line in lines[start:]:
        if line.strip() and not line.startswith("        "):
            break
        body.append(line[8:])
    return "\n".join(body) + "\n"


def step(name: str) -> str:
    start = TEXT.index(f"      - name: {name}\n")
    end = TEXT.find("\n      - name: ", start + 1)
    return TEXT[start : end if end != -1 else len(TEXT)]


def function(lib: str, name: str) -> str:
    start = lib.index(f"{name}() {{")
    end = lib.index("\n}\n", start) + 3
    return lib[start:end]


def run(script: str, path: str | None = None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "REPO": "o/r"}
    if path:
        env["PATH"] = f"{path}:{env['PATH']}"
    return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)


class FreshTokenTest(unittest.TestCase):
    def test_second_mint_sits_between_trace_and_revert(self) -> None:
        trace = TEXT.index("      - name: Trace the culprit among the unverified commits\n")
        mint = TEXT.index("      - name: Mint a fresh App token after the trace\n")
        revert = TEXT.index("      - name: Revert the culprit or report it\n")
        self.assertLess(trace, mint)
        self.assertLess(mint, revert)

    def test_second_mint_asks_for_the_same_grant(self) -> None:
        first = step("Mint the App token for the caller's repository")
        second = step("Mint a fresh App token after the trace")
        perms = lambda s: sorted(re.findall(r"permission-[a-z-]+: \w+", s))
        self.assertEqual(perms(first), perms(second))
        self.assertIn("always()", second)
        self.assertNotIn("continue-on-error", second)

    def test_steps_after_the_trace_use_the_fresh_token(self) -> None:
        for name in ("Revert the culprit or report it", "Record what the handler did"):
            token = re.search(r"GH_TOKEN: \$\{\{ (.*) \}\}", step(name)).group(1)
            self.assertTrue(token.startswith("steps.app-token-act.outputs.token"), (name, token))


class FailClosedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.lib = embedded("LIB")

    def fake_gh(self, directory: str) -> str:
        gh = Path(directory, "gh")
        gh.write_text(
            "#!/bin/sh\n"
            'printf \'{\\n  "message": "Bad credentials",\\n  "status": "401"\\n}\\n\'\n'
            "echo 'gh: Bad credentials (HTTP 401)' >&2\n"
            "exit 1\n"
        )
        gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
        return directory

    def test_failed_pr_read_is_not_a_pr_number(self) -> None:
        with tempfile.TemporaryDirectory() as bin_dir:
            script = (
                "set -euo pipefail\n"
                + function(self.lib, "commit_prs")
                + "pr=$(commit_prs abc '.[0].number // empty')\n"
                + 'echo "reached $pr"\n'
            )
            result = run(script, self.fake_gh(bin_dir))
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("reached", result.stdout)
        self.assertIn("HTTP 401", result.stderr)

    def test_rejected_credentials_fail_the_step(self) -> None:
        script = (
            "state_set() { :; }\nGRANT_HOLDER=test\n"
            + function(self.lib, "write_refused")
            + 'write_refused "gh: Bad credentials (HTTP 401)" "pull requests: write" "commenting" || true\n'
            + "echo survived\n"
        )
        result = run(script)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("survived", result.stdout)

    def test_missing_grant_still_returns_to_the_caller(self) -> None:
        script = (
            "state_set() { :; }\nGRANT_HOLDER=test\n"
            + function(self.lib, "write_refused")
            + 'write_refused "HTTP 403" "pull requests: write" "commenting" || true\n'
            + "echo survived\n"
        )
        result = run(script)
        self.assertEqual(result.returncode, 0)
        self.assertIn("survived", result.stdout)


if __name__ == "__main__":
    unittest.main()
