#!/usr/bin/env python3
"""Tests for the ci-range action (range and lane selection) and the needs-pass verdict."""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ci_range = load("ci_range", ROOT / ".github" / "actions" / "ci-range" / "ci_range.py")
needs_pass = load("needs_pass", ROOT / ".github" / "actions" / "needs-pass" / "needs_pass.py")

LANES = ci_range.parse_lanes("""
lint:
test: src/* tests/*   # comment
e2e: web/*
""")


class ParseLanesTest(unittest.TestCase):
    def test_parses_names_and_patterns(self) -> None:
        self.assertEqual(LANES, {"lint": [], "test": ["src/*", "tests/*"], "e2e": ["web/*"]})

    def test_rejects_bad_or_duplicate_names_and_empty(self) -> None:
        for text in ("bad name: x", "a:\na:", "", "# only a comment"):
            with self.assertRaises(ValueError, msg=text):
                ci_range.parse_lanes(text)


class SelectTest(unittest.TestCase):
    def test_no_base_runs_everything(self) -> None:
        self.assertEqual(ci_range.select(LANES, None), {"lint": True, "test": True, "e2e": True})

    def test_patterns_cross_directories(self) -> None:
        self.assertEqual(ci_range.select(LANES, ["src/deep/a.rs"]), {"lint": True, "test": True, "e2e": False})
        self.assertEqual(ci_range.select(LANES, ["README.md"]), {"lint": True, "test": False, "e2e": False})

    def test_workflow_change_runs_everything(self) -> None:
        self.assertTrue(all(ci_range.select(LANES, [".github/workflows/ci.yml"]).values()))

    def test_star_always_runs(self) -> None:
        self.assertEqual(ci_range.select({"a": ["*"], "b": ["x/*"]}, []), {"a": True, "b": False})

    def test_only_narrows_by_job_name(self) -> None:
        run = ci_range.select(LANES, None, "verify / test (linux),verified")
        self.assertEqual(run, {"lint": False, "test": True, "e2e": False})

    def test_only_runs_a_named_lane_outside_the_range(self) -> None:
        # A dispatch naming lanes on an already verified head (empty range)
        # runs those lanes and nothing else.
        self.assertEqual(ci_range.select(LANES, [], "lint,e2e"), {"lint": True, "test": False, "e2e": True})
        self.assertEqual(ci_range.select(LANES, ["README.md"], "e2e")["e2e"], True)

    def test_only_drops_a_selected_lane_it_does_not_name(self) -> None:
        self.assertEqual(ci_range.select(LANES, ["src/a.rs", "web/b.ts"], "e2e"), {"lint": False, "test": False, "e2e": True})

    def test_only_with_no_known_lane_runs_the_selection(self) -> None:
        self.assertEqual(ci_range.select(LANES, None, "verified,plan"), {"lint": True, "test": True, "e2e": True})


class BaseTest(unittest.TestCase):
    def test_pull_request_and_merge_group(self) -> None:
        self.assertEqual(ci_range.base_for("pull_request", {"pull_request": {"base": {"sha": "a1"}}}, "o/r", "ci.yml", "main"), "a1")
        self.assertEqual(ci_range.base_for("merge_group", {"merge_group": {"base_sha": "b2"}}, "o/r", "ci.yml", "main"), "b2")

    def test_schedule_has_no_base(self) -> None:
        self.assertEqual(ci_range.base_for("schedule", {}, "o/r", "verify.yml", "main"), "")


class MainTest(unittest.TestCase):
    """End to end on a real repository, for the events that need no API."""

    def run_in_repo(self, event: str, payload_for) -> dict[str, str]:
        with tempfile.TemporaryDirectory() as tmp:
            git = lambda *a: subprocess.run(["git", "-C", tmp, *a], check=True, capture_output=True, text=True).stdout.strip()
            git("init", "-q", "-b", "main")
            git("config", "user.email", "t@example.com")
            git("config", "user.name", "t")
            pathlib.Path(tmp, "README.md").write_text("x")
            git("add", "-A")
            git("commit", "-q", "-m", "base")
            base = git("rev-parse", "HEAD")
            pathlib.Path(tmp, "src").mkdir()
            pathlib.Path(tmp, "src", "lib.rs").write_text("y")
            git("add", "-A")
            git("commit", "-q", "-m", "change")
            event_path = pathlib.Path(tmp, "..", f"event-{os.getpid()}.json").resolve()
            event_path.write_text(json.dumps(payload_for(base)))
            out = pathlib.Path(tmp, "..", f"out-{os.getpid()}").resolve()
            env = dict(os.environ, LANES="lint:\ntest: src/*\ne2e: web/*", GITHUB_EVENT_NAME=event,
                       GITHUB_EVENT_PATH=str(event_path), GITHUB_OUTPUT=str(out), GITHUB_REPOSITORY="o/r",
                       GITHUB_WORKFLOW_REF="o/r/.github/workflows/ci.yml@refs/heads/main", TOKEN="x")
            env.pop("GITHUB_STEP_SUMMARY", None)
            try:
                subprocess.run(["python3", str(ROOT / ".github/actions/ci-range/ci_range.py")], cwd=tmp, env=env,
                               check=True, capture_output=True, text=True)
                return dict(line.split("=", 1) for line in out.read_text().splitlines()), base
            finally:
                event_path.unlink(missing_ok=True)
                out.unlink(missing_ok=True)

    def test_merge_group_selects_by_the_diff(self) -> None:
        outputs, base = self.run_in_repo("merge_group", lambda b: {"merge_group": {"base_sha": b}})
        self.assertEqual(outputs["base"], base)
        self.assertEqual(json.loads(outputs["run"]), {"lint": True, "test": True, "e2e": False})
        self.assertEqual(outputs["changed"], "1")

    def test_schedule_runs_everything(self) -> None:
        outputs, _ = self.run_in_repo("schedule", lambda b: {})
        self.assertEqual((outputs["base"], outputs["changed"]), ("", ""))
        self.assertTrue(all(json.loads(outputs["run"]).values()))


class NeedsPassTest(unittest.TestCase):
    def test_pr_time_job_skipped_fails_outside_merge_group(self) -> None:
        needs = {"plan": {"result": "success"}, "suite": {"result": "skipped"}}
        for event in ("pull_request", "workflow_dispatch", "push", ""):
            self.assertEqual(needs_pass.verdict(needs, {"plan"}, set(), event, {"suite"}),
                             ["suite: skipped (must succeed)"], event)

    def test_pr_time_job_skipped_passes_in_merge_group(self) -> None:
        needs = {"plan": {"result": "success"}, "suite": {"result": "skipped"}}
        self.assertEqual(needs_pass.verdict(needs, {"plan"}, set(), "merge_group", {"suite"}), [])

    def test_pr_time_job_success_and_failure(self) -> None:
        ok = {"plan": {"result": "success"}, "suite": {"result": "success"}}
        self.assertEqual(needs_pass.verdict(ok, {"plan"}, set(), "workflow_dispatch", {"suite"}), [])
        bad = {"plan": {"result": "success"}, "suite": {"result": "failure"}}
        self.assertTrue(needs_pass.verdict(bad, {"plan"}, set(), "merge_group", {"suite"}))

    def test_success_and_skipped_pass(self) -> None:
        needs = {"plan": {"result": "success"}, "a": {"result": "skipped"}, "b": {"result": "success"}}
        self.assertEqual(needs_pass.verdict(needs, {"plan"}, set()), [])

    def test_failure_cancel_fail(self) -> None:
        for bad in ("failure", "cancelled", ""):
            self.assertTrue(needs_pass.verdict({"a": {"result": bad}}, set(), set()), bad)

    def test_required_must_succeed(self) -> None:
        self.assertEqual(needs_pass.verdict({"plan": {"result": "skipped"}}, {"plan"}, set()),
                         ["plan: skipped (must succeed)"])
        self.assertEqual(needs_pass.verdict({}, {"plan"}, set()), ["plan: required but not in needs"])

    def test_advisory_never_fails(self) -> None:
        self.assertEqual(needs_pass.verdict({"quarantine": {"result": "failure"}}, set(), {"quarantine"}), [])


class ActionManifestExpressionTest(unittest.TestCase):
    """The runner evaluates `${{ }}` anywhere in a composite action.yml, even in
    an input's description, and a context the action cannot see (needs) fails
    the step before it starts. Expressions belong only in defaults and steps."""

    def test_no_expression_in_any_description(self) -> None:
        import yaml
        for manifest in (ROOT / ".github" / "actions").glob("*/action.yml"):
            data = yaml.safe_load(manifest.read_text())
            texts = [data.get("description", "")]
            for section in ("inputs", "outputs"):
                texts += [(v or {}).get("description", "") for v in (data.get(section) or {}).values()]
            for text in texts:
                self.assertNotIn("${{", text or "", manifest)


class WorkflowLintArgumentsTest(unittest.TestCase):
    """workflow-lint passes each input line as one argument: an -ignore pattern
    with spaces and quotes reaches actionlint intact (cubeage-platform#857)."""

    def test_patterns_and_args_are_never_word_split(self) -> None:
        import yaml
        action = yaml.safe_load((ROOT / ".github" / "actions" / "workflow-lint" / "action.yml").read_text())
        step = action["runs"]["steps"][0]
        with tempfile.TemporaryDirectory() as tmp:
            fake = pathlib.Path(tmp, "actionlint")
            fake.write_text('#!/usr/bin/env bash\n'
                            'if [ "${1:-}" = -version ]; then echo "$VERSION"; exit 0; fi\n'
                            'for a in "$@"; do printf "%s\\n" "$a"; done > "$OUT"\n')
            fake.chmod(0o755)
            out = pathlib.Path(tmp, "argv")
            env = dict(os.environ, PATH=f"{tmp}:{os.environ['PATH']}", RUNNER_TEMP=tmp, OUT=str(out),
                       VERSION=step["env"]["VERSION"], SHA256=step["env"]["SHA256"],
                       IGNORE='label ".+" is unknown\n\nproperty "x y" is not defined\n',
                       ARGS="-config-file\n.github/actionlint.yaml\n")
            subprocess.run(["bash", "-c", step["run"]], env=env, check=True)
            self.assertEqual(out.read_text().splitlines(), [
                "-shellcheck=", "-pyflakes=",
                "-ignore", 'label ".+" is unknown',
                "-ignore", 'property "x y" is not defined',
                "-config-file", ".github/actionlint.yaml"])


class StarterWorkflowTest(unittest.TestCase):
    def test_starters_parse_and_reference_existing_actions(self) -> None:
        import yaml
        for path in (ROOT / "workflow-templates").glob("*.yml"):
            text = path.read_text()
            self.assertIsInstance(yaml.safe_load(text), dict, path)
            self.assertTrue((path.with_suffix(".properties.json")).exists(), path)
            for line in text.splitlines():
                if "uses: SylphxAI/.github/.github/actions/" in line:
                    name = line.split("/actions/", 1)[1].split("@", 1)[0]
                    self.assertTrue((ROOT / ".github" / "actions" / name / "action.yml").exists(), line)

    def test_verify_starter_aggregate_is_named_verified(self) -> None:
        import yaml
        verify = yaml.safe_load((ROOT / "workflow-templates" / "optimistic-verify.yml").read_text())
        self.assertEqual(verify["jobs"]["verified"]["name"], "verified")
        gate = yaml.safe_load((ROOT / "workflow-templates" / "optimistic-gate.yml").read_text())
        self.assertIn("ci-ok", gate["jobs"])
        # A bot-opened pull request gets its ci-ok from a dispatched run.
        self.assertIn("workflow_dispatch", gate[True])
        self.assertIn("pull_request", gate["jobs"]["suite"]["if"])

    def test_verify_starter_hand_over_keeps_a_bounded_size(self) -> None:
        # docs/optimistic-merge.md: a heavy hand-over between jobs of a run
        # uses `run-store` (the org's BuildCache), never GitHub artifact
        # storage, which is a metered organization quota ("Artifact storage
        # quota has been hit", fb#4348). A raw platform bundle left in the
        # starter is copied into a caller and uploaded there.
        import yaml
        text = (ROOT / "workflow-templates" / "optimistic-verify.yml").read_text()
        raw = sum(path.stat().st_size for path in (ROOT / "workflow-templates").glob("*.bundle"))
        self.assertLess(raw, 4 * 1024, "a raw bundle in workflow-templates")
        verify = yaml.safe_load(text)
        for job_name, job in (verify.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                if "path" in (step.get("with") or {}):
                    self.assertNotIn("*", str(step["with"]["path"]), f"{job_name} {step.get('name')}")
        guide = (ROOT / "docs" / "optimistic-merge.md").read_text()
        self.assertIn("never `upload-artifact`", guide)
        self.assertIn("complete\n   set of steps", guide)

    def test_starter_artifact_uploads_never_fail_a_job(self) -> None:
        # Artifact storage is an organization quota; once it is spent every
        # upload fails ("Artifact storage quota has been hit") and a passing
        # suite went red (docs/run-store.md). A starter's upload is a
        # diagnostic, so it may not fail its job and keeps a short retention.
        import yaml
        seen = 0
        for path in (ROOT / "workflow-templates").glob("*.yml"):
            workflow = yaml.safe_load(path.read_text())
            for job_name, job in (workflow.get("jobs") or {}).items():
                for step in job.get("steps") or []:
                    if not str(step.get("uses", "")).startswith("actions/upload-artifact@"):
                        continue
                    seen += 1
                    where = f"{path.name} {job_name} {step.get('with', {}).get('name')}"
                    self.assertIs(step.get("continue-on-error"), True, where)
                    retention = (step.get("with") or {}).get("retention-days")
                    self.assertIsInstance(retention, int, where)
                    self.assertLessEqual(retention, 3, where)
        self.assertGreater(seen, 0)


class RedMainOnlyOnFailureTest(unittest.TestCase):
    """A cancelled, superseded or timed-out run is never a red trunk: only a
    completed `failure` of the verify workflow on the trunk wakes the handler."""

    def test_caller_fires_only_on_a_failed_trunk_run(self) -> None:
        import yaml
        caller = yaml.safe_load((ROOT / "workflow-templates" / "red-main.yml").read_text())
        condition = " ".join(caller["jobs"]["red-main"]["if"].split())
        self.assertIn("github.event.workflow_run.conclusion == 'failure'", condition)
        self.assertIn("github.event.workflow_run.head_branch == github.event.repository.default_branch", condition)
        self.assertEqual(caller[True]["workflow_run"]["workflows"], ["Verify"])  # yaml reads `on` as True

    def test_handler_quiets_only_authenticated_success_or_ineligible_producer(self) -> None:
        handler = (ROOT / ".github" / "workflows" / "red-main.yml").read_text()
        self.assertIn('case "$proof" in', handler)
        self.assertIn('failure|timed_out|action_required|startup_failure)', handler)
        self.assertIn('success|ineligible) quiet "the authenticated post-main proof', handler)
        self.assertIn('proof=$(read_proof run', handler)
        # Manual event selection cannot take a diagnostic or merge-group run;
        # whole-workflow failure is not proof (optional publishing may fail).
        self.assertIn("runs?event=push&branch=$TRUNK", handler)
        self.assertNotIn("runs?branch=main&status=failure", handler)


if __name__ == "__main__":
    unittest.main()
