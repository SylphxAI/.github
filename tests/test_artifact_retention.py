"""Require explicit bounded retention-days on actions/upload-artifact steps."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
LINT_DIR = ROOT / ".github/actions/workflow-lint"
sys.path.insert(0, str(LINT_DIR))
SPEC = importlib.util.spec_from_file_location("workflow_contract_artifact", LINT_DIR / "contract.py")
contract = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(contract)

from artifact_retention import (
    DEFAULT_RETENTION_DAYS,
    MAX_EXCEPTION_RETENTION_DAYS,
    MAX_RETENTION_DAYS,
    UPLOAD_ARTIFACT,
    action_problems,
    workflow_problems,
)
from merge_lane import expression

PIN = "ea165f8d65b6e75b540449e92b4886f43607fa02"
HEADER = "# artifact-retention: exception release-proof — Signed release bundle retained for review.\n"
CONCURRENCY = (
    "# pr-concurrency: exception complete-evidence — Isolated artifact retention fixture.\n"
    "concurrency: {group: '${{ github.workflow }}-run-${{ github.run_id }}', cancel-in-progress: false}\n"
)


def make_workflow(step, *, trigger="push", header="", job_name="build"):
    return (
        header
        + f"on: {trigger}\n"
        + "jobs:\n"
        + f"  {job_name}:\n"
        + "    runs-on: sylphx-linux-standard\n"
        + "    timeout-minutes: 10\n"
        + "    steps:\n"
        + step
    )


def upload_step(uses=f"actions/upload-artifact@{PIN}", with_block=None):
    lines = [f"      - uses: {uses}"]
    if with_block is not None:
        lines.append("        with:")
        for line in with_block:
            lines.append("          " + line)
    return "\n".join(lines) + "\n"


def make_composite(step, *, header="", using="composite"):
    return (
        header
        + "name: Example action\n"
        + "description: Fixture\n"
        + "runs:\n"
        + f"  using: {using}\n"
        + "  steps:\n"
        + step
    )


class ArtifactRetentionWorkflowTests(unittest.TestCase):
    def test_valid_explicit_retention_days_one_through_three_pass(self):
        for uses in (
            f"actions/upload-artifact@{PIN}",
            "actions/upload-artifact@v4",
            "Actions/Upload-Artifact@v4",
            f"actions/upload-artifact/merge@{PIN}",
            "actions/upload-artifact/merge@v4",
        ):
            for value in ("1", "2", "3", '"1"', "'3'"):
                with self.subTest(uses=uses, value=value):
                    text = make_workflow(upload_step(uses, ["name: diag", "path: out/", f"retention-days: {value}"]))
                    self.assertEqual(workflow_problems(text), [])

    def test_missing_with_or_missing_retention_days_fails_closed(self):
        for step in (
            upload_step(with_block=None),
            upload_step(with_block=["name: diag", "path: out/"]),
            "      - uses: actions/upload-artifact@v4\n        with: null\n",
            "      - uses: actions/upload-artifact@v4\n        with: {}\n",
            "      - uses: actions/upload-artifact/merge@v4\n        with:\n          name: merged\n",
        ):
            with self.subTest(step=step):
                found = workflow_problems(make_workflow(step))
                self.assertEqual(len(found), 1)
                self.assertIn("must set explicit integer retention-days", found[0][1])
                self.assertIn("SylphxAI/.github/.github/actions/run-store", found[0][1])
                self.assertIn("build-cache/Ceph RGW, $0 GitHub storage", found[0][1])

    def test_zero_retention_days_is_refused_with_org_default_explanation(self):
        for value in ("0", '"0"', "'0'"):
            with self.subTest(value=value):
                text = make_workflow(upload_step(with_block=["name: diag", f"retention-days: {value}"]))
                found = workflow_problems(text)
                self.assertEqual(len(found), 1)
                self.assertIn("90-day org default", found[0][1])

    def test_invalid_non_integer_and_dynamic_retention_values_are_refused(self):
        invalid_values = (
            "-1",
            "01",
            "00",
            "+1",
            "1.5",
            "1.0",
            '""',
            "''",
            "null",
            "~",
            "true",
            "false",
            '" 1 "',
            "1d",
            "[1]",
            "{days: 1}",
            "${{ inputs.retention-days }}",
            "${{ vars.RETENTION || 1 }}",
            '"${{ env.RETENTION }}"',
        )
        for value in invalid_values:
            with self.subTest(value=value):
                text = make_workflow(upload_step(with_block=["name: diag", f"retention-days: {value}"]))
                found = workflow_problems(text)
                self.assertEqual(len(found), 1)
                if "${{" in value:
                    self.assertIn("dynamic expressions are refused", found[0][1])
                else:
                    self.assertIn("literal integer", found[0][1])

    def test_exceeding_standard_max_requires_release_proof_exception(self):
        for days in (4, 7, 14, 30, 90):
            with self.subTest(days=days):
                text = make_workflow(upload_step(with_block=["name: diag", f"retention-days: {days}"]))
                found = workflow_problems(text)
                self.assertEqual(len(found), 1)
                self.assertIn(f"retention-days={days} exceeds max {MAX_RETENTION_DAYS}", found[0][1])
                self.assertIn("SylphxAI/.github/.github/actions/run-store", found[0][1])
                self.assertIn("build-cache/Ceph RGW, $0 GitHub storage", found[0][1])

    def test_release_proof_exception_allows_up_to_thirty_days_only(self):
        for header in (
            HEADER,
            "# artifact-retention: exception release-proof: Signed iOS release proof.\n",
            "# artifact-retention: exception release-proof : Signed iOS release proof.\n",
        ):
            for days in (1, 3, 4, 7, 14, 30):
                with self.subTest(header=header, days=days):
                    text = make_workflow(
                        upload_step(with_block=["name: proof", f"retention-days: {days}"]),
                        header=header,
                    )
                    self.assertEqual(workflow_problems(text), [])
        for bad_days in (31, 90, 365):
            with self.subTest(bad_days=bad_days):
                text = make_workflow(
                    upload_step(with_block=["name: proof", f"retention-days: {bad_days}"]),
                    header=HEADER,
                )
                found = workflow_problems(text)
                self.assertEqual(len(found), 1)
                self.assertIn(f"exceeds max {MAX_EXCEPTION_RETENTION_DAYS}", found[0][1])
        # Exception never waives omitted, zero, or dynamic retention-days.
        for bad_with in (
            ["name: proof"],
            ["name: proof", "retention-days: 0"],
            ["name: proof", "retention-days: ${{ inputs.days }}"],
        ):
            with self.subTest(bad_with=bad_with):
                text = make_workflow(upload_step(with_block=bad_with), header=HEADER)
                self.assertEqual(len(workflow_problems(text)), 1)

    def test_invalid_or_spoofed_exception_headers_are_refused(self):
        invalid_headers = (
            "# artifact-retention: exempt — generic escape hatch\n",
            "# artifact-retention: exception unknown-kind — unsupported\n",
            "# artifact-retention: exception release-proof — \n",
            "# artifact-retention: exception release-proof — ---\n",
            "# artifact-retention: exception release-proof:\n",
            "# ARTIFACT-RETENTION: exception release-proof — wrong case\n",
            HEADER + "# artifact-retention: exempt — extra bad marker\n",
        )
        for bad_header in invalid_headers:
            with self.subTest(bad_header=bad_header):
                text = make_workflow(
                    upload_step(with_block=["name: proof", "retention-days: 14"]),
                    header=bad_header,
                )
                found = workflow_problems(text)
                self.assertTrue(any("artifact-retention exception must use" in msg for _, msg in found), found)
                # Invalid header does not grant the extended bound.
                self.assertTrue(any("exceeds max 3" in msg for _, msg in found), found)

        # Spoofed comments after jobs:, indented, or inside run: bodies never grant an exception.
        spoofed_cases = (
            make_workflow(
                f"      - run: |\n          {HEADER.strip()}\n          echo hi\n"
                + upload_step(with_block=["name: proof", "retention-days: 14"])
            ),
            make_workflow(
                "      " + HEADER
                + upload_step(with_block=["name: proof", "retention-days: 14"])
            ),
            "on: push\njobs:\n"
            + HEADER
            + "  build:\n    runs-on: sylphx-linux-standard\n    timeout-minutes: 5\n    steps:\n"
            + upload_step(with_block=["name: proof", "retention-days: 14"]),
        )
        for text in spoofed_cases:
            with self.subTest(text=text):
                found = workflow_problems(text)
                self.assertEqual(len(found), 1)
                self.assertIn("exceeds max 3", found[0][1])

    def test_duplicate_retention_or_with_keys_fail_closed(self):
        dup_case = make_workflow(
            upload_step(with_block=["name: diag", "retention-days: 1", "Retention-Days: 90"])
        )
        found = workflow_problems(dup_case)
        self.assertEqual(len(found), 1)
        self.assertIn("duplicate retention-days", found[0][1])

        dup_with = (
            "on: push\njobs:\n  build:\n    runs-on: sylphx-linux-standard\n"
            "    steps:\n      - uses: actions/upload-artifact@v4\n"
            "        with:\n          retention-days: 1\n"
            "        with:\n          retention-days: 90\n"
        )
        found = workflow_problems(dup_with)
        self.assertEqual(len(found), 1)
        self.assertIn("duplicate 'with'", found[0][1])

    def test_unrelated_actions_and_inline_mentions_are_ignored(self):
        steps = "\n".join([
            "      - uses: actions/download-artifact@v8",
            "      - uses: actions/upload-pages-artifact@v3",
            "      - uses: actions/upload-artifact-custom@v1",
            "      - uses: other-org/upload-artifact@v4",
            f"      - uses: SylphxAI/.github/.github/actions/run-store@{PIN}",
            "        with:",
            "          mode: put",
            "          name: junit-unit",
            "          path: junit.xml",
            "      - run: echo actions/upload-artifact@v4",
        ]) + "\n"
        self.assertEqual(workflow_problems(make_workflow(steps)), [])

    def test_all_trigger_events_and_flow_yaml_are_checked(self):
        for trigger in (
            "pull_request",
            "merge_group",
            "push",
            "schedule",
            "workflow_dispatch",
            "workflow_call",
            "release",
            "[push, workflow_call]",
        ):
            with self.subTest(trigger=trigger):
                text = make_workflow(upload_step(with_block=["name: diag"]), trigger=trigger)
                self.assertEqual(len(workflow_problems(text)), 1)
        flow = "on: push\njobs: {build: {steps: [{uses: 'actions/upload-artifact@v4', with: {name: diag}}]}}\n"
        self.assertEqual(len(workflow_problems(flow)), 1)
        flow_ok = "on: push\njobs: {build: {steps: [{uses: 'actions/upload-artifact@v4', with: {name: diag, retention-days: 1}}]}}\n"
        self.assertEqual(workflow_problems(flow_ok), [])

    def test_composite_actions_are_checked_with_same_bounds_and_exceptions(self):
        good = make_composite(upload_step(with_block=["name: diag", "retention-days: 1"]))
        self.assertEqual(action_problems(good), [])

        bad = make_composite(upload_step(with_block=["name: diag"]))
        found = action_problems(bad)
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0][1].startswith("composite action:"))

        over = make_composite(upload_step(with_block=["name: proof", "retention-days: 14"]))
        self.assertEqual(len(action_problems(over)), 1)
        self.assertEqual(action_problems(HEADER + over), [])

        # Exception placed after runs: cannot apply.
        spoofed = "name: Example\nruns:\n" + HEADER + "  using: composite\n  steps:\n" + upload_step(
            with_block=["name: proof", "retention-days: 14"]
        )
        self.assertEqual(len(action_problems(spoofed)), 1)

        # Non-composite actions are ignored.
        node_action = "name: Node\nruns:\n  using: node20\n  main: index.js\n"
        self.assertEqual(action_problems(node_action), [])


class ArtifactRetentionContractIntegrationTests(unittest.TestCase):
    def test_blocking_in_report_only_and_enforce_modes_for_workflows_and_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wf_dir = root / ".github/workflows"
            act_dir = root / ".github/actions/my-upload"
            wf_dir.mkdir(parents=True)
            act_dir.mkdir(parents=True)
            (wf_dir / "diag.yml").write_text(
                CONCURRENCY
                + make_workflow(upload_step(with_block=["name: diag"]), trigger="push")
            )
            (act_dir / "action.yml").write_text(
                make_composite(upload_step(with_block=["name: nested", "retention-days: 7"]))
            )
            event = root / "event.json"
            event.write_text('{"repository":{"private":false,"visibility":"public"}}')
            env = {k: v for k, v in os.environ.items() if not k.startswith("GITHUB_")}
            env["GITHUB_EVENT_PATH"] = str(event)
            for mode in ("report-only", "enforce"):
                with self.subTest(mode=mode):
                    run_env = {**env, "CONTRACT_MODE": mode}
                    proc = subprocess.run(
                        [sys.executable, str(LINT_DIR / "contract.py")],
                        cwd=root,
                        env=run_env,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                    self.assertIn("::error file=.github/workflows/diag.yml", proc.stdout)
                    self.assertIn("::error file=.github/actions/my-upload/action.yml", proc.stdout)

    def test_scoped_rules_artifact_retention_never_calls_unrelated_families(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wf_dir = root / ".github/workflows"
            act_dir = root / ".github/actions/sub/nested"
            wf_dir.mkdir(parents=True)
            act_dir.mkdir(parents=True)
            (wf_dir / "ci.yml").write_text(
                make_workflow(upload_step(with_block=["name: diag", "retention-days: 1"]), trigger="pull_request")
            )
            (act_dir / "action.yaml").write_text(
                make_composite(upload_step(with_block=["name: nested", "retention-days: 2"]))
            )
            with patch.dict(os.environ, {}, clear=True), \
                    patch.object(contract, "runner_evaluations", side_effect=AssertionError("runner evaluation")), \
                    patch.object(contract, "scan_workflow", side_effect=AssertionError("job contracts")), \
                    patch.object(contract, "gate_coverage", side_effect=AssertionError("ci-ok closure")), \
                    patch.object(contract, "check_declaration", side_effect=AssertionError("declarations")), \
                    patch.object(contract, "merge_lane_findings", side_effect=AssertionError("runner routing")), \
                    patch.object(contract, "pr_concurrency_problems", side_effect=AssertionError("pr concurrency")):
                self.assertEqual(contract.scan_dir(root, rules="artifact-retention"), [])
                (act_dir / "action.yaml").write_text(
                    make_composite(upload_step(with_block=["name: nested", "retention-days: 0"]))
                )
                found = contract.scan_dir(root, rules="artifact-retention")
                self.assertEqual(len(found), 1)
                self.assertEqual(found[0].file, ".github/actions/sub/nested/action.yaml")
                self.assertTrue(found[0].blocking)

    def test_scoped_entrypoint_ignores_unrelated_debt_and_rejects_bad_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wf_dir = root / ".github/workflows"
            wf_dir.mkdir(parents=True)
            # Contains unrelated debt: no concurrency, no timeout, manual approval, unknown runner.
            wf_path = wf_dir / "legacy.yml"
            legacy_ok = (
                "on: [pull_request, merge_group]\n"
                "jobs:\n"
                "  isolated:\n"
                "    runs-on: unknown-runner\n"
                "    steps:\n"
                "      - uses: trstringer/manual-approval@v1\n"
                "      - uses: actions/upload-artifact@v4\n"
                "        with:\n"
                "          name: diag\n"
                "          retention-days: 1\n"
            )
            wf_path.write_text(legacy_ok)
            env = {
                k: v for k, v in os.environ.items()
                if not k.startswith("GITHUB_") and k not in ("WORKFLOW_LINT_RULES", "CONTRACT_MODE")
            }
            env.update(CONTRACT_MODE="report-only", WORKFLOW_LINT_RULES="artifact-retention")
            cmd = [sys.executable, str(LINT_DIR / "contract.py")]
            ok = subprocess.run(cmd, cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
            self.assertIn("0 violation(s)", ok.stdout)

            wf_path.write_text(legacy_ok.replace("retention-days: 1", "retention-days: 14"))
            bad = subprocess.run(cmd, cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(bad.returncode, 1, bad.stdout + bad.stderr)
            self.assertIn("retention-days=14 exceeds max 3", bad.stdout)

            for invalid in ("", "typo", "artifact-retention,all"):
                env["WORKFLOW_LINT_RULES"] = invalid
                refused = subprocess.run(cmd, cwd=root, env=env, capture_output=True, text=True)
                self.assertNotEqual(refused.returncode, 0)
                self.assertIn("unknown workflow-lint rules", refused.stderr)

    def test_composite_action_yml_validates_and_selects_artifact_retention_scope(self):
        action = yaml.safe_load((LINT_DIR / "action.yml").read_text())
        context = {"inputs.rules": {"artifact-retention"}, "steps.restore-binary.outputs.cache-hit": {"false"}}
        selected = [
            step for step in action["runs"]["steps"]
            if not step.get("if") or expression("${{ " + step["if"] + " }}", context) == {True}
        ]
        self.assertEqual(
            [s["name"] for s in selected],
            ["Validate rule selection", "Required workflow contract"],
        )
        validator = action["runs"]["steps"][0]["run"]
        for rules, expected in (("all", 0), ("pr-concurrency", 0), ("artifact-retention", 0), ("", 1), ("typo", 1)):
            checked = subprocess.run(
                ["bash", "-euo", "pipefail", "-c", validator],
                env={**os.environ, "RULES": rules},
                capture_output=True,
                text=True,
            )
            self.assertEqual(checked.returncode, expected, checked.stdout + checked.stderr)

    def test_hands_off_and_delivered_repositories_skip_artifact_retention(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wf_dir = root / ".github/workflows"
            wf_dir.mkdir(parents=True)
            (wf_dir / "diag.yml").write_text(
                make_workflow(upload_step(with_block=["name: diag"]), trigger="push")
            )
            with patch.dict(os.environ, {"GITHUB_REPOSITORY": "SylphxAI/bgca"}, clear=True):
                self.assertEqual(contract.scan_dir(root, rules="artifact-retention"), [])
            with patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "WORKFLOW_LINT_RULES": "artifact-retention"}, clear=True), \
                    patch.object(contract, "delivered", return_value=True), \
                    patch.object(contract, "scan_dir", side_effect=AssertionError("must skip")), \
                    patch("builtins.print"):
                self.assertEqual(contract.main(), 0)

    def test_this_repository_workflows_actions_and_templates_pass_artifact_retention(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(contract.scan_dir(ROOT, rules="artifact-retention"), [])
        for folder in (ROOT / ".github/workflows", ROOT / "workflow-templates"):
            for path in sorted(folder.iterdir()):
                if path.is_file() and path.suffix in (".yml", ".yaml"):
                    text = path.read_text(encoding="utf-8")
                    self.assertEqual(workflow_problems(text), [], str(path))
                    doc = yaml.load(text, Loader=yaml.BaseLoader) or {}
                    for job in (doc.get("jobs") or {}).values():
                        for step in (job or {}).get("steps") or []:
                            uses = str((step or {}).get("uses", "")).strip()
                            self.assertFalse(UPLOAD_ARTIFACT.match(uses), f"{path}: {uses}")
        for path in sorted((ROOT / ".github/actions").rglob("action.y*ml")):
            if path.is_file() and path.name in ("action.yml", "action.yaml"):
                text = path.read_text(encoding="utf-8")
                self.assertEqual(action_problems(text), [], str(path))
                doc = yaml.load(text, Loader=yaml.BaseLoader) or {}
                for step in ((doc.get("runs") or {}).get("steps") or []):
                    uses = str((step or {}).get("uses", "")).strip()
                    self.assertFalse(UPLOAD_ARTIFACT.match(uses), f"{path}: {uses}")
        self.assertEqual(DEFAULT_RETENTION_DAYS, 1)


if __name__ == "__main__":
    unittest.main()
