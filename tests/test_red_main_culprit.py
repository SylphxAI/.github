#!/usr/bin/env python3
"""Tests for the red-main culprit rule (CULPRIT_PY in red-main.yml).

Every candidate is verified at once; the culprit is the oldest failure whose
older candidates all passed. A cancelled, timed-out or unfinished run is
inconclusive and is never blamed.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/red-main.yml"


def embedded(name: str) -> str:
    """The block scalar `name: |` in the workflow env, dedented (no YAML dependency)."""
    lines = WORKFLOW.read_text().splitlines()
    start = lines.index(f"      {name}: |") + 1
    body = []
    for line in lines[start:]:
        if line.strip() and not line.startswith("        "):
            break
        body.append(line[8:])
    return "\n".join(body) + "\n"


SCRIPT = embedded("CULPRIT_PY")


def decide(*conclusions: str) -> list[str]:
    """Candidates c1..cN, oldest first, with the given conclusions."""
    rows = "".join(f"c{i + 1}\t{c}\n" for i, c in enumerate(conclusions))
    out = subprocess.run(
        [sys.executable, "-c", SCRIPT], input=rows, capture_output=True, text=True, check=True
    ).stdout
    return out.splitlines()


class CulpritRuleTest(unittest.TestCase):
    def test_all_pass(self) -> None:
        self.assertEqual(decide("success", "success", "success"), ["none"])

    def test_oldest_failure_with_passing_ancestors(self) -> None:
        self.assertEqual(decide("success", "failure", "failure")[0], "culprit c2")

    def test_first_candidate_fails(self) -> None:
        self.assertEqual(decide("failure", "success")[0], "culprit c1")

    def test_cancelled_before_first_failure_reverts_window(self) -> None:
        self.assertEqual(decide("success", "cancelled", "failure")[0], "window")

    def test_startup_failure_before_first_failure_reverts_window(self) -> None:
        self.assertEqual(decide("startup_failure", "failure")[0], "window")

    def test_timeout_reverts_window(self) -> None:
        self.assertEqual(decide("success", "timeout", "success")[0], "window")

    def test_timed_out_conclusion_reverts_window(self) -> None:
        self.assertEqual(decide("timed_out", "success")[0], "window")

    def test_failure_then_cancelled_after_it_names_the_failure(self) -> None:
        self.assertEqual(decide("success", "failure", "cancelled")[0], "culprit c2")

    def test_window_says_why(self) -> None:
        out = decide("success", "cancelled")
        self.assertEqual(out[0], "window")
        self.assertIn("cancelled", out[1])


def step(name: str) -> str:
    """The text of the workflow step called `name` (up to the next step)."""
    text = WORKFLOW.read_text()
    start = text.index(f"      - name: {name}\n")
    end = text.find("\n      - name: ", start + 1)
    return text[start : end if end != -1 else len(text)]


GUARD = embedded("REVERT_GUARD_PY")


def guard(number: int, labels: list[str], protected_prs: str = "") -> str:
    import json
    import os

    env = {**os.environ, "PROTECTED_LABELS": "queue-jump:outage,P0", "PROTECTED_PRS": protected_prs}
    out = subprocess.run(
        [sys.executable, "-c", GUARD],
        input=json.dumps({"number": number, "labels": labels}),
        capture_output=True,
        text=True,
        check=True,
        env=env,
    ).stdout
    return out.strip()


class RevertGuardTest(unittest.TestCase):
    """A live-outage or P0 fix, or a listed pull request, is never auto-reverted."""

    def test_an_ordinary_pull_request_may_be_reverted(self) -> None:
        self.assertEqual(guard(1, ["area:ci", "owner:ops"]), "ok")

    def test_outage_label_is_protected(self) -> None:
        self.assertTrue(guard(2, ["queue-jump:outage"]).startswith("protected"))

    def test_p0_label_is_protected_in_any_case_or_prefix(self) -> None:
        self.assertTrue(guard(3, ["P0"]).startswith("protected"))
        self.assertTrue(guard(3, ["p0"]).startswith("protected"))
        self.assertTrue(guard(3, ["priority:P0"]).startswith("protected"))

    def test_a_listed_number_is_protected_whatever_its_labels(self) -> None:
        self.assertTrue(guard(10791, [], "10791, #10808").startswith("protected"))
        self.assertTrue(guard(10808, ["area:ci"], "10791,#10808").startswith("protected"))
        self.assertEqual(guard(10792, [], "10791,10808"), "ok")

    def test_red_main_label_alone_is_not_protected(self) -> None:
        self.assertEqual(guard(4, ["queue-jump:red-main"]), "ok")

    def test_the_revert_step_checks_the_guard_before_any_push(self) -> None:
        revert = step("Revert the culprit or report it")
        self.assertLess(revert.index("revert-guard.py"), revert.index("git_c revert"))
        self.assertLess(revert.index("revert-guard.py"), revert.index("push_branch"))


class TokenModeTest(unittest.TestCase):
    """No builder App key: notify on the caller's own GITHUB_TOKEN."""

    def test_no_permissions_block_at_any_level(self) -> None:
        lines = WORKFLOW.read_text().splitlines()
        self.assertEqual([l for l in lines if l.lstrip().startswith("permissions:")], [])

    def test_no_write_permission_is_requested_for_the_token(self) -> None:
        # `permission-<x>: write` inputs of the App mint are the App's own grant;
        # a `<x>: write` line anywhere else would request it from GITHUB_TOKEN.
        for line in WORKFLOW.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("permission-") or stripped.startswith("#"):
                continue
            self.assertNotRegex(stripped, r"^(actions|checks|contents|issues|pull-requests|packages|id-token|statuses): write$")

    def test_key_flag_reads_the_secret_outside_an_if(self) -> None:
        detect = step("Detect the builder App key")
        self.assertIn("secrets.app-private-key || secrets.SYLPHX_BUILDER_PRIVATE_KEY", detect)
        for line in WORKFLOW.read_text().splitlines():
            if line.strip().startswith("if:"):
                self.assertNotIn("secrets.", line)

    def test_mint_steps_need_the_key_flag(self) -> None:
        for name in (
            "Mint the App token for the caller's repository",
            "Mint the App token with the observation grant only",
        ):
            self.assertIn("steps.key.outputs.present == 'yes'", step(name))

    def test_token_falls_back_to_github_token(self) -> None:
        text = WORKFLOW.read_text()
        self.assertNotIn("app-token-min.outputs.token }}", text)
        self.assertIn("steps.app-token-min.outputs.token || github.token }}", text)

    def test_no_key_forces_notify(self) -> None:
        detect = step("Detect the builder App key")
        self.assertIn("echo \"mode=notify\"", detect)
        self.assertIn("revert needs a CI-triggering token (the builder App); notifying only.", detect)
        revert = step("Revert the culprit or report it")
        self.assertIn("MODE: ${{ steps.key.outputs.mode || steps.gate.outputs.mode }}", revert)

    def test_quarantine_pull_request_needs_the_key(self) -> None:
        self.assertIn("steps.key.outputs.present == 'yes'", step("Mark the flaky units in their own source"))

    def test_revert_pull_request_is_unreachable_in_notify(self) -> None:
        revert = step("Revert the culprit or report it")
        self.assertLess(revert.index('if [ "$MODE" != "revert" ]'), revert.index("git_c revert"))

    def test_refused_refs_skip_the_trace_with_a_reason(self) -> None:
        trace = step("Trace the culprit among the unverified commits")
        self.assertIn("refs_refused", trace)
        self.assertIn("needs contents: write", trace)

    def test_summary_lists_what_a_person_must_do(self) -> None:
        self.assertIn("A person must:", step("Record what the handler did"))

    def test_templates_still_parse(self) -> None:
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is not installed")
        # The red-main starter lands with #108; nothing to parse until it does.
        for path in (ROOT / "workflow-templates").glob("red-main*.yml"):
            self.assertIsInstance(yaml.safe_load(path.read_text()), dict, path.name)
        doc = yaml.safe_load(WORKFLOW.read_text())
        self.assertNotIn("permissions", doc)


if __name__ == "__main__":
    unittest.main()
