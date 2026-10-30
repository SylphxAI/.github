#!/usr/bin/env python3
"""The shared actions' shell scripts run on Linux and on macOS runners."""

from __future__ import annotations

import pathlib
import re
import unittest

ACTIONS = pathlib.Path(__file__).resolve().parents[1] / ".github" / "actions"


class PortableRegexTest(unittest.TestCase):
    """macOS runners use bash 3.2 on the BSD regex library, whose RE_DUP_MAX is
    255: a bounded repeat above it ({1,8192}) does not compile there, so `=~`
    returns 2 and every check built on it fails. glibc allows 32767, so a Linux
    run cannot show it; read the bounds instead."""

    def test_no_bounded_repeat_over_255(self) -> None:
        for script in sorted(ACTIONS.rglob("*.sh")):
            for n, line in enumerate(script.read_text().splitlines(), 1):
                if "=~" not in line and "_re=" not in line:
                    continue
                for lo, hi in re.findall(r"\{(\d+)(?:,(\d*))?\}", line):
                    for bound in (lo, hi):
                        if bound:
                            self.assertLessEqual(int(bound), 255, f"{script.name}:{n}: {line.strip()}")


class OptionalArgumentsTest(unittest.TestCase):
    """Run each optional-input action step, including its real shell parsing."""

    def test_optional_inputs_keep_their_argument_boundaries(self):
        import json
        import os
        import subprocess
        import sys
        import tempfile
        import yaml

        cases = (
            ("plain-language", "Check added lines for coined terms", "EXCLUDE",
             "path with spaces", [":(exclude)path with spaces"]),
            ("identifiers", "Check added lines for non-v7 identifiers", "EXCLUDE",
             "path with spaces", [":(exclude)path with spaces"]),
            ("workflow-pin-check", "Check the workflow pins", "OWNERS",
             "first second", ["--owner", "first", "--owner", "second"]),
        )
        for action, step_name, variable, value, expected in cases:
            steps = yaml.safe_load((ACTIONS / action / "action.yml").read_text())["runs"]["steps"]
            script = next(step["run"] for step in steps if step.get("name") == step_name)
            for configured in (False, True):
                with self.subTest(action=action, configured=configured), tempfile.TemporaryDirectory() as tmp:
                    root = pathlib.Path(tmp)
                    fakebin = root / "bin"
                    fakebin.mkdir()
                    capture = (f"#!{sys.executable}\nimport json, os, sys\n"
                               "open(os.environ['ARGV_OUT'], 'w').write(json.dumps(sys.argv[1:]))\n")
                    for name in ("python3", "check.sh"):
                        target = fakebin / name
                        target.write_text(capture)
                        target.chmod(0o755)
                    (fakebin / "gh").write_text("#!/bin/sh\necho '[]'\n")
                    (fakebin / "gh").chmod(0o755)
                    (fakebin / "range.sh").write_text("#!/bin/sh\necho 'base..head'\n")
                    env = {**os.environ, "PATH": f"{fakebin}:{os.environ['PATH']}",
                           "ARGV_OUT": str(root / "argv.json"), "GITHUB_ACTION_PATH": str(fakebin),
                           "ACTION_PATH": str(fakebin), "GITHUB_WORKSPACE": str(root),
                           "RUNNER_TEMP": str(root), "GITHUB_OUTPUT": str(root / "outputs"),
                           "GITHUB_REPOSITORY": "example/title", "GH_TOKEN": "fixture-token",
                           "EVENT_NAME": "push", "PUSH_BEFORE": "base", "GIT_SHA": "head",
                           "LIST": "terms", "MODE": "warn", "EXCLUDE": "", "OWNERS": "",
                           "MODEL_ALIASES": "", "BASE": "base", "HEAD_REV": "head",
                           "BASELINE": ".baseline", variable: value if configured else ""}
                    result = subprocess.run([os.environ.get("BASH_UNDER_TEST", "bash"),
                        "--posix", "-euc", script], cwd=root, env=env, text=True, capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    argv = json.loads((root / "argv.json").read_text())
                    self.assertNotIn("", argv)
                    if configured:
                        if action == "workflow-pin-check":
                            self.assertEqual(argv[1:1 + len(expected)], expected)
                        else:
                            self.assertEqual(argv[-len(expected):], expected)
                    else:
                        for argument in expected:
                            self.assertNotIn(argument, argv)


if __name__ == "__main__":
    unittest.main()
