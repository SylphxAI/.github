#!/usr/bin/env python3
"""Tests for the plain-language action: it flags coined terms on added lines only."""

from __future__ import annotations

import pathlib
import subprocess
import tempfile
import unittest

ACTION = pathlib.Path(__file__).resolve().parents[1] / ".github" / "actions" / "plain-language"


def run_check(before: str, after: str) -> subprocess.CompletedProcess[str]:
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
            [str(ACTION / "check.sh"), "HEAD~1..HEAD", str(ACTION / "terms.tsv")],
            cwd=tmp, capture_output=True, text=True,
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


if __name__ == "__main__":
    unittest.main()
