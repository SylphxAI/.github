#!/usr/bin/env python3
"""Tests for the shared-action pin drift check (scripts/action_pin_drift_check.py).

GitHub is a fake that answers the commit reads: .github main's head and one committer date per pinned
commit. Dates are fixed so "days behind" is deterministic.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import yaml

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
            f"SylphxAI/.github/.github/actions/needs-pass@{BEHIND} # main",
            f"SylphxAI/.github/.github/workflows/rust-ci.yml@{FRESH} # main",
            "SylphxAI/.github/.github/actions/needs-pass@main",  # a branch, not judged here
            "actions/checkout@" + "f" * 40,
            "./.github/actions/local")
        found = drift.find_pins(text)
        self.assertEqual([(p["path"], p["sha"][0]) for p in found],
                         [(".github/actions/needs-pass", "d"), (".github/workflows/rust-ci.yml", "b")])
        self.assertEqual([p["line"] for p in found], [7, 8])

    def test_a_comment_is_not_a_pin(self):
        self.assertEqual(drift.find_pins(f"# uses: SylphxAI/.github/.github/actions/needs-pass@{BEHIND}\n"), [])

    def test_branch_comment_is_outside_the_quoted_pin(self):
        for quote in ("", "'", '"'):
            with self.subTest(quote=quote):
                pin = f"{quote}SylphxAI/.github/.github/actions/x@{HEAD}{quote} # main"
                self.assertTrue(drift.find_pins(workflow(pin))[0]["tracks_main"])

    def test_bare_sha_and_other_comments_do_not_track_main(self):
        for comment in ("", " # v1", " # master", " # main because it is current"):
            with self.subTest(comment=comment):
                pin = f"SylphxAI/.github/.github/actions/x@{HEAD}{comment}"
                self.assertFalse(drift.find_pins(workflow(pin))[0]["tracks_main"])

    def test_main_inside_the_quoted_value_is_not_a_version_comment(self):
        self.assertEqual(drift.find_pins(workflow(f'"SylphxAI/.github/.github/actions/x@{HEAD} # main"')), [])


class Judge(unittest.TestCase):
    def test_bare_sha_fails_even_at_current_head(self):
        for sha in (HEAD, FRESH, AT_LIMIT):
            with self.subTest(sha=sha):
                files = {".github/workflows/ci.yml": workflow(f"SylphxAI/.github/.github/actions/x@{sha}")}
                found, _ = judge(files)
                self.assertEqual(len(found), 1)
                self.assertEqual((found[0]["file"], found[0]["line"]), (".github/workflows/ci.yml", 7))
                self.assertIn("missing # main", found[0]["why"])
                self.assertEqual(drift.main_marker_errors(files), found)

    def test_pin_past_the_limit_fails_with_file_line_and_days(self):
        found, _ = judge({".github/workflows/ci.yml": workflow(f"SylphxAI/.github/.github/actions/needs-pass@{BEHIND} # main")})
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0]["file"], found[0]["line"]), (".github/workflows/ci.yml", 7))
        self.assertEqual(found[0]["days"], drift.DRIFT_DAYS + 1)
        self.assertIn("days behind main", found[0]["why"])
        self.assertIn("ci.yml:7:", drift.render_offenders(found))

    def test_fresh_and_at_the_limit_pass(self):
        found, _ = judge({"a.yml": workflow(f"SylphxAI/.github/.github/actions/x@{FRESH} # main",
                                            f"SylphxAI/.github/.github/actions/y@{AT_LIMIT} # main")})
        self.assertEqual(found, [])

    def test_the_head_commit_itself_is_never_behind(self):
        # A pin already at .github main is current, whatever its date.
        found, gh = judge({"a.yml": workflow(f"SylphxAI/.github/.github/actions/x@{HEAD} # main")})
        self.assertEqual(found, [])
        self.assertFalse(any(c.endswith(HEAD) for c in gh.calls if "commits/main" not in c))

    def test_unknown_commit_fails(self):
        found, _ = judge({"a.yml": workflow(f"SylphxAI/.github/.github/actions/x@{GONE} # main")})
        self.assertEqual(len(found), 1)
        self.assertIn("could not be read", found[0]["why"])

    def test_one_read_per_distinct_pin(self):
        text = workflow(f"SylphxAI/.github/.github/actions/x@{FRESH} # main",
                        f"SylphxAI/.github/.github/actions/x@{FRESH} # main")
        _, gh = judge({"a.yml": text, "b.yml": text})
        self.assertEqual(sum(1 for c in gh.calls if c.endswith(FRESH)), 1)

    def test_days_argument_widens_the_window(self):
        found, _ = judge({"a.yml": workflow(f"SylphxAI/.github/.github/actions/x@{BEHIND} # main")},
                         days=drift.DRIFT_DAYS + 5)
        self.assertEqual(found, [])


class ReadRepo(unittest.TestCase):
    class Reader:
        def __init__(self):
            self.tree = {"truncated": False, "tree": [
                {"type": "blob", "path": "action.yml", "sha": "nested"},
                {"type": "blob", "path": "README.md", "sha": "ignored"}]}
            self.content = workflow(f"SylphxAI/.github/.github/actions/x@{HEAD}")
            self.reply = {"data": {"repository": {
                "wf": {"entries": [{"name": "ci.yaml", "type": "blob", "object": {"text": "name: CI"}}]},
                "actions": {"entries": [{"name": "composite", "type": "tree", "oid": "action-tree"}]}}}}
            self.calls = []

        def graphql(self, query):
            self.calls.append(query)
            return self.reply

        def rest(self, path):
            self.calls.append(path)
            if "/git/trees/" in path:
                return self.tree
            return {"encoding": "base64", "content": base64.b64encode(self.content.encode()).decode()}

    def test_workflows_and_nested_composite_actions_are_judged(self):
        gh = self.Reader()
        files, error = drift.read_repo(gh, "example", "repo")
        self.assertEqual(error, "")
        self.assertEqual(set(files), {".github/workflows/ci.yaml", ".github/actions/composite/action.yml"})
        self.assertEqual(drift.main_marker_errors(files)[0]["file"], ".github/actions/composite/action.yml")
        self.assertFalse(any(c.endswith("ignored") for c in gh.calls))

    def test_partial_graphql_errors_are_not_empty_success(self):
        gh = self.Reader()
        gh.reply["errors"] = [{"message": "Resource not accessible by integration"}]
        files, error = drift.read_repo(gh, "example", "repo")
        self.assertIsNone(files)
        self.assertIn("Resource not accessible", error)

    def test_truncated_tree_is_unreadable(self):
        gh = self.Reader()
        gh.tree["truncated"] = True
        files, error = drift.read_repo(gh, "example", "repo")
        self.assertIsNone(files)
        self.assertIn("truncated", error)

    def test_missing_workflow_directories_are_empty(self):
        gh = self.Reader()
        gh.reply = {"data": {"repository": {"wf": None, "actions": None}}}
        self.assertEqual(drift.read_repo(gh, "example", "repo"), ({}, ""))


class Audit(unittest.TestCase):
    def test_public_scope_and_source_exception_are_explicit(self):
        bases = [{"name": name, "isPrivate": private}
                 for name, private in ((".github", False), ("public", False), ("private", True), ("delivered", False))]
        files = {"ci.yml": workflow(f"SylphxAI/.github/.github/actions/x@{HEAD}")}
        with mock.patch.object(drift.om, "list_org", return_value=bases), \
                mock.patch.object(drift, "read_repo", return_value=(files, "")) as reader:
            report = drift.audit(FakeGh(), ["SylphxAI"], ["SylphxAI/delivered"], public_only=True)
        self.assertEqual(report["scope"], "public")
        self.assertEqual(report["summary"], {"repos": 1, "PASS": 0, "FAIL": 1, "UNREADABLE": 0})
        self.assertEqual(reader.call_args.args[1:], ("SylphxAI", "public"))

    def test_repository_list_failure_is_not_a_zero_repository_pass(self):
        with mock.patch.object(drift.om, "list_org", side_effect=RuntimeError("token refused")):
            report = drift.audit(FakeGh(), ["example"], [])
            self.assertEqual(drift.main(["audit", "--org", "example"], gh=FakeGh()), 1)
            self.assertEqual(drift.main(["watcher", "--org", "example"], gh=FakeGh()), 1)
        self.assertEqual(report["summary"]["UNREADABLE"], 1)
        self.assertIn("token refused", report["repos"][0]["error"])


class WorkflowInputs(unittest.TestCase):
    def test_missing_token_fails_before_either_audit_runs(self):
        document = yaml.safe_load((ROOT / ".github/workflows/action-pin-drift.yml").read_text())
        audit_steps = [s for s in document["jobs"]["drift"]["steps"] if "run" in s]
        self.assertEqual(len(audit_steps), 2)
        for step in audit_steps:
            with self.subTest(step=step["name"]), tempfile.TemporaryDirectory() as tmp:
                fake_python = pathlib.Path(tmp) / "python3"
                fake_python.write_text("#!/bin/sh\necho AUDIT_STARTED\n")
                fake_python.chmod(0o755)
                env = {**os.environ, "GH_TOKEN": "", "PATH": tmp + os.pathsep + os.environ["PATH"]}
                result = subprocess.run(["bash", "-e", "-c", step["run"]], env=env, text=True, capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("read-only GitHub token is unavailable", result.stderr)
                self.assertNotIn("AUDIT_STARTED", result.stdout)


class Watcher(unittest.TestCase):
    """A red trunk must draw a red-main run that reached its repair step within the window."""

    class FakeRuns:
        """Serves runs for the workflow-name filter and jobs for the repair-step check."""

        def __init__(self, rows, jobs=None):
            self.rows, self.jobs, self.calls = rows, jobs or {}, []

        def rest(self, path):
            self.calls.append(path)
            if "/jobs" in path:
                run_id = int(path.split("/runs/")[1].split("/")[0])
                return {"jobs": self.jobs.get(run_id, [])}
            return {"workflow_runs": self.rows}

    REPAIR = [{"name": "Red main handler", "steps": [
        {"name": "Decide the verdict of the failed run", "conclusion": "success"},
        {"name": "Revert the culprit or report it", "conclusion": "success"}]}]
    ACK_ONLY = [{"name": "Red main handler", "steps": [
        {"name": "Decide the verdict of the failed run", "conclusion": "success"},
        {"name": "Revert the culprit or report it", "conclusion": "skipped"}]}]

    def run_(self, verify, red, jobs=None, trunk="main", window=30, gh=None):
        gh = gh or self.FakeRuns(verify + red, jobs)
        return drift.watcher_offenders(gh, "o/r", trunk, window), gh

    def test_skipped_red_main_fails(self):
        verify = [{"id": 1, "name": "Verify", "conclusion": "failure", "head_branch": "main",
                   "created_at": "2026-10-14T01:52:43Z"}]
        red = [{"id": 2, "name": "Red main", "conclusion": "skipped", "head_branch": "main",
                "created_at": "2026-10-14T02:06:14Z"}]
        found, _ = self.run_(verify, red)
        self.assertEqual(len(found), 1)
        self.assertIn("repair step", found[0]["why"])

    def test_a_green_run_that_stopped_at_classification_is_not_a_repair(self):
        # The exact keel shape: conclusion success, but the repair step is skipped (verdict=infra).
        verify = [{"id": 1, "name": "Verify", "conclusion": "failure", "head_branch": "main",
                   "created_at": "2026-10-14T01:52:43Z"}]
        red = [{"id": 2, "name": "Red main", "conclusion": "success", "head_branch": "main",
                "created_at": "2026-10-14T02:06:14Z"}]
        found, _ = self.run_(verify, red, jobs={2: self.ACK_ONLY})
        self.assertEqual(len(found), 1)

    def test_a_run_that_reached_the_repair_step_passes(self):
        verify = [{"id": 1, "name": "Verify", "conclusion": "failure", "head_branch": "main",
                   "created_at": "2026-10-14T01:52:43Z"}]
        red = [{"id": 2, "name": "Red main", "conclusion": "success", "head_branch": "main",
                "created_at": "2026-10-14T02:06:14Z"}]
        found, _ = self.run_(verify, red, jobs={2: self.REPAIR})
        self.assertEqual(found, [])

    def test_a_late_repair_outside_the_window_fails(self):
        verify = [{"id": 1, "name": "Verify", "conclusion": "failure", "head_branch": "main",
                   "created_at": "2026-10-14T01:00:00Z"}]
        red = [{"id": 2, "name": "Red main", "conclusion": "success", "head_branch": "main",
                "created_at": "2026-10-14T03:00:00Z"}]
        found, _ = self.run_(verify, red, jobs={2: self.REPAIR})
        self.assertEqual(len(found), 1)

    def test_a_branch_verify_failure_is_not_a_trunk_red(self):
        verify = [{"id": 1, "name": "Verify", "conclusion": "failure", "head_branch": "feature",
                   "created_at": "2026-10-14T01:52:43Z"}]
        self.assertEqual(self.run_(verify, [])[0], [])

    def test_a_successful_trunk_verify_needs_no_red_main(self):
        verify = [{"id": 1, "name": "Verify", "conclusion": "success", "head_branch": "main",
                   "created_at": "2026-10-14T01:52:43Z"}]
        self.assertEqual(self.run_(verify, [])[0], [])

    def test_jobs_are_read_only_for_candidate_runs(self):
        # A skipped run is not a candidate, so its jobs are never fetched.
        verify = [{"id": 1, "name": "Verify", "conclusion": "failure", "head_branch": "main",
                   "created_at": "2026-10-14T01:52:43Z"}]
        red = [{"id": 2, "name": "Red main", "conclusion": "skipped", "head_branch": "main",
                "created_at": "2026-10-14T02:06:14Z"}]
        _, gh = self.run_(verify, red)
        self.assertFalse(any("/jobs" in c for c in gh.calls))


class Command(unittest.TestCase):
    def run_check(self, text, gh=None):
        with tempfile.TemporaryDirectory() as tmp:
            wf = pathlib.Path(tmp) / ".github" / "workflows"
            wf.mkdir(parents=True)
            (wf / "ci.yml").write_text(text)
            return drift.main(["--days", str(drift.DRIFT_DAYS), "check", "--dir", tmp], gh=gh or FakeGh())

    def test_exit_codes(self):
        self.assertEqual(self.run_check(workflow(f"SylphxAI/.github/.github/actions/x@{HEAD}")), 1)
        self.assertEqual(self.run_check(workflow(f"SylphxAI/.github/.github/actions/x@{BEHIND} # main")), 1)
        self.assertEqual(self.run_check(workflow(f"SylphxAI/.github/.github/actions/x@{FRESH} # main")), 0)
        self.assertEqual(self.run_check(workflow("actions/checkout@v4")), 0)

    def test_an_unreadable_head_is_a_check_error_not_a_pass(self):
        class NoHead(FakeGh):
            def rest(self, path):
                if "commits/main" in path:
                    raise RuntimeError("Not Found (HTTP 404)")
                return super().rest(path)

        self.assertEqual(self.run_check(workflow(f"SylphxAI/.github/.github/actions/x@{FRESH} # main"), NoHead()), 2)


if __name__ == "__main__":
    unittest.main()
