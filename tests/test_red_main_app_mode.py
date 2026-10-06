#!/usr/bin/env python3
"""App-mode regressions found by the seeded-red drill on a sandbox repository.

1. The grant probe records `granted (App)` for an App token; the revert step's
   `require_grant` accepted only the bare word `granted`, so every App-mode
   revert was refused.
2. The candidate dispatch read `.workflow_run_id` from a reply the forge sends
   only when `return_run_details` is requested; without it the run id was
   empty and every candidate read as timed out.
3. The dispatch went out the moment the temporary `sylphx-verify/<sha>` ref
   was created, and the forge answered 422 "No ref found" for it (its ref
   reads lag a write by seconds), so the trace stopped with no culprit. That
   one answer is retried briefly; any other failure still stops at once.
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


def run_dispatch(answers: list[str], max_calls: int = 20) -> tuple[int, str, str, int]:
    """Run the trace step's dispatch() against a stub `gha` that replays `answers`.

    Each answer is `ok <id>` (print the id) or `err <message>` (fail with it on
    stderr). The call count lives in a file: dispatch() calls gha in a command
    substitution, a subshell. Returns (exit code, stdout, stderr, number of gha calls).
    """
    trace = step("Trace the culprit among the unverified commits")
    start = trace.index("          dispatch() {")
    end = trace.index("          # Every candidate at once")
    body = "\n".join(line[10:] for line in trace[start:end].splitlines())
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "answers").write_text("\n".join(answers) + "\n")
        stub = f"""
echo 0 >"{tmp}/calls"
gha() {{
  local calls; calls=$(( $(cat "{tmp}/calls") + 1 )); echo "$calls" >"{tmp}/calls"
  [ "$calls" -le {max_calls} ] || {{ echo "too many calls" >&2; return 9; }}
  cat >/dev/null
  local a; a=$(sed -n "${{calls}}p" "{tmp}/answers")
  case "$a" in
    ok\\ *) printf '%s\\n' "${{a#ok }}" ;;
    err\\ *) printf '%s\\n' "${{a#err }}" >&2; return 1 ;;
    *) echo "no answer" >&2; return 1 ;;
  esac
}}
sleep() {{ :; }}
REPO=o/r VERIFY_WORKFLOW=verify.yml dispatch_lanes= LANE_INPUT= WORK_DIR={tmp}
"""
        script = "set -euo pipefail\n" + stub + body + '\ndispatch "refs/heads/sylphx-verify/abc"\n'
        done = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        calls = int(Path(tmp, "calls").read_text()) if Path(tmp, "calls").exists() else 0
        return done.returncode, done.stdout, done.stderr, calls


NO_REF = "err gh: No ref found for: refs/heads/sylphx-verify/abc (HTTP 422)"


class DispatchRefLagTest(unittest.TestCase):
    def test_a_ref_not_yet_visible_is_retried_until_the_dispatch_lands(self) -> None:
        code, out, _err, calls = run_dispatch([NO_REF, NO_REF, "ok 4242"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "4242")
        self.assertEqual(calls, 3)

    def test_the_retry_is_bounded(self) -> None:
        code, out, err, calls = run_dispatch([NO_REF] * 20)
        self.assertNotEqual(code, 0)
        self.assertEqual(out.strip(), "")
        self.assertIn("No ref found", err)
        self.assertLessEqual(calls, 8)
        self.assertGreater(calls, 1)

    def test_any_other_failure_stops_at_once(self) -> None:
        code, _out, err, calls = run_dispatch(["err gh: Resource not accessible by integration (HTTP 403)", "ok 1"])
        self.assertNotEqual(code, 0)
        self.assertEqual(calls, 1)
        self.assertIn("HTTP 403", err)

    def test_an_empty_run_id_is_still_refused(self) -> None:
        code, _out, err, calls = run_dispatch(["ok ", "ok 1"])
        self.assertNotEqual(code, 0)
        self.assertEqual(calls, 1)
        self.assertIn("no run id", err)


if __name__ == "__main__":
    unittest.main()
