#!/usr/bin/env python3
"""Tests for the main-red-gate decision: a red trunk admits only the revert or the fix while its way back is in motion."""

from __future__ import annotations

import contextlib
import datetime
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
           "MERGE_GROUP_REF": "gh-readonly-queue/main/pr-77-" + "a" * 40, "HOLD_MINUTES": "0"}

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
        self.assertIn("::error::hold expired after 0 minutes", out)
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


class VerdictHoldTest(unittest.TestCase):
    """Exercise the assembled entrypoint with run-scoped jobs and a controlled clock."""

    def go(self, snapshots, env=None, comparisons=None, this_pr=None, api_error=False, older_verdict="failure"):
        calls = []
        sleeps = []
        clock = [0.0]
        snapshot = [-1]
        base = "b" * 40
        newer = {**running(), "id": 2, "head_sha": base, "html_url": "new"}
        older = {**run("failure"), "id": 1, "html_url": "old"}

        def fake_get(url, token):
            calls.append(url)
            if "/actions/workflows/verify.yml/runs" in url:
                snapshot[0] = min(snapshot[0] + 1, len(snapshots) - 1)
                return {"workflow_runs": [newer, older]}
            if "/compare/" in url:
                sha = url.split("/compare/")[1].split("...")[0]
                return {"status": (comparisons or {}).get(sha, "ahead")}
            if "/actions/runs/" in url:
                if api_error:
                    raise urllib.error.URLError("jobs unavailable")
                current = snapshots[snapshot[0]] if "/runs/2/" in url else older_verdict
                jobs = [] if current == "missing" else [
                    {"name": "full-suite / suite-ok", "status": "in_progress" if current is None else "completed",
                     "conclusion": current}]
                return {"jobs": jobs}
            if url.endswith("/pulls/77"):
                return this_pr or pull(number=77)
            if "/pulls?state=open" in url:
                return []
            raise AssertionError(url)

        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds

        out = io.StringIO()
        config = {**MainTest.ENV, "HOLD_MINUTES": "20", "BASE_SHA": base,
                  "VERDICT_CHECK": "full-suite / suite-ok", **(env or {})}
        with mock.patch.dict(os.environ, config, clear=True), mock.patch.object(gate, "_get", fake_get), \
                mock.patch.object(gate.time, "sleep", sleep), \
                mock.patch.object(gate.time, "monotonic", lambda: clock[0]), contextlib.redirect_stdout(out):
            code = gate.main()
        return code, out.getvalue(), calls, sleeps

    def test_success_verdict_admits_while_run_and_trailing_jobs_are_in_progress(self):
        code, out, calls, sleeps = self.go(["success"])
        self.assertEqual(code, 0)
        self.assertIn("trunk is green", out)
        self.assertEqual(sleeps, [])
        self.assertFalse(any("/pulls" in url for url in calls))

    def test_pending_verdict_turns_green_within_default_twenty_minute_hold(self):
        code, out, calls, sleeps = self.go([None, None, "success"])
        self.assertEqual(code, 0)
        self.assertIn("green: admitted after hold", out)
        self.assertEqual(sleeps, [60, 60])
        self.assertEqual(sum("/compare/" in url for url in calls), 1, "ancestry is immutable and cached")

    def test_pending_verdict_turns_red_and_refuses_even_without_an_open_fix(self):
        code, out, _, sleeps = self.go([None, "failure"])
        self.assertEqual(code, 1)
        self.assertIn("newer verify.yml verdict is red", out)
        self.assertEqual(sleeps, [60])

    def test_default_hold_expires_after_twenty_minutes_not_sixty_seconds(self):
        code, out, _, sleeps = self.go([None])
        self.assertEqual(code, 1)
        self.assertIn("hold expired after 20 minutes", out)
        self.assertIn("verdict is still pending (new)", out)
        self.assertEqual(sleeps, [60] * 20)

    def test_fractional_hold_never_sleeps_past_deadline(self):
        code, out, _, sleeps = self.go([None], env={"HOLD_MINUTES": "1.5"})
        self.assertEqual(code, 1)
        self.assertEqual(sleeps, [60, 30])
        self.assertIn("after 1.5 minutes", out)

    def test_later_red_run_is_excluded_at_merge_groups_green_base(self):
        code, out, calls, sleeps = self.go(["failure"], env={"BASE_SHA": "a" * 40},
                                          comparisons={"b" * 40: "behind"}, older_verdict="success")
        self.assertEqual(code, 0)
        self.assertIn("trunk is green", out)
        self.assertFalse(any("/runs/2/jobs" in url for url in calls))
        self.assertEqual(sleeps, [])

    def test_observe_mode_does_not_hold_or_refuse(self):
        code, out, _, sleeps = self.go([None], env={"MODE": "observe"})
        self.assertEqual(code, 0)
        self.assertIn("would have refused", out)
        self.assertEqual(sleeps, [])

    def test_way_back_does_not_wait_for_pending_verdict(self):
        for p in (pull(title="Revert x"), pull(labels=("main-red-fix",)), pull(labels=("queue-jump:outage",))):
            code, out, _, sleeps = self.go([None], this_pr=p)
            self.assertEqual(code, 0)
            self.assertIn("way back to green", out)
            self.assertEqual(sleeps, [])

    def test_jobs_api_error_preserves_fail_open_contract(self):
        code, out, _, sleeps = self.go([None], api_error=True)
        self.assertEqual(code, 0)
        self.assertIn("::warning::cannot read", out)
        self.assertEqual(sleeps, [])

    def test_missing_verdict_cannot_borrow_success_from_the_run(self):
        code, out, _, sleeps = self.go(["missing"], env={"HOLD_MINUTES": "1"})
        self.assertEqual(code, 1)
        self.assertIn("hold expired", out)
        self.assertEqual(sleeps, [60])

    def test_invalid_hold_is_rejected_without_api_calls(self):
        for hold in ("-1", "inf", "nan", "bad"):
            code, out, calls, sleeps = self.go([None], env={"HOLD_MINUTES": hold})
            self.assertEqual(code, 1)
            self.assertIn("invalid hold-minutes", out)
            self.assertEqual(calls, [])

    def test_jobs_pagination_and_run_scoping(self):
        newer = {**running(), "id": 2}
        pages = [{"jobs": [{"name": "other", "status": "completed", "conclusion": "failure"}] * 100},
                 {"jobs": [{"name": "full-suite / suite-ok", "status": "completed", "conclusion": "success"}]}]
        with mock.patch.object(gate, "_get", side_effect=pages) as get:
            projected = gate.gate_runs([newer, run("failure")], "main", "", "full-suite / suite-ok", "api", "t", {})
        self.assertEqual(gate.trunk_state(projected, "main")[0], "green")
        self.assertEqual([c.args[0] for c in get.call_args_list], [
            "api/actions/runs/2/jobs?filter=latest&per_page=100&page=1",
            "api/actions/runs/2/jobs?filter=latest&per_page=100&page=2"])


def stamped(minutes_ago: float, **kwargs) -> dict:
    at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=minutes_ago)
    return {**run(**kwargs), "created_at": at.strftime("%Y-%m-%dT%H:%M:%SZ")}


class WatchTest(unittest.TestCase):
    """`mode: watch`: alert when the trunk has stayed red past the limit, whatever the verdict was."""

    ENV = {"EVENT_NAME": "schedule", "REPO": "o/r", "TOKEN": "t", "BASE_BRANCH": "main",
           "VERIFY_WORKFLOW": "verify.yml", "MODE": "watch", "WATCH_LIMIT_MINUTES": "30",
           "FIX_LABEL": "main-red-fix"}

    def go(self, runs, open_pulls=(), env=None, post_error=False):
        calls: list[str] = []
        posts: list[tuple[str, dict]] = []

        def fake_get(url, token):
            calls.append(url)
            if "/actions/workflows/verify.yml/runs" in url:
                return {"workflow_runs": runs}
            if "/pulls?state=open" in url:
                return list(open_pulls)
            if "/issues?state=open" in url:
                return []
            raise AssertionError(url)

        def fake_post(url, token, payload):
            calls.append(url)
            posts.append((url, payload))
            if post_error:
                raise urllib.error.HTTPError(url, 403, "forbidden", {}, None)
            if url.endswith("/issues"):
                return {"number": 55}
            return {}

        out = io.StringIO()
        with mock.patch.dict(os.environ, {**self.ENV, **(env or {})}), \
                mock.patch.object(gate, "_get", fake_get), mock.patch.object(gate, "_post", fake_post), \
                contextlib.redirect_stdout(out):
            code = gate.main()
        return code, out.getvalue(), calls, posts

    def test_a_green_trunk_alerts_nothing(self) -> None:
        code, out, calls, posts = self.go([stamped(120, conclusion="success")])
        self.assertEqual((code, posts), (0, []))
        self.assertIn("nothing to alert", out)

    def test_a_red_trunk_past_the_limit_files_one_watchdog_issue(self) -> None:
        code, out, calls, posts = self.go([stamped(45, conclusion="failure")])
        self.assertEqual(code, 0)
        opened = [p for url, p in posts if url.endswith("/issues")]
        self.assertEqual(len(opened), 1, posts)
        self.assertEqual(opened[0]["labels"], ["red-main-red-watchdog", "owner:ops"])
        self.assertIn("45 minutes", opened[0]["body"])
        self.assertIn("red-main-red-watchdog", opened[0]["labels"])

    def test_a_red_trunk_inside_the_limit_waits(self) -> None:
        code, out, calls, posts = self.go([stamped(12, conclusion="failure")])
        self.assertEqual((code, posts), (0, []))
        self.assertIn("nothing to alert", out)

    def test_the_limit_is_read_from_the_environment(self) -> None:
        code, out, calls, posts = self.go([stamped(12, conclusion="failure")],
                                          env={"WATCH_LIMIT_MINUTES": "10"})
        self.assertTrue([p for url, p in posts if url.endswith("/issues")], out)

    def test_a_way_back_in_motion_is_the_net_working_and_does_not_page(self) -> None:
        code, out, calls, posts = self.go([stamped(90, conclusion="failure")],
                                          open_pulls=[pull(head="auto-revert/abc", number=41)])
        self.assertEqual((code, posts), (0, []))
        self.assertIn("nothing to alert", out)

    def test_an_unreadable_age_does_not_alert(self) -> None:
        # No created_at: the age cannot be judged, so the watchdog stays quiet
        # rather than page on a guess.
        stale = {k: v for k, v in run("failure").items() if k != "created_at"}
        code, out, calls, posts = self.go([stale])
        self.assertEqual((code, posts), (0, []))
        self.assertIn("age could not be read", out)

    def test_an_issue_write_that_is_refused_is_reported_not_swallowed(self) -> None:
        code, out, calls, posts = self.go([stamped(60, conclusion="failure")], post_error=True)
        self.assertEqual(code, 0)
        self.assertIn("needs issues: write", out)


if __name__ == "__main__":
    unittest.main()
