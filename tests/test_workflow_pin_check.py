#!/usr/bin/env python3
"""Tests for the cross-repository workflow pin check (scripts/workflow_pin_check.py).

GitHub is a fake that answers repository, immutable commit, compare and tree reads.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("workflow_pin_check", ROOT / "scripts" / "workflow_pin_check.py")
pins = importlib.util.module_from_spec(SPEC)
sys.modules["workflow_pin_check"] = pins
SPEC.loader.exec_module(pins)

OWNERS = ["SylphxAI", "Cubeage"]
ON_MAIN = "a" * 40
OFF_MAIN = "b" * 40
ANCESTOR = "c" * 40
GONE = "d" * 40


class FakeGh:
    def __init__(self, branch="main"):
        self.branch, self.calls = branch, []
        self.status = {ON_MAIN: "identical", ANCESTOR: "behind", OFF_MAIN: "ahead"}
        self.targets = {"action.yml", "other/action.yaml", "sub/action.yml", ".github/workflows/x.yml"}
        self.truncated = False

    def rest(self, path):
        self.calls.append(path)
        if path.count("/") == 2:  # repos/owner/name
            return {"default_branch": self.branch}
        if "/compare/" in path:
            sha = path.rsplit("...", 1)[1]
            if sha not in self.status:
                raise RuntimeError("Not Found (HTTP 404)")
            return {"status": self.status[sha]}
        if "/git/trees/" in path:
            return {"truncated": self.truncated, "tree": [
                {"path": target, "type": "blob", "mode": "100644"} for target in self.targets]}
        if "/commits/" in path:
            sha = path.rsplit("/", 1)[1]
            sha = ON_MAIN if sha == self.branch else sha
            if sha not in self.status:
                raise RuntimeError("Not Found (HTTP 404)")
            return {"sha": sha, "commit": {"committer": {"date": "2026-10-30T00:00:00Z"},
                                           "tree": {"sha": sha}}}
        raise AssertionError(path)


def workflow(*uses: str) -> str:
    steps = "".join(f"      - uses: {u}\n" for u in uses)
    return f"name: CI\non: [pull_request]\njobs:\n  a:\n    runs-on: sylphx-linux-standard\n    steps:\n{steps}"


class FindPins(unittest.TestCase):
    def test_owned_full_sha_pins_only(self):
        text = workflow(
            f"SylphxAI/studio/.github/workflows/x.yml@{OFF_MAIN}", "actions/checkout@" + "e" * 40,
            "SylphxAI/.github/.github/actions/y@v1", "./.github/actions/local", f"cubeage/tool@{ON_MAIN} # pinned")
        found = pins.find_pins(text, OWNERS)
        self.assertEqual([(p["repo"], p["sha"][:1], p["path"]) for p in found],
                         [("SylphxAI/studio", "b", ".github/workflows/x.yml"), ("cubeage/tool", "a", "")])
        self.assertEqual([p["line"] for p in found], [7, 11])

    def test_a_comment_is_not_a_pin(self):
        self.assertEqual(pins.find_pins(f"# uses: SylphxAI/studio@{OFF_MAIN}\n", OWNERS), [])


class Judge(unittest.TestCase):
    def judge(self, files, gh=None):
        gh = gh or FakeGh()
        return pins.judge(files, OWNERS, pins.Reachability(gh)), gh

    def test_pin_not_on_default_branch_fails_with_file_and_line(self):
        found, _ = self.judge({".github/workflows/ci.yml": workflow(f"SylphxAI/studio/.github/workflows/x.yml@{OFF_MAIN}")})
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0]["file"], found[0]["line"]), (".github/workflows/ci.yml", 7))
        self.assertIn("not on SylphxAI/studio main", found[0]["why"])
        self.assertIn("ci.yml:7:", pins.render_offenders(found))

    def test_pins_on_or_behind_default_pass(self):
        found, _ = self.judge({"a.yml": workflow(f"SylphxAI/x@{ON_MAIN}", f"Cubeage/y/sub@{ANCESTOR}")})
        self.assertEqual(found, [])

    def test_unknown_commit_fails(self):
        found, _ = self.judge({"a.yml": workflow(f"SylphxAI/x@{GONE}")})
        self.assertEqual(len(found), 1)
        self.assertIn("could not be read", found[0]["why"])

    def test_default_branch_name_is_read_not_assumed(self):
        _, gh = self.judge({"a.yml": workflow(f"SylphxAI/x@{ON_MAIN}")}, FakeGh(branch="trunk"))
        self.assertIn("repos/SylphxAI/x/commits/trunk", gh.calls)

    def test_missing_action_on_main_fails_with_exact_file_and_line(self):
        found, _ = self.judge({".github/actions/composite/action.yml":
                              workflow(f"SylphxAI/.github/.github/actions/missing@{ON_MAIN}")})
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0]["file"], found[0]["line"]), (".github/actions/composite/action.yml", 7))
        self.assertIn("target is absent", found[0]["why"])
        self.assertIn(".github/actions/missing/action.yml", found[0]["why"])

    def test_off_main_pin_fails_even_with_existing_target(self):
        found, _ = self.judge({"ci.yml": workflow(f"SylphxAI/.github/sub@{OFF_MAIN}")})
        self.assertEqual(len(found), 1)
        self.assertIn("not on", found[0]["why"])

    def test_valid_yaml_action_and_reusable_workflow_pass(self):
        found, _ = self.judge({"ci.yml": workflow(f"SylphxAI/.github/other@{ON_MAIN}",
                              f"SylphxAI/.github/.github/workflows/x.yml@{ANCESTOR}")})
        self.assertEqual(found, [])

    def test_missing_workflow_is_not_replaced_by_another_workflow(self):
        found, _ = self.judge({"ci.yml": workflow(f"SylphxAI/.github/.github/workflows/missing.yml@{ON_MAIN}")})
        self.assertEqual(len(found), 1)
        self.assertIn(".github/workflows/missing.yml", found[0]["why"])

    def test_truncated_tree_fails_closed(self):
        gh = FakeGh()
        gh.truncated = True
        found, _ = self.judge({"ci.yml": workflow(f"SylphxAI/.github/sub@{ON_MAIN}")}, gh)
        self.assertEqual(len(found), 1)
        self.assertIn("truncated", found[0]["why"])

    def test_one_read_per_distinct_pin(self):
        text = workflow(f"SylphxAI/x@{ON_MAIN}", f"SylphxAI/x/other@{ON_MAIN}")
        _, gh = self.judge({"a.yml": text, "b.yml": text})
        self.assertEqual(len(gh.calls), 3)
        self.assertEqual(sum("/git/trees/" in path for path in gh.calls), 1)


class Command(unittest.TestCase):
    def run_check(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            wf = pathlib.Path(tmp) / ".github" / "workflows"
            wf.mkdir(parents=True)
            (wf / "ci.yml").write_text(text)
            return pins.main(["--owner", "SylphxAI", "check", "--dir", tmp], gh=FakeGh())

    def test_exit_codes(self):
        self.assertEqual(self.run_check(workflow(f"SylphxAI/x@{OFF_MAIN}")), 1)
        self.assertEqual(self.run_check(workflow(f"SylphxAI/x@{ON_MAIN}")), 0)
        self.assertEqual(self.run_check(workflow(f"SylphxAI/x/missing@{ON_MAIN}")), 1)
        self.assertEqual(self.run_check(workflow("actions/checkout@v4")), 0)


if __name__ == "__main__":
    unittest.main()
