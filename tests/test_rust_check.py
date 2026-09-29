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

    def test_rust_cache_only_without_sccache_credentials(self) -> None:
        step = re.search(r"- name: Compile cache on GitHub Actions cache\n((?:        .*\n)+)", self.text)
        self.assertIsNotNone(step)
        body = step.group(1)
        self.assertIn("if: env.AWS_ACCESS_KEY_ID == ''", body)
        self.assertRegex(body, r"uses: Swatinem/rust-cache@[0-9a-f]{40} # v\d+\.\d+\.\d+")
        self.assertIn("cache-on-failure: true", body)
        self.assertLess(self.text.index("Rust toolchain and sccache"), self.text.index("Compile cache on GitHub"))
        self.assertLess(self.text.index("Compile cache on GitHub"), self.text.index("- name: cargo check"))


if __name__ == "__main__":
    unittest.main()
