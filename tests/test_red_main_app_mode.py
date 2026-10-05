#!/usr/bin/env python3
"""App-mode regressions found by the seeded-red drill on a sandbox repository.

1. The grant probe records `granted (App)` for an App token; the revert step's
   `require_grant` accepted only the bare word `granted`, so every App-mode
   revert was refused.
2. The candidate dispatch read `.workflow_run_id` from a reply the forge sends
   only when `return_run_details` is requested; without it the run id was
   empty and every candidate read as timed out.
"""

from __future__ import annotations

import os
import subprocess
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


def step(name: str) -> str:
    text = WORKFLOW.read_text()
    start = text.index(f"      - name: {name}\n")
    end = text.find("\n      - name: ", start + 1)
    return text[start : end if end != -1 else len(text)]


def require_grant(verdict: str) -> int:
    """Run the LIB's require_grant against a probe file holding `verdict` for contents."""
    lib = embedded("LIB")
    start = lib.index("        grant_of() {".strip()) if "grant_of() {" in lib else -1
    assert start != -1, "grant_of not found in LIB"
    end = lib.index("# write_refused")
    funcs = lib[start:end]
    with tempfile.TemporaryDirectory() as state:
        Path(state, "grant.tsv").write_text(f"contents\t{verdict}\n")
        script = "state_set() { :; }\nGRANT_HOLDER=test\n" + funcs + '\nrequire_grant contents "contents: write" "x"\n'
        return subprocess.run(["bash", "-c", script], env={**os.environ, "STATE_DIR": state},
                              capture_output=True, text=True).returncode


class GrantTest(unittest.TestCase):
    def test_app_grant_is_accepted(self) -> None:
        self.assertEqual(require_grant("granted (App)"), 0)

    def test_token_grant_is_accepted(self) -> None:
        self.assertEqual(require_grant("granted"), 0)

    def test_anything_else_is_refused(self) -> None:
        for verdict in ("read-only", "missing", "unknown", "", "granted-ish", "not granted (App)"):
            self.assertNotEqual(require_grant(verdict), 0, verdict)

    def test_every_probe_verdict_for_a_write_surface_is_accepted(self) -> None:
        import re
        for verdict in set(re.findall(r'grant (?:contents|issues|pull-requests) "?(granted[^"\n]*)"?', WORKFLOW.read_text())):
            self.assertEqual(require_grant(verdict), 0, verdict)


class DispatchTest(unittest.TestCase):
    def test_dispatch_asks_for_the_run_id_and_refuses_an_empty_one(self) -> None:
        trace = step("Trace the culprit among the unverified commits")
        dispatch = trace[trace.index("dispatch() {") : trace.index("# Every candidate at once")]
        self.assertEqual(dispatch.count("return_run_details: true"), 2)
        self.assertIn(".workflow_run_id // empty", dispatch)
        self.assertIn("''|*[!0-9]*)", dispatch)


if __name__ == "__main__":
    unittest.main()
