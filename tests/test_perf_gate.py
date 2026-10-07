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
        step = next(s for s in action["runs"]["steps"] if s.get("name") == "No timing gate before merge")
        root = repo(tmp, {"ci.yml": LIGHTHOUSE_PR})
        event = pathlib.Path(tmp, "event.json")
        event.write_text(json.dumps({"repository": {"custom_properties": properties}}))
        env = dict(os.environ, GITHUB_ACTION_PATH=str(ACTION), GITHUB_EVENT_PATH=str(event),
                   GITHUB_ACTIONS="true")
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
