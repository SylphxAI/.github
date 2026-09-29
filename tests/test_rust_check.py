#!/usr/bin/env python3
"""The rust-check reusable workflow keeps caller inputs out of shell bodies."""

from __future__ import annotations

import pathlib
import subprocess
import re
import unittest

WORKFLOW = pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows" / "rust-check.yml"


class RustCheckWorkflow(unittest.TestCase):
    def setUp(self) -> None:
        self.text = WORKFLOW.read_text()

    def test_inputs_are_not_spliced_into_run_bodies(self) -> None:
        for block in re.findall(r"run: \|\n((?:          .*\n|\n)+)", self.text):
            self.assertNotIn("${{", block)

    def test_runs_on_the_ready_pr_class(self) -> None:
        self.assertEqual(re.findall(r"^\s*runs-on: (\S+)$", self.text, re.M), ["sylphx-linux-standard"])

    def test_emits_the_markers_the_cli_reads(self) -> None:
        for marker in ("@@sylphx-check-begin", "@@sylphx-check-end", "@@sylphx-check-exit="):
            self.assertIn(f'echo "{marker}', self.text)

    def test_sccache_through_the_action_then_rust_cache(self) -> None:
        sccache = re.search(r"- name: Compile cache \(sccache\)\n((?:        .*\n)+)", self.text)
        self.assertIsNotNone(sccache)
        self.assertIn("uses: SylphxAI/.github/.github/actions/rust-sccache@17838ee1f421b91e26c3c16fc0370568bcdb6363", sccache.group(1))
        self.assertIn("actions-cache: 'false'", sccache.group(1))
        self.assertIn("secrets.SYLPHX_CI_CACHE_ACCESS_KEY", sccache.group(1))
        step = re.search(r"- name: Compile cache on GitHub Actions cache\n((?:        .*\n)+)", self.text)
        self.assertIsNotNone(step)
        body = step.group(1)
        self.assertIn(
            "if: steps.sccache.outputs.backend != 'org' && steps.sccache.outputs.backend != 'static'", body
        )
        self.assertRegex(body, r"uses: Swatinem/rust-cache@[0-9a-f]{40} # v\d+\.\d+\.\d+")
        self.assertIn("cache-on-failure: true", body)
        self.assertLess(self.text.index("- name: Rust toolchain"), self.text.index("Compile cache (sccache)"))
        self.assertLess(self.text.index("Compile cache (sccache)"), self.text.index("Compile cache on GitHub"))
        self.assertLess(self.text.index("Compile cache on GitHub"), self.text.index("- name: cargo check"))

    def test_never_uses_the_platform_ci_sccache_key(self) -> None:
        job = "\n".join(
            line for line in self.text[self.text.index("jobs:"):].splitlines() if not line.strip().startswith("#")
        )
        self.assertNotIn("RGW_S3", job)
        self.assertNotIn("ci-sccache", job)
        self.assertNotIn("AWS_ACCESS_KEY_ID", job)


if __name__ == "__main__":
    unittest.main()


class ToolchainSelectionTest(unittest.TestCase):
    """The toolchain step honours a rust-toolchain file and falls back to stable
    (a tenant repository without one failed: "no default is configured")."""

    def run_step(self, with_file: bool) -> list[str]:
        import os, subprocess, tempfile, yaml
        job = yaml.safe_load(WORKFLOW.read_text())["jobs"]["check"]
        script = next(s["run"] for s in job["steps"] if s.get("name") == "Rust toolchain")
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp, "repo"); (root / "ws").mkdir(parents=True)
            if with_file:
                (root / "rust-toolchain.toml").write_text('[toolchain]\nchannel = "1.90.0"\n')
            fake = pathlib.Path(tmp, "bin"); fake.mkdir()
            log = pathlib.Path(tmp, "log")
            (fake / "curl").write_text("#!/bin/sh\necho 'exit 0'\n")
            (fake / "rustup").write_text(f'#!/bin/sh\necho "$*" >> {log}\n[ "$1 $2" = "show active-toolchain" ] && echo "1.90.0-x86_64 (overridden)"\nexit 0\n')
            (fake / "cargo").write_text("#!/bin/sh\necho cargo\n")
            for f in fake.iterdir(): f.chmod(0o755)
            env = dict(os.environ, PATH=f"{fake}:{os.environ['PATH']}", RUNNER_TEMP=tmp,
                       GITHUB_ENV=str(pathlib.Path(tmp, "env")), GITHUB_PATH=str(pathlib.Path(tmp, "path")),
                       GITHUB_WORKSPACE=str(root), IN_WORKSPACE="ws")
            subprocess.run(["bash", "-c", script], cwd=root, env=env, check=True, capture_output=True)
            return log.read_text().splitlines()

    def test_pinned_file_is_installed(self) -> None:
        calls = self.run_step(with_file=True)
        self.assertIn("toolchain install", calls)
        self.assertIn("default 1.90.0-x86_64", calls)
        self.assertNotIn("default stable", calls)

    def test_no_file_falls_back_to_stable(self) -> None:
        calls = self.run_step(with_file=False)
        self.assertIn("default stable", calls)
        self.assertNotIn("toolchain install", calls)
        self.assertIn("component add clippy", calls)


class GitDepsTest(unittest.TestCase):
    """Private git dependencies: refused without the caller's App, and a line
    that is not OWNER/REPO never reaches git or the API."""

    def run_step(self, deps: str, app: bool) -> subprocess.CompletedProcess:
        import os, yaml
        job = yaml.safe_load(WORKFLOW.read_text())["jobs"]["check"]
        step = next(s for s in job["steps"] if s.get("name") == "Private git dependencies")
        env = dict(os.environ, GIT_DEPS=deps, APP_ID="1" if app else "", APP_KEY="k" if app else "",
                   GITHUB_API_URL="http://127.0.0.1:9", GITHUB_ENV="/dev/null", HOME="/nonexistent")
        return subprocess.run(["bash", "-c", step["run"]], env=env, capture_output=True, text=True)

    def test_needs_the_callers_app(self) -> None:
        out = self.run_step("SylphxAI/keel", app=False)
        self.assertEqual(out.returncode, 2)
        self.assertIn("GIT_DEPS_APP_ID", out.stdout)

    def test_rejects_a_line_that_is_not_owner_repo(self) -> None:
        for bad in ("SylphxAI/keel;rm -rf /", "https://github.com/x/y", "a/b/c"):
            out = self.run_step(bad, app=True)
            self.assertEqual(out.returncode, 2, bad)
            self.assertIn("bad git-deps line", out.stdout)
