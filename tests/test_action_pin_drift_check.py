#!/usr/bin/env python3
"""Tests for the shared-action pin drift check (scripts/action_pin_drift_check.py).

GitHub is a fake that answers the commit reads: .github main's head and one committer date per pinned
commit. Dates are fixed so "days behind" is deterministic.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("action_pin_drift_check", ROOT / "scripts" / "action_pin_drift_check.py")
drift = importlib.util.module_from_spec(SPEC)
sys.modules["action_pin_drift_check"] = drift
SPEC.loader.exec_module(drift)

HEAD = "a" * 40          # .github main head
FRESH = "b" * 40         # 1 day behind
AT_LIMIT = "c" * 40      # exactly DRIFT_DAYS behind
BEHIND = "d" * 40        # DRIFT_DAYS + 1 behind
GONE = "e" * 40          # unreadable


def workflow(*uses: str) -> str:
    steps = "".join(f"      - uses: {u}\n" for u in uses)
    return f"name: CI\non: [pull_request]\njobs:\n  a:\n    runs-on: sylphx-linux-standard\n    steps:\n{steps}"


class FakeGh:
    HEAD_DATE = "2026-10-15T00:00:00Z"

    def __init__(self):
        self.calls = []
        self.dates = {HEAD: self.HEAD_DATE,
                      FRESH: "2026-10-14T00:00:00Z",
                      AT_LIMIT: f"2026-10-{15 - drift.DRIFT_DAYS:02d}T00:00:00Z",
                      BEHIND: f"2026-10-{15 - drift.DRIFT_DAYS - 1:02d}T00:00:00Z"}

    def rest(self, path):
        self.calls.append(path)
        if path == f"repos/{drift.SHARED_REPO}/commits/main":
            return {"sha": HEAD, "commit": {"committer": {"date": self.HEAD_DATE}}}
        sha = path.rsplit("/", 1)[1]
        if sha == GONE:
            raise RuntimeError("Not Found (HTTP 404)")
        return {"sha": sha, "commit": {"committer": {"date": self.dates[sha]}}}


def judge(files, gh=None, days=drift.DRIFT_DAYS):
    gh = gh or FakeGh()
    commits = drift.Commits(gh)
    head_sha, head_date = commits.main_head(drift.SHARED_REPO)
    return drift.judge(files, commits, head_sha, head_date, days), gh


class FindPins(unittest.TestCase):
    def test_only_full_sha_github_pins(self):
        text = workflow(
            f"SylphxAI/.github/.github/actions/needs-pass@{BEHIND}",
            f"SylphxAI/.github/.github/workflows/rust-ci.yml@{FRESH}",
            "SylphxAI/.github/.github/actions/needs-pass@main",  # a branch, not judged here
            "actions/checkout@" + "f" * 40,
            "./.github/actions/local")
        found = drift.find_pins(text)
        self.assertEqual([(p["path"], p["sha"][0]) for p in found],
                         [(".github/actions/needs-pass", "d"), (".github/workflows/rust-ci.yml", "b")])
        self.assertEqual([p["line"] for p in found], [7, 8])

    def test_a_comment_is_not_a_pin(self):
        self.assertEqual(drift.find_pins(f"# uses: SylphxAI/.github/.github/actions/needs-pass@{BEHIND}\n"), [])


class Judge(unittest.TestCase):
    def test_pin_past_the_limit_fails_with_file_line_and_days(self):
        found, _ = judge({".github/workflows/ci.yml": workflow(f"SylphxAI/.github/.github/actions/needs-pass@{BEHIND}")})
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0]["file"], found[0]["line"]), (".github/workflows/ci.yml", 7))
        self.assertEqual(found[0]["days"], drift.DRIFT_DAYS + 1)
        self.assertIn("days behind main", found[0]["why"])
        self.assertIn("ci.yml:7:", drift.render_offenders(found))

    def test_fresh_and_at_the_limit_pass(self):
        found, _ = judge({"a.yml": workflow(f"SylphxAI/.github/.github/actions/x@{FRESH}",
                                            f"SylphxAI/.github/.github/actions/y@{AT_LIMIT}")})
        self.assertEqual(found, [])

    def test_the_head_commit_itself_is_never_behind(self):
        # A pin already at .github main is current, whatever its date.
        found, gh = judge({"a.yml": workflow(f"SylphxAI/.github/.github/actions/x@{HEAD}")})
        self.assertEqual(found, [])
        self.assertFalse(any(c.endswith(HEAD) for c in gh.calls if "commits/main" not in c))

    def test_unknown_commit_fails(self):
        found, _ = judge({"a.yml": workflow(f"SylphxAI/.github/.github/actions/x@{GONE}")})
        self.assertEqual(len(found), 1)
        self.assertIn("could not be read", found[0]["why"])

    def test_one_read_per_distinct_pin(self):
        text = workflow(f"SylphxAI/.github/.github/actions/x@{FRESH}",
                        f"SylphxAI/.github/.github/actions/x@{FRESH}")
        _, gh = judge({"a.yml": text, "b.yml": text})
        self.assertEqual(sum(1 for c in gh.calls if c.endswith(FRESH)), 1)

    def test_days_argument_widens_the_window(self):
        found, _ = judge({"a.yml": workflow(f"SylphxAI/.github/.github/actions/x@{BEHIND}")},
                         days=drift.DRIFT_DAYS + 5)
        self.assertEqual(found, [])


class Watcher(unittest.TestCase):
    """A red trunk must draw a non-skipped red-main run within the window."""

    class FakeRuns:
        def __init__(self, rows):
            self.rows, self.calls = rows, []

        def rest(self, path):
            self.calls.append(path)
            return {"workflow_runs": self.rows}

    def run_(self, verify, red, trunk="main", window=30):
        return drift.watcher_offenders(self.FakeRuns(verify + red), "o/r", trunk, window)

    def test_failed_trunk_verify_with_only_skipped_red_main_fails(self):
        verify = [{"id": 1, "name": "Verify", "conclusion": "failure", "head_branch": "main",
                   "created_at": "2026-10-14T01:52:43Z"}]
        red = [{"id": 2, "name": "Red main", "conclusion": "skipped", "head_branch": "main",
                "created_at": "2026-10-14T02:06:14Z"}]
        found = self.run_(verify, red)
        self.assertEqual(len(found), 1)
        self.assertIn("safety net did not fire", found[0]["why"])

    def test_a_non_skipped_red_main_inside_the_window_passes(self):
        verify = [{"id": 1, "name": "Verify", "conclusion": "failure", "head_branch": "main",
                   "created_at": "2026-10-14T01:52:43Z"}]
        red = [{"id": 2, "name": "Red main", "conclusion": "success", "head_branch": "main",
                "created_at": "2026-10-14T02:06:14Z"}]
        self.assertEqual(self.run_(verify, red), [])

    def test_a_late_red_main_outside_the_window_fails(self):
        verify = [{"id": 1, "name": "Verify", "conclusion": "failure", "head_branch": "main",
                   "created_at": "2026-10-14T01:00:00Z"}]
        red = [{"id": 2, "name": "Red main", "conclusion": "success", "head_branch": "main",
                "created_at": "2026-10-14T03:00:00Z"}]
        self.assertEqual(len(self.run_(verify, red)), 1)

    def test_a_branch_verify_failure_is_not_a_trunk_red(self):
        verify = [{"id": 1, "name": "Verify", "conclusion": "failure", "head_branch": "feature",
                   "created_at": "2026-10-14T01:52:43Z"}]
        self.assertEqual(self.run_(verify, []), [])

    def test_a_successful_trunk_verify_needs_no_red_main(self):
        verify = [{"id": 1, "name": "Verify", "conclusion": "success", "head_branch": "main",
                   "created_at": "2026-10-14T01:52:43Z"}]
        self.assertEqual(self.run_(verify, []), [])



    def run_check(self, text, gh=None):
        with tempfile.TemporaryDirectory() as tmp:
            wf = pathlib.Path(tmp) / ".github" / "workflows"
            wf.mkdir(parents=True)
            (wf / "ci.yml").write_text(text)
            return drift.main(["--days", str(drift.DRIFT_DAYS), "check", "--dir", tmp], gh=gh or FakeGh())

    def test_exit_codes(self):
        self.assertEqual(self.run_check(workflow(f"SylphxAI/.github/.github/actions/x@{BEHIND}")), 1)
        self.assertEqual(self.run_check(workflow(f"SylphxAI/.github/.github/actions/x@{FRESH}")), 0)
        self.assertEqual(self.run_check(workflow("actions/checkout@v4")), 0)

    def test_an_unreadable_head_is_a_check_error_not_a_pass(self):
        class NoHead(FakeGh):
            def rest(self, path):
                if "commits/main" in path:
                    raise RuntimeError("Not Found (HTTP 404)")
                return super().rest(path)

        self.assertEqual(self.run_check(workflow(f"SylphxAI/.github/.github/actions/x@{FRESH}"), NoHead()), 2)


if __name__ == "__main__":
    unittest.main()
