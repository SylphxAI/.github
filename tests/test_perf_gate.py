#!/usr/bin/env python3
"""Tests for workflow-lint's rule: no timing or performance budget before merge."""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
ACTION = ROOT / ".github" / "actions" / "workflow-lint"

spec = importlib.util.spec_from_file_location("perf_gate", ACTION / "perf_gate.py")
perf_gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(perf_gate)

LIGHTHOUSE_PR = """\
name: CI
on:
  pull_request:
  push:
    branches: [main]
jobs:
  web:
    runs-on: sylphx-linux-standard
    steps:
      - uses: actions/checkout@v4
      - name: Lighthouse report
        run: npx lhci autorun --budget-path=budget.json
"""


def repo(tmp: str, workflows: dict[str, str]) -> pathlib.Path:
    root = pathlib.Path(tmp, "repo")
    folder = root / ".github" / "workflows"
    folder.mkdir(parents=True)
    for name, text in workflows.items():
        (folder / name).write_text(text)
    return root


def problems(text: str) -> list[tuple[int, str, str]]:
    return [(f.line, f.where, f.problem) for f in perf_gate.scan_workflow("w.yml", text)]


class TriggerTest(unittest.TestCase):
    def test_block_list_flow_and_scalar_forms(self) -> None:
        t = lambda s: perf_gate.triggers(s.splitlines())  # noqa: E731
        self.assertEqual(t(LIGHTHOUSE_PR), ["pull_request", "push"])
        self.assertEqual(t("on: [push, merge_group]\njobs: {}\n"), ["push", "merge_group"])
        self.assertEqual(t("'on': pull_request\n"), ["pull_request"])
        self.assertEqual(t("on:\n  - push\n  - workflow_call\n"), ["push", "workflow_call"])
        self.assertEqual(t("on: {push: {branches: [main]}}\n")[0], "push")
        self.assertEqual(t("# on: pull_request\non:\n  push:\n    branches:\n      - main\n"), ["push"])


class ScanTest(unittest.TestCase):
    def test_lighthouse_in_a_pull_request_workflow_fails(self) -> None:
        self.assertEqual(problems(LIGHTHOUSE_PR),
                         [(12, "job web", "runs a Lighthouse budget in a pull_request workflow")])

    def test_each_gate_in_each_pre_merge_trigger(self) -> None:
        for trigger in ("pull_request", "pull_request_target", "merge_group", "workflow_call"):
            for run in ("hyperfine 'bun x'", "k6 run load.js", "bun run perf:web", "e2e --perf-only",
                        "PERF_ENFORCE=1 bun test", "uses: treosh/lighthouse-ci-action@v12"):
                body = run if run.startswith("uses:") else f"run: {run}"
                text = f"on:\n  {trigger}:\njobs:\n  a:\n    steps:\n      - {body}\n"
                self.assertEqual(len(problems(text)), 1, (trigger, run))

    def test_dependency_audits_fail_for_every_pre_merge_trigger(self) -> None:
        for trigger in perf_gate.PRE_MERGE:
            for command in ("bun audit --audit-level=high --prod", "npm audit --omit=dev",
                            "pnpm audit --prod", "yarn audit", "yarn npm audit",
                            "npm --production audit", "bun audit || true"):
                with self.subTest(trigger=trigger, command=command):
                    text = f"on: {trigger}\njobs:\n  a:\n    steps:\n      - run: {command}\n"
                    found = problems(text)
                    self.assertEqual(len(found), 1)
                    self.assertIn("dependency audit", found[0][2])

    def test_dependency_audits_belong_to_a_separate_default_branch_workflow(self) -> None:
        text = ("on:\n  schedule:\n    - cron: '17 6 * * *'\n"
                "  push:\n    branches: [main]\n  workflow_dispatch:\njobs:\n  a:\n"
                "    steps:\n      - run: bun audit --audit-level=high --prod\n")
        self.assertEqual(problems(text), [])
        self.assertTrue(problems(text.replace("  workflow_dispatch:", "  pull_request:")))

    def test_audit_comments_names_and_secret_scans_are_not_dependency_audits(self) -> None:
        text = ("on: pull_request\njobs:\n  a:\n    steps:\n"
                "      - name: bun audit is deferred\n"
                "        run: trufflehog git file://. --only-verified --fail\n"
                "      # run: npm audit\n"
                "      - run: bun run test # bun audit later\n")
        self.assertEqual(problems(text), [])

    def test_audit_gate_uses_the_existing_delivered_exemption(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            text = "on: pull_request\njobs:\n  a:\n    steps:\n      - run: bun audit\n"
            root = repo(tmp, {"ci.yml": text})
            normal = {"GITHUB_EVENT_PATH": self.event_path(tmp, {})}
            self.assertEqual(perf_gate.main(normal, root), 1)
            done = {"GITHUB_EVENT_PATH": self.event_path(tmp, {"sylphx_delivery": "delivered"})}
            self.assertEqual(perf_gate.main(done, root), 0)

    @staticmethod
    def event_path(tmp: str, properties: dict) -> str:
        path = pathlib.Path(tmp, "event.json")
        path.write_text(json.dumps({"repository": {"custom_properties": properties}}))
        return str(path)

    def test_perf_enforce_env_at_every_level(self) -> None:
        text = ("on: merge_group\nenv:\n  PERF_ENFORCE: '1'\njobs:\n  a:\n    env:\n      PERF_ENFORCE: true\n"
                "    steps:\n      - run: echo\n        env:\n          PERF_ENFORCE: ${{ vars.X }}\n")
        found = problems(text)
        self.assertEqual([(n, w) for n, w, _ in found], [(3, "env"), (7, "job a"), (11, "job a")])
        self.assertIn("may only set it to '0'", found[0][2])

    def test_allowed_forms(self) -> None:
        for text in (
            # A post-merge workflow may gate on timing.
            LIGHTHOUSE_PR.replace("  pull_request:\n", ""),
            "on: pull_request\nenv:\n  PERF_ENFORCE: '0'\njobs:\n  a:\n    steps:\n      - run: PERF_ENFORCE=0 bun x\n",
            "on: pull_request\njobs:\n  a:\n    steps:\n      - run: echo  # lighthouse later, after merge\n",
            "on: pull_request\njobs:\n  a:\n    steps:\n      - name: no hyperfine here\n        run: echo\n",
            "on: pull_request\njobs:\n  a:\n    env:\n      PERF_ENFORCE: \"false\"\n",
        ):
            self.assertEqual(problems(text), [], text)

    def test_post_merge_job_in_reusable_workflow_is_allowed(self) -> None:
        condition = ("github.event_name != 'pull_request' && "
                     "github.event_name != 'pull_request_target' && "
                     "github.event_name != 'merge_group' && fromJSON(needs.plan.outputs.run).build")
        for wrapped in (condition, "${{ " + condition + " }}"):
            for trigger in perf_gate.PRE_MERGE:
                with self.subTest(trigger=trigger, wrapped=wrapped):
                    text = (f"on: {trigger}\njobs:\n  lighthouse:\n"
                            f"    if: {wrapped}\n    steps:\n      - run: bun run lighthouse\n"
                            "  verified:\n    needs: [lighthouse]\n    steps:\n"
                            "      - run: echo verified\n")
                    self.assertEqual(problems(text), [])
                    self.assertTrue(problems(text + "      - run: npm audit\n"))

    def test_partial_or_disjunctive_exclusions_are_not_proven(self) -> None:
        safe = ("github.event_name != 'pull_request' && "
                "github.event_name != 'pull_request_target' && github.event_name != 'merge_group'")
        for condition in ("github.event_name != 'pull_request'", safe + " || true",
                          "${{ !(" + safe + ") }}", "true", "${{ inputs.post_merge }}"):
            text = ("on: workflow_call\njobs:\n  lighthouse:\n"
                    f"    if: {condition}\n    steps:\n      - run: bun run lighthouse\n")
            self.assertEqual(len(problems(text)), 1, condition)

    def test_only_executable_fields_are_commands(self) -> None:
        text = ("on: workflow_call\njobs:\n  lighthouse:\n    steps:\n"
                "      - run: |\n          echo report\n"
                "        working-directory: lighthouse\n"
                "      - uses: actions/upload-artifact@v4\n"
                "        with:\n          path: .lighthouseci\n"
                "  verified:\n    needs: [lighthouse]\n    steps:\n"
                "      - run: echo verified\n")
        self.assertEqual(problems(text), [])
        self.assertEqual(len(problems(text.replace("          echo report", "          npm audit"))), 1)

    def test_all_yaml_block_scalar_headers_scan_executable_bodies(self) -> None:
        import yaml

        for style in ("|", ">"):
            for chomp in ("", "-", "+"):
                for indicator in ("", *map(str, range(1, 10))):
                    for suffix in {indicator + chomp, chomp + indicator}:
                        for sequence_run in (False, True):
                            with self.subTest(style=style, suffix=suffix, sequence_run=sequence_run):
                                prefix = "      - run: " if sequence_run else "      - name: report\n        run: "
                                # The scalar is relative to the run key, not the dash.
                                body_indent = 8 + int(indicator or "2")
                                text = ("on: pull_request\njobs:\n  a:\n    steps:\n" + prefix
                                        + style + suffix + " # valid YAML header\n"
                                        + " " * body_indent + "npm audit\n"
                                        + "        working-directory: lighthouse\n"
                                        + "      - run: echo finished\n")
                                parsed = yaml.safe_load(text)
                                self.assertEqual(parsed["jobs"]["a"]["steps"][0]["run"].strip(), "npm audit")
                                found = problems(text)
                                self.assertEqual(len(found), 1)
                                self.assertIn("dependency audit", found[0][2])

    def test_job_exclusions_follow_mapping_depth_not_two_spaces(self) -> None:
        safe = ("github.event_name != 'pull_request' && "
                "github.event_name != 'pull_request_target' && github.event_name != 'merge_group'")
        for job_depth in (2, 4):
            for child_depth in (1, 2, 4):
                for condition_first in (False, True):
                    with self.subTest(job_depth=job_depth, child_depth=child_depth, first=condition_first):
                        j, p = " " * job_depth, " " * (job_depth + child_depth)
                        steps = f"{p}steps:\n{p}  - run: npm audit\n"
                        condition = f"{p}if: {safe}\n"
                        text = (f"on: workflow_call\njobs:\n{j}post_merge:\n"
                                + "# ignore comments when finding property depth\n\n"
                                + (condition + steps if condition_first else steps + condition))
                        self.assertEqual(problems(text), [])
                        self.assertEqual(len(problems(text + f"{j}pre_merge:\n" + steps)), 1)
                        nested = (f"on: workflow_call\njobs:\n{j}a:\n{p}steps:\n"
                                  f"{p}  - if: {safe}\n{p}    run: npm audit\n")
                        self.assertEqual(len(problems(nested)), 1)
                        self.assertEqual(len(problems(text.replace(" && ", " || "))), 1)

    def test_decoded_multiline_commands_cannot_bypass_scanning(self) -> None:
        # Exercise actual scalar semantics, not merely a header/indent match.
        values = (
            "echo preparing &&\n          npx lhci autorun",
            "'echo preparing &&\n          npx lhci autorun'",
            '"echo preparing &&\n          npx lhci autorun"',
            ">-\n          npm\n          audit --prod",
            "'npm\n          audit --prod'",
            '"npm\\x20audit --prod"',
            '"npm\\u0020audit --prod"',
            '"npm \\\n          audit --prod"',
            "|\n          echo preparing\n          npm audit --prod",
        )
        for trigger in perf_gate.PRE_MERGE:
            for value in values:
                with self.subTest(trigger=trigger, value=value):
                    text = (f"on: {trigger}\njobs:\n  a:\n    steps:\n"
                            f"      - run: {value}\n        working-directory: lighthouse\n"
                            "      - run: echo finished\n")
                    found = problems(text)
                    self.assertEqual(len(found), 1)
                    # Findings point to the scalar's physical source line,
                    # even when its command is folded from later lines.
                    self.assertEqual(found[0][0], 5)

    def test_decoded_multiline_job_conditions_exempt_only_safe_jobs(self) -> None:
        terms = [f"github.event_name != '{event}'" for event in perf_gate.PRE_MERGE[:-1]]
        safe = " && ".join(terms)
        folded = " &&\n      ".join(terms)
        for value in (
            ">-\n      " + folded,
            "|\n      " + folded,
            "${{ " + folded + " }}",
            '"' + folded + '"',
            "'" + folded.replace("'", "''") + "'",
            '"' + safe.replace(" && ", "\\x20&&\\x20") + '"',
        ):
            with self.subTest(value=value):
                text = ("on: workflow_call\njobs:\n  post_merge:\n    steps:\n"
                        "      - run: npm audit\n    if: " + value + "\n"
                        "  pre_merge:\n    steps:\n      - run: echo finished\n")
                self.assertEqual(problems(text), [])
                self.assertEqual(len(problems(text.replace("echo finished", "npm audit"))), 1)
                self.assertEqual(len(problems(text.replace("&&", "||"))), 1)
                self.assertEqual(len(problems(text.replace("pull_request_target", "push"))), 1)

    def test_structural_fields_and_decoded_env_values(self) -> None:
        text = ("on: [pull_request, merge_group]\nenv: {PERF_ENFORCE: '0'}\njobs:\n"
                "  a:\n    env: {PERF_ENFORCE: false}\n    steps:\n"
                "      - {run: 'npm audit', name: deferred}\n"
                "      - run: echo done\n        env:\n          PERF_ENFORCE: >-\n            0\n")
        found = problems(text)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][0], 7)
        self.assertEqual(len(problems(text.replace("            0", "            1"))), 2)

    def test_step_condition_does_not_exempt_other_commands(self) -> None:
        safe = ("github.event_name != 'pull_request' && "
                "github.event_name != 'pull_request_target' && github.event_name != 'merge_group'")
        text = ("on: workflow_call\njobs:\n  a:\n    steps:\n"
                f"      - if: {safe}\n        run: echo skipped\n"
                "      - run: npm audit\n")
        self.assertEqual(len(problems(text)), 1)

    def test_this_repository_has_no_timing_gate_before_merge(self) -> None:
        self.assertEqual(perf_gate.scan_dir(ROOT), [])


class DeliveredTest(unittest.TestCase):
    def event(self, tmp: str, repository: dict) -> str:
        path = pathlib.Path(tmp, "event.json")
        path.write_text(json.dumps({"repository": repository}))
        return str(path)

    def test_reads_the_event_payload_first(self) -> None:
        def no_api(*_):
            raise AssertionError("the payload carried the properties")
        with tempfile.TemporaryDirectory() as tmp:
            on = {"GITHUB_EVENT_PATH": self.event(tmp, {"custom_properties": {"sylphx_delivery": "delivered"}})}
            self.assertIs(perf_gate.delivered(on, no_api), True)
            off = {"GITHUB_EVENT_PATH": self.event(tmp, {"custom_properties": {}})}
            self.assertIs(perf_gate.delivered(off, no_api), False)

    def test_falls_back_to_property_values_and_unreadable_is_none(self) -> None:
        env = {"GITHUB_REPOSITORY": "o/r", "GITHUB_TOKEN": "t"}
        calls = []
        ok = lambda repo, token, api: calls.append((repo, api)) or [  # noqa: E731
            {"property_name": "sylphx_delivery", "value": "delivered"}]
        self.assertIs(perf_gate.delivered(env, ok), True)
        self.assertEqual(calls, [("o/r", "https://api.github.com")])
        self.assertIs(perf_gate.delivered(env, lambda *_: []), False)

        def boom(*_):
            raise OSError("403")
        self.assertIsNone(perf_gate.delivered(env, boom))
        self.assertIsNone(perf_gate.delivered({}, ok))

    def test_main_fails_a_normal_repo_and_skips_a_delivered_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = repo(tmp, {"ci.yml": LIGHTHOUSE_PR})
            normal = {"GITHUB_EVENT_PATH": self.event(tmp, {"custom_properties": {"sylphx_delivery": "active"}})}
            self.assertEqual(perf_gate.main(normal, root), 1)
            done = {"GITHUB_EVENT_PATH": self.event(tmp, {"custom_properties": {"sylphx_delivery": "delivered"}})}
            self.assertEqual(perf_gate.main(done, root), 0)
            unreadable = {"GITHUB_ACTIONS": "true"}
            self.assertEqual(perf_gate.main(unreadable, root, lambda *_: []), 0)


class ActionStepTest(unittest.TestCase):
    """The workflow-lint action's own step, run as a consumer runs it."""

    def run_step(self, tmp: str, properties: dict) -> subprocess.CompletedProcess:
        import yaml
        action = yaml.safe_load((ACTION / "action.yml").read_text())
        step = next(s for s in action["runs"]["steps"] if s.get("name") == "No non-deterministic gate before merge")
        root = repo(tmp, {"ci.yml": LIGHTHOUSE_PR})
        event = pathlib.Path(tmp, "event.json")
        event.write_text(json.dumps({"repository": {"custom_properties": properties}}))
        env = dict(os.environ, GITHUB_ACTION_PATH=str(ACTION), GITHUB_EVENT_PATH=str(event),
                   GITHUB_ACTIONS="true", RUNNER_TEMP=tmp)
        return subprocess.run(["bash", "-c", step["run"]], cwd=root, env=env, capture_output=True, text=True)

    def test_a_pull_request_workflow_running_lighthouse_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            done = self.run_step(tmp, {})
            self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
            self.assertIn("::error file=.github/workflows/ci.yml,line=12::", done.stdout)

    def test_a_delivered_repository_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            done = self.run_step(tmp, {"sylphx_delivery": "delivered"})
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertIn("skipped", done.stdout)


if __name__ == "__main__":
    unittest.main()
