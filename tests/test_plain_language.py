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


    def test_retired_platform_name_is_flagged(self) -> None:
        result = run_check("", "runs on Sylphx Platform via api.identity.sylphx.com\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Sylphx Cloud", result.stdout)
        self.assertIn("api.sylphx.com", result.stdout)


def run_tree(files: dict[str, str], baseline: str | None, mode: str = "fail", env: dict[str, str] | None = None, *specs: str) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["git", "init", "-q"], cwd=tmp, check=True, capture_output=True)
        for name, text in files.items():
            pathlib.Path(tmp, name).parent.mkdir(parents=True, exist_ok=True)
            pathlib.Path(tmp, name).write_text(text)
        extra = {}
        if baseline is not None:
            pathlib.Path(tmp, "base.txt").write_text(baseline)
            extra["PLAIN_LANGUAGE_BASELINE"] = "base.txt"
        subprocess.run(["git", "add", "."], cwd=tmp, check=True, capture_output=True)
        return subprocess.run(
            [str(ACTION / "check.sh"), "HEAD", str(ACTION / "product-names.tsv"), *specs],
            cwd=tmp, capture_output=True, text=True,
            env={**os.environ, "PLAIN_LANGUAGE_MODE": mode, "PLAIN_LANGUAGE_TODAY": "2026-10-01", **extra, **(env or {})},
        )


class ProductNameGuardV2Test(unittest.TestCase):
    def test_underscore_and_dash_are_boundaries(self) -> None:
        for line in ("KALKAS_NAMESPACE=x", "HKMJ_KEEL_OFFICIAL_LABEL", "kalkas_ksvc", "pg-dearhouse-db-1", "com.cubeage.fmj16.app", "big2tw-api"[:4] + "_x", "lavapot-1292"):
            result = run_check("", line + "\n", "product-names.tsv")
            self.assertEqual(result.returncode, 1, line)

    def test_names_inside_longer_words_pass(self) -> None:
        for line in ("a spironic tale\n", "tycoon and mahjong are words\n", "a mahjongg tile\n", "the keeled hull\n"):
            result = run_check("", line, "product-names.tsv")
            self.assertEqual(result.returncode, 0, result.stdout)

    def test_new_portfolio_names_and_product_tokens_are_flagged(self) -> None:
        for line in ("tachyn", "gpdt", "opencity", "worldreign", "tenkind", "bloomkin", "number-grove", "twmj", "fun-mahjong", "Mahjong Tycoon TW", "tycoon-engine", "Big2 Tycoon", "Harbour Sort"):
            result = run_check("", f"x {line} y\n", "product-names.tsv")
            self.assertEqual(result.returncode, 1, line)

    def test_allow_marker_with_reason_and_future_date_passes(self) -> None:
        env = {"PLAIN_LANGUAGE_TODAY": "2026-10-01"}
        os.environ.update(env)
        try:
            ok = run_check("", "spiron plain-language: allow(word list; until=2026-12-31)\n", "product-names.tsv")
            self.assertEqual(ok.returncode, 0, ok.stdout)
            expired = run_check("", "spiron plain-language: allow(old; until=2026-09-30)\n", "product-names.tsv")
            self.assertEqual(expired.returncode, 1)
            self.assertIn("expired", expired.stdout)
            no_reason = run_check("", "spiron plain-language: allow(; until=2026-12-31)\n", "product-names.tsv")
            self.assertEqual(no_reason.returncode, 1)
            no_date = run_check("", "spiron plain-language: allow(because)\n", "product-names.tsv")
            self.assertEqual(no_date.returncode, 1)
        finally:
            os.environ.pop("PLAIN_LANGUAGE_TODAY")

    def test_bare_allow_has_a_grace_period_then_fails(self) -> None:
        os.environ["PLAIN_LANGUAGE_TODAY"] = "2026-10-15"
        try:
            grace = run_check("", "spiron plain-language: allow\n", "product-names.tsv")
            self.assertEqual(grace.returncode, 0, grace.stdout)
            self.assertIn("::warning", grace.stdout)
            os.environ["PLAIN_LANGUAGE_TODAY"] = "2026-10-16"
            late = run_check("", "spiron plain-language: allow\n", "product-names.tsv")
            self.assertEqual(late.returncode, 1)
            self.assertIn("no longer accepted", late.stdout)
        finally:
            os.environ.pop("PLAIN_LANGUAGE_TODAY")

    def test_ratchet_passes_at_baseline_and_fails_on_a_new_hit(self) -> None:
        base = "a.md\tspiron\t2\n"
        self.assertEqual(run_tree({"a.md": "spiron\nx spiron\n"}, base).returncode, 0)
        grown = run_tree({"a.md": "spiron\nspiron\nspiron\n"}, base)
        self.assertEqual(grown.returncode, 1)
        self.assertIn("baseline allows 2", grown.stdout)
        new_file = run_tree({"a.md": "spiron\nspiron\n", "b.md": "kalkas\n"}, base)
        self.assertEqual(new_file.returncode, 1)
        self.assertIn("file=b.md", new_file.stdout)

    def test_ratchet_reads_every_line_of_a_multi_entry_baseline(self) -> None:
        base = "# header\na.md\tspiron\t1\nb.md\tkalkas\t1\n"
        result = run_tree({"a.md": "spiron\n", "b.md": "kalkas\n"}, base)
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_ratchet_requires_removed_hits_to_leave_the_baseline(self) -> None:
        stale = run_tree({"a.md": "spiron\n"}, "a.md\tspiron\t2\n")
        self.assertEqual(stale.returncode, 1)
        self.assertIn("shrink the baseline", stale.stdout)
        gone = run_tree({"a.md": "clean\n"}, "a.md\tspiron\t1\n")
        self.assertEqual(gone.returncode, 1)

    def test_ratchet_skips_excluded_paths_and_allowed_lines(self) -> None:
        result = run_tree({"a.md": "spiron plain-language: allow(word list; until=2026-12-31)\n", "t/x.md": "spiron\n"}, "", "fail", None, ":(exclude)t/**")
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_write_baseline_init_and_shrink(self) -> None:
        init = run_tree({"a.md": "spiron\nspiron\n"}, "", env={"PLAIN_LANGUAGE_WRITE_BASELINE": "init"})
        self.assertEqual(init.returncode, 0)
        self.assertIn("a.md\tspiron\t2", init.stdout)
        shrink = run_tree({"a.md": "spiron\n"}, "a.md\tspiron\t2\n", env={"PLAIN_LANGUAGE_WRITE_BASELINE": "shrink"})
        self.assertIn("a.md\tspiron\t1", shrink.stdout)
        grows = run_tree({"a.md": "spiron\nspiron\nspiron\n"}, "a.md\tspiron\t2\n", env={"PLAIN_LANGUAGE_WRITE_BASELINE": "shrink"})
        self.assertEqual(grows.returncode, 1)


if __name__ == "__main__":
    unittest.main()
