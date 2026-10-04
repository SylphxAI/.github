#!/usr/bin/env python3
"""Tests for the main-red-gate decision: a red trunk admits only the revert or the fix while its way back is in motion."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import pathlib
import unittest
import urllib.error
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "main_red_gate",
    pathlib.Path(__file__).resolve().parents[1] / ".github" / "actions" / "main-red-gate" / "main_red_gate.py")
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)

MOTION = "a newer verify.yml run is still running (u)"


def run(conclusion: str | None, status: str = "completed", event: str = "push", branch: str = "main") -> dict:
    return {"id": 1, "event": event, "head_branch": branch, "status": status, "conclusion": conclusion,
            "html_url": "u", "head_sha": "a" * 40}


def running(status: str = "in_progress", **kwargs) -> dict:
    return run(None, status=status, **kwargs)


def pull(title: str = "feat: x", head: str = "feat/x", labels: tuple[str, ...] = (), number: int = 9,
         draft: bool = False) -> dict:
    return {"number": number, "title": title, "head": {"ref": head}, "draft": draft,
            "labels": [{"name": n} for n in labels]}


class TrunkStateTest(unittest.TestCase):
    def test_newest_conclusive_run_decides(self) -> None:
        self.assertEqual(gate.trunk_state([run("failure"), run("success")], "main")[0], "red")
        self.assertEqual(gate.trunk_state([run("success"), run("failure")], "main")[0], "green")

    def test_cancelled_and_skipped_are_passed_over(self) -> None:
        runs = [run("cancelled"), run("skipped"), run("failure"), run("success")]
        self.assertEqual(gate.trunk_state(runs, "main")[0], "red")
        self.assertEqual(gate.trunk_state([run("cancelled"), run("success")], "main")[0], "green")

    def test_failure_kinds_are_red(self) -> None:
        for kind in ("failure", "timed_out", "action_required", "startup_failure"):
            self.assertEqual(gate.trunk_state([run(kind)], "main")[0], "red", kind)

    def test_other_branches_events_and_unfinished_runs_do_not_count(self) -> None:
        runs = [run("failure", branch="sylphx-verify/abc"), run("failure", event="workflow_dispatch"),
                run("failure", status="in_progress")]
        self.assertEqual(gate.trunk_state(runs, "main"), ("unknown", None))

    def test_no_runs_is_unknown(self) -> None:
        self.assertEqual(gate.trunk_state([], "main"), ("unknown", None))


class NewerUnfinishedRunTest(unittest.TestCase):
    def test_a_run_still_going_after_the_red_one_is_found(self) -> None:
        red = run("failure")
        for status in ("in_progress", "queued", "waiting", "pending", "requested"):
            going = running(status)
            runs = [going, red, run("success")]
            self.assertIs(gate.newer_unfinished_run(runs, "main", gate.trunk_state(runs, "main")[1]), going, status)

    def test_a_run_older_than_the_red_one_is_ignored(self) -> None:
        red = run("failure")
        runs = [red, running()]
        self.assertIsNone(gate.newer_unfinished_run(runs, "main", gate.trunk_state(runs, "main")[1]))

    def test_other_branches_and_events_are_ignored(self) -> None:
        runs = [running(branch="feature"), running(event="workflow_dispatch"), run("failure")]
        self.assertIsNone(gate.newer_unfinished_run(runs, "main", gate.trunk_state(runs, "main")[1]))

    def test_finished_runs_are_not_running(self) -> None:
        runs = [run("cancelled"), run("skipped"), run("failure")]
        self.assertIsNone(gate.newer_unfinished_run(runs, "main", gate.trunk_state(runs, "main")[1]))


class MergeGroupRefTest(unittest.TestCase):
    def test_pull_request_number(self) -> None:
        self.assertEqual(gate.pr_number("gh-readonly-queue/main/pr-4321-" + "0" * 40), 4321)
        self.assertEqual(gate.pr_number("refs/heads/gh-readonly-queue/release/x/pr-7-abcdef1"), 7)
        self.assertIsNone(gate.pr_number("refs/heads/main"))
        self.assertIsNone(gate.pr_number(""))


class WayBackTest(unittest.TestCase):
    def test_reverts_the_outage_jump_and_the_fix_are_the_way_back(self) -> None:
        for p in (pull(head="auto-revert/abcdef123"), pull(title="Revert \"feat: x\""), pull(labels=("auto-revert",)),
                  pull(labels=("queue-jump:red-main",)), pull(labels=("queue-jump:outage",)),
                  pull(labels=("main-red-fix",))):
            self.assertTrue(gate.is_way_back(p, "main-red-fix"), p)

    def test_an_ordinary_change_is_not(self) -> None:
        self.assertIsNone(gate.is_way_back(pull(), "main-red-fix"))
        self.assertIsNone(gate.is_way_back(pull(title="revisit docs"), "main-red-fix"))
        self.assertIsNone(gate.is_way_back(pull(labels=("main-red-fix",)), "other"))

    def test_open_way_back_finds_an_open_revert_or_fix(self) -> None:
        for p in (pull(head="auto-revert/abcdef123", number=41), pull(title="Revert x", number=41),
                  pull(labels=("main-red-fix",), number=41), pull(labels=("queue-jump:outage",), number=41)):
            found = gate.open_way_back([pull(number=40), p], "main-red-fix")
            self.assertIn("#41", found, p)

    def test_open_way_back_ignores_drafts_and_ordinary_changes(self) -> None:
        self.assertIsNone(gate.open_way_back([pull(), pull(title="Revert x", draft=True)], "main-red-fix"))
        self.assertIsNone(gate.open_way_back([], "main-red-fix"))


class DecideTest(unittest.TestCase):
    def test_green_and_unknown_admit_anything(self) -> None:
        for state in ("green", "unknown"):
            self.assertTrue(gate.decide(state, pull(), "enforce", "main-red-fix", MOTION)[0])

    def test_red_with_the_way_back_in_motion_refuses_an_ordinary_change(self) -> None:
        admit, message = gate.decide("red", pull(), "enforce", "main-red-fix", MOTION)
        self.assertFalse(admit)
        self.assertIn("main-red-fix", message)
        self.assertIn(MOTION, message)

    def test_red_with_nothing_in_motion_admits_with_a_warning(self) -> None:
        # The red-main handler reverts only after a second completed failure and never for infrastructure,
        # timed-out or not-started runs: a gate that stayed shut here would wait for a run nobody can start.
        admit, message = gate.decide("red", pull(), "enforce", "main-red-fix", None)
        self.assertTrue(admit)
        self.assertTrue(message.startswith("::warning::"), message)
        self.assertIn("nothing is on its way to green", message)

    def test_red_admits_reverts_and_the_outage_jump_even_in_motion(self) -> None:
        for p in (pull(head="auto-revert/abcdef123"), pull(title="Revert \"feat: x\""),
                  pull(labels=("auto-revert",)), pull(labels=("queue-jump:red-main",)),
                  pull(labels=("queue-jump:outage",))):
            self.assertTrue(gate.decide("red", p, "enforce", "main-red-fix", MOTION)[0], p)

    def test_red_admits_the_labelled_fix_only(self) -> None:
        self.assertTrue(gate.decide("red", pull(labels=("main-red-fix",)), "enforce", "main-red-fix", MOTION)[0])
        self.assertFalse(gate.decide("red", pull(labels=("main-red-fix",)), "enforce", "other", MOTION)[0])
        self.assertFalse(gate.decide("red", pull(title="revisit docs"), "enforce", "main-red-fix", MOTION)[0])

    def test_unreadable_pull_request_admits_with_a_warning(self) -> None:
        admit, message = gate.decide("red", None, "enforce", "main-red-fix", MOTION)
        self.assertTrue(admit)
        self.assertIn("::warning::", message)

    def test_observe_mode_never_refuses(self) -> None:
        admit, message = gate.decide("red", pull(), "observe", "main-red-fix", MOTION)
        self.assertTrue(admit)
        self.assertIn("would have refused", message)


class MainTest(unittest.TestCase):
    """The whole gate against a fake API: the liveness cases of a red trunk."""

    ENV = {"EVENT_NAME": "merge_group", "REPO": "o/r", "TOKEN": "t", "BASE_BRANCH": "refs/heads/main",
           "VERIFY_WORKFLOW": "verify.yml", "MODE": "enforce", "FIX_LABEL": "main-red-fix",
           "MERGE_GROUP_REF": "gh-readonly-queue/main/pr-77-" + "a" * 40}

    def go(self, runs, this_pr=None, open_pulls=(), env=None, list_error=False):
        calls: list[str] = []

        def fake_get(url, token):
            calls.append(url)
            if "/actions/workflows/verify.yml/runs" in url:
                return {"workflow_runs": runs}
            if url.endswith("/pulls/77"):
                return this_pr or pull(number=77)
            if "/pulls?state=open" in url:
                if list_error:
                    raise urllib.error.URLError("boom")
                return list(open_pulls)
            raise AssertionError(url)

        out = io.StringIO()
        with mock.patch.dict(os.environ, {**self.ENV, **(env or {})}), \
                mock.patch.object(gate, "_get", fake_get), contextlib.redirect_stdout(out):
            code = gate.main()
        return code, out.getvalue(), calls

    def test_not_a_merge_group_is_not_gated(self) -> None:
        code, out, calls = self.go([run("failure")], env={"EVENT_NAME": "pull_request"})
        self.assertEqual((code, calls), (0, []))

    def test_green_admits_without_reading_pull_requests(self) -> None:
        code, out, calls = self.go([run("success")])
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1, calls)

    def test_first_red_in_a_quiet_repository_admits_so_the_confirming_run_can_start(self) -> None:
        code, out, _ = self.go([run("failure"), run("success")])
        self.assertEqual(code, 0)
        self.assertIn("::warning::trunk is red but nothing is on its way to green", out)

    def test_timed_out_and_not_started_runs_never_start_the_handler_and_never_jam_the_queue(self) -> None:
        for kind in ("timed_out", "startup_failure", "action_required"):
            code, out, _ = self.go([run(kind), run("success")])
            self.assertEqual(code, 0, kind)
            self.assertIn("nothing is on its way to green", out, kind)

    def test_a_newer_verify_run_still_going_holds_the_line(self) -> None:
        code, out, calls = self.go([running(), run("failure"), run("success")])
        self.assertEqual(code, 1)
        self.assertIn("::error::trunk is red and a newer verify.yml run is still running (u)", out)
        self.assertFalse([c for c in calls if "/pulls?state=open" in c], "no need to list pull requests")

    def test_an_open_revert_or_fix_pull_request_holds_the_line(self) -> None:
        for way_back in (pull(head="auto-revert/abc", number=41), pull(labels=("main-red-fix",), number=41)):
            code, out, _ = self.go([run("failure")], open_pulls=[pull(number=40), way_back])
            self.assertEqual(code, 1, way_back)
            self.assertIn("pull request #41", out)

    def test_a_draft_revert_does_not_hold_the_line(self) -> None:
        code, out, _ = self.go([run("failure")], open_pulls=[pull(title="Revert x", draft=True, number=41)])
        self.assertEqual(code, 0)
        self.assertIn("nothing is on its way to green", out)

    def test_the_revert_itself_and_an_outage_fix_pass_while_the_line_is_held(self) -> None:
        for this_pr in (pull(head="auto-revert/abc", number=77), pull(labels=("queue-jump:outage",), number=77)):
            code, out, calls = self.go([running(), run("failure")], this_pr=this_pr)
            self.assertEqual(code, 0, this_pr)
            self.assertIn("way back to green", out)

    def test_a_pull_request_list_that_cannot_be_read_admits(self) -> None:
        code, out, _ = self.go([run("failure")], list_error=True)
        self.assertEqual(code, 0)
        self.assertIn("::warning::cannot list open pull requests", out)

    def test_observe_mode_prints_what_it_would_refuse(self) -> None:
        code, out, _ = self.go([running(), run("failure")], env={"MODE": "observe"})
        self.assertEqual(code, 0)
        self.assertIn("would have refused", out)


if __name__ == "__main__":
    unittest.main()
