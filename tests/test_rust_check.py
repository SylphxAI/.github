#!/usr/bin/env python3
"""The rust-check reusable workflow keeps caller inputs out of shell bodies."""

from __future__ import annotations

import pathlib
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
