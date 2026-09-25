#!/usr/bin/env python3
"""Tests for the plain-language action: it flags coined terms on added lines only."""

from __future__ import annotations

import os
import pathlib
import subprocess
import tempfile
import unittest

ACTION = pathlib.Path(__file__).resolve().parents[1] / ".github" / "actions" / "plain-language"


def run_check(before: str, after: str, terms: str = "terms.tsv", *specs: str, mode: str = "fail") -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as tmp:
        def git(*args: str) -> None:
            subprocess.run(["git", *args], cwd=tmp, check=True, capture_output=True)

        git("init", "-q")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "t")
        doc = pathlib.Path(tmp, "doc.md")
        doc.write_text(before)
        git("add", ".")
        git("commit", "-qm", "base")
        doc.write_text(after)
        git("commit", "-qam", "change")
        return subprocess.run(
            [str(ACTION / "check.sh"), "HEAD~1..HEAD", str(ACTION / terms), *specs],
            cwd=tmp, capture_output=True, text=True, env={**os.environ, "PLAIN_LANGUAGE_MODE": mode},
        )


class PlainLanguageTest(unittest.TestCase):
    def test_flags_added_coined_term_with_location_and_replacement(self) -> None:
        result = run_check("intro\n", "intro\nthe residual TS copy\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("file=doc.md,line=2::", result.stdout)
        self.assertIn("legacy code", result.stdout)

    def test_existing_text_does_not_block(self) -> None:
        result = run_check("the residual TS copy\n", "the residual TS copy\nplain line\n")
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_allow_marker_skips_the_line(self) -> None:
        result = run_check("", "quoting residual here plain-language: allow\n")
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_standard_words_pass(self) -> None:
        result = run_check("", "a fencing token, Kueue admission, a database tombstone\n")
        self.assertEqual(result.returncode, 0, result.stdout)


    def test_product_names_list_flags_a_product_in_platform_code(self) -> None:
        result = run_check("", "if project == 'spiron' { grant_extra_quota() }\n", "product-names.tsv")
        self.assertEqual(result.returncode, 1)
        self.assertIn("names a product", result.stdout)

    def test_excluded_paths_are_skipped(self) -> None:
        result = run_check("", "spiron\n", "product-names.tsv", ":(exclude)doc.md")
        self.assertEqual(result.returncode, 0, result.stdout)


    def test_warn_mode_annotates_and_passes(self) -> None:
        result = run_check("", "the residual TS copy\n", mode="warn")
        self.assertEqual(result.returncode, 0)
        self.assertIn("::warning file=doc.md,line=1::", result.stdout)


    def test_lines_merged_in_from_base_are_not_the_branchs_own(self) -> None:
        """A branch that merged its base in is checked against the merge base, as the action computes it."""
        with tempfile.TemporaryDirectory() as tmp:
            def git(*args: str) -> str:
                return subprocess.run(["git", *args], cwd=tmp, check=True, capture_output=True, text=True).stdout.strip()

            git("init", "-q", "-b", "main")
            git("config", "user.email", "t@example.com")
            git("config", "user.name", "t")
            pathlib.Path(tmp, "a.md").write_text("start\n")
            git("add", ".")
            git("commit", "-qm", "base")
            stale_base = git("rev-parse", "HEAD")
            git("checkout", "-qb", "feature")
            pathlib.Path(tmp, "b.md").write_text("clean feature line\n")
            git("add", ".")
            git("commit", "-qm", "feature")
            git("checkout", "-q", "main")
            pathlib.Path(tmp, "c.md").write_text("main added spiron here\n")
            git("add", ".")
            git("commit", "-qm", "main moves")
            git("checkout", "-q", "feature")
            git("merge", "-q", "--no-edit", "main")
            head = git("rev-parse", "HEAD")

            def check(rng: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [str(ACTION / "check.sh"), rng, str(ACTION / "product-names.tsv")],
                    cwd=tmp, capture_output=True, text=True,
                    env={**os.environ, "PLAIN_LANGUAGE_MODE": "fail"},
                )

            # The event's stale base sha sees main's line as the branch's own.
            self.assertEqual(check(f"{stale_base}..{head}").returncode, 1)
            # The merge base with the current base branch does not.
            merge_base = git("merge-base", "main", head)
            result = check(f"{merge_base}..{head}")
            self.assertEqual(result.returncode, 0, result.stdout)


if __name__ == "__main__":
    unittest.main()
