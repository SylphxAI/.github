"""Behavioral tests for the legacy cross-workflow completion boundary."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import unittest
import pathlib
from pathlib import Path
from unittest import mock

import pytest

SPEC = importlib.util.spec_from_file_location(
    "ci_ok_polling", Path(__file__).resolve().parents[1] / ".github/actions/ci-ok-polling/ci_ok.py")
ci_ok = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci_ok)


def run(name, status="completed", conclusion="success"):
    return {"name": name, "status": status, "conclusion": conclusion}


@pytest.mark.parametrize("bad", sorted(ci_ok.BAD))
@pytest.mark.parametrize("status", ["queued", "in_progress"])
def test_terminal_failure_wins_over_pending(bad, status):
    assert ci_ok.evaluate([run("build", conclusion=bad), run("tests", status, None)], set()) == (
        "fail", [f"build={bad}"])


def test_passing_checks_do_not_override_pending_checks():
    assert ci_ok.evaluate([run("build"), run("tests", "queued", None)], set()) == ("pending", ["tests"])


def test_cli_exits_on_first_failure_poll_without_stability_delay():
    env = {"REPO": "owner/repo", "SHA": "head", "TOKEN": "test",
           "CI_OK_SETTLE": "0"}
    output = io.StringIO()
    with mock.patch.dict(ci_ok.os.environ, env, clear=True), \
            mock.patch.object(ci_ok, "fetch_suites", return_value=[]), \
            mock.patch.object(ci_ok, "fetch", return_value=[run("build", conclusion="failure"),
                                                          run("tests", "queued", None)]) as fetch, \
            mock.patch.object(ci_ok.time, "sleep") as sleep, contextlib.redirect_stdout(output):
        assert ci_ok.main() == 1
    assert fetch.call_count == 1
    assert sleep.call_args_list == [mock.call(0)]
    assert "::error::failed checks: build=failure" in output.getvalue()


@pytest.mark.parametrize("conclusion", ["success", "skipped", "neutral"])
def test_existing_passing_conclusions_are_unchanged(conclusion):
    assert ci_ok.evaluate([run("build", conclusion=conclusion)], set()) == ("pass", ["build"])


def test_empty_inventory_retains_fail_closed_protection():
    assert ci_ok.evaluate([], set()) == ("fail", ["no other check ran on this commit"])


def test_self_and_advisory_exclusions_are_unchanged():
    checks = [run("ci-ok", "in_progress", None), run("advisory", conclusion="failure"), run("build")]
    assert ci_ok.evaluate(checks, {"ci-ok", "advisory"}) == ("pass", ["build"])


def test_non_actions_checks_remain_optional():
    external = dict(run("preview", conclusion="failure"), app={"slug": "external-app"})
    checks = [run("build"), external]
    assert ci_ok.evaluate(checks, set()) == ("pass", ["build"])
    assert ci_ok.evaluate(checks, set(), actions_only=False) == ("fail", ["preview=failure"])


def test_names_all_current_failures_in_sorted_order():
    assert ci_ok.evaluate([run("z", conclusion="failure"), run("a", conclusion="cancelled"),
                           run("pending", "in_progress", None)], set()) == (
        "fail", ["a=cancelled", "z=failure"])


def test_nonterminal_conclusion_does_not_create_failure():
    assert ci_ok.evaluate([run("build", "in_progress", "failure")], set()) == ("pending", ["build"])


def poll(snapshots):
    """Drive the real main loop with a deterministic clock and API snapshots."""
    env = {"REPO": "owner/repo", "SHA": "head", "TOKEN": "test", "CI_OK_SETTLE": "0",
           "CI_OK_INTERVAL": "20", "CI_OK_TIMEOUT_MINUTES": "2"}
    clock = [0]
    output = io.StringIO()
    with mock.patch.dict(ci_ok.os.environ, env, clear=True), \
            mock.patch.object(ci_ok.time, "time", side_effect=lambda: clock[0]), \
            mock.patch.object(ci_ok.time, "sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)), \
            mock.patch.object(ci_ok, "fetch_suites", return_value=[]), \
            mock.patch.object(ci_ok, "fetch", side_effect=snapshots) as fetch, \
            contextlib.redirect_stdout(output):
        result = ci_ok.main()
    return result, fetch.call_count, clock[0], output.getvalue()


def test_failure_arriving_after_pending_polls_fails_on_that_poll():
    pending = [run("build", "in_progress", None), run("tests", "queued", None)]
    failed = [run("build", conclusion="failure"), run("tests", "queued", None)]
    result, calls, elapsed, output = poll([pending, pending, failed])
    assert (result, calls, elapsed) == (1, 3, 40)
    assert output.count("waiting on:") == 2
    assert "::error::failed checks: build=failure" in output
    assert "all checks passed:" not in output


def test_success_still_requires_two_complete_stable_polls():
    pending = [run("build"), run("tests", "in_progress", None)]
    passed = [run("build"), run("tests")]
    result, calls, elapsed, output = poll([pending, pending, passed, passed])
    assert (result, calls, elapsed) == (0, 4, 60)
    assert output.count("waiting on:") == 2
    assert "all checks passed: build, tests" in output


def test_a_new_pending_check_resets_success_stability():
    first = [run("build")]
    pending = [run("build"), run("tests", "queued", None)]
    passed = [run("build"), run("tests")]
    assert poll([first, pending, passed, passed])[:3] == (0, 4, 60)


def test_a_changed_complete_inventory_resets_success_stability():
    first = [run("build")]
    passed = [run("build"), run("tests")]
    assert poll([first, passed, passed])[:3] == (0, 3, 40)


def test_a_completed_failure_does_not_wait_for_a_second_complete_poll():
    assert poll([[run("build", conclusion="failure")]])[:3] == (1, 1, 0)


class ExistingPollingProtectionsTest(unittest.TestCase):
    def test_action_manifests_parse(self) -> None:
        import yaml  # every shared action.yml must be valid YAML for the runner
        root = pathlib.Path(__file__).resolve().parents[1] / ".github" / "actions"
        for manifest in root.glob("*/action.yml"):
            self.assertIsInstance(yaml.safe_load(manifest.read_text()), dict, manifest)

    def test_pending_until_all_complete(self) -> None:
        state, detail = ci_ok.evaluate([run("a"), run("b", "in_progress", None)], {"ci-ok"})
        self.assertEqual((state, detail), ("pending", ["b"]))

    def test_passes_with_success_skipped_neutral(self) -> None:
        runs = [run("a"), run("b", conclusion="skipped"), run("c", conclusion="neutral")]
        self.assertEqual(ci_ok.evaluate(runs, set())[0], "pass")

    def test_fails_on_failure_or_cancel(self) -> None:
        for bad in ("failure", "cancelled", "timed_out", "action_required", "startup_failure"):
            state, detail = ci_ok.evaluate([run("a"), run("b", conclusion=bad)], set())
            self.assertEqual(state, "fail", bad)
            self.assertEqual(detail, [f"b={bad}"])

    def test_ignores_itself_and_listed_checks(self) -> None:
        runs = [run("ci-ok", "in_progress", None), run("plain-language", conclusion="failure"), run("a")]
        self.assertEqual(ci_ok.evaluate(runs, {"ci-ok", "plain-language"})[0], "pass")

    def test_newer_run_of_the_same_check_replaces_an_older_suite_failure(self) -> None:
        old = {"id": 1, "name": "check", "status": "completed", "conclusion": "failure", "check_suite": {"id": 1}}
        new = {"id": 2, "name": "check", "status": "completed", "conclusion": "success", "check_suite": {"id": 2}}
        self.assertEqual(ci_ok.evaluate([old, new], set(), suite_events={1: "superseded", 2: "pull_request"})[0], "pass")
        self.assertEqual(ci_ok.evaluate([new, old], set(), suite_events={1: "superseded", 2: "pull_request"})[0], "pass")
        rerun = {"id": 3, "name": "check", "status": "in_progress", "conclusion": None}
        self.assertEqual(ci_ok.evaluate([old, new, rerun], set()), ("fail", ["check=failure"]))

    def test_other_apps_do_not_gate_by_default(self) -> None:
        deploy = {"name": "preview", "status": "in_progress", "conclusion": None, "app": {"slug": "external-app"}}
        mine = {"name": "a", "status": "completed", "conclusion": "success", "app": {"slug": "github-actions"}}
        self.assertEqual(ci_ok.evaluate([mine, deploy], set())[0], "pass")
        self.assertEqual(ci_ok.evaluate([mine, deploy], set(), actions_only=False)[0], "pending")

    def test_zero_other_checks_fails(self) -> None:
        state, detail = ci_ok.evaluate([run("ci-ok", "in_progress", None)], {"ci-ok"})
        self.assertEqual((state, detail), ("fail", ["no other check ran on this commit"]))
        self.assertEqual(ci_ok.evaluate([], {"ci-ok"}, allow_none=True), ("pass", []))

    def test_workflow_that_failed_to_start_fails(self) -> None:
        runs = [run("plain-language"), run("game-standard")]
        own = {"id": 1, "status": "in_progress", "conclusion": None, "latest_check_runs_count": 1}
        ok = {"id": 2, "status": "completed", "conclusion": "success", "latest_check_runs_count": 1}
        for bad in ("failure", "startup_failure", "cancelled", "action_required", "timed_out"):
            broken = {"id": 3, "status": "completed", "conclusion": bad, "latest_check_runs_count": 0}
            state, detail = ci_ok.evaluate(runs, {"ci-ok"}, suites=[own, ok, broken])
            self.assertEqual(state, "fail", bad)
            self.assertIn("workflow failed to start", detail[0])
        self.assertEqual(ci_ok.evaluate(runs, {"ci-ok"}, suites=[own, ok])[0], "pass")

    def test_non_gating_workflow_that_failed_to_start_is_ignored(self) -> None:
        runs = [run("a")]
        broken = {"id": 7, "status": "completed", "conclusion": "failure", "latest_check_runs_count": 0}
        self.assertEqual(ci_ok.evaluate(runs, set(), suites=[broken], suite_events={7: "workflow_run"})[0], "pass")
        self.assertEqual(ci_ok.evaluate(runs, set(), suites=[broken], suite_events={7: "push"})[0], "pass")
        self.assertEqual(ci_ok.evaluate(runs, set(), suites=[broken], suite_events={7: "merge_group"})[0], "fail")
        self.assertEqual(ci_ok.evaluate(runs, set(), suites=[broken], suite_events={})[0], "fail")
        self.assertEqual(ci_ok.evaluate(runs, set(), suites=[broken], suite_events=None)[0], "fail")

    def test_push_startup_rejection_fails_pr_but_not_merge_group(self) -> None:
        broken = {"id": 7, "status": "completed", "conclusion": "failure", "latest_check_runs_count": 0}
        for event in ("pull_request", "pull_request_target"):
            state, detail = ci_ok.evaluate([run("control")], set(), suites=[broken],
                                            suite_events={7: "push"}, event_name=event,
                                            suite_names={7: ".github/workflows/ci.yml"})
            self.assertEqual(state, "fail")
            self.assertIn(".github/workflows/ci.yml", detail[0])
        self.assertEqual(ci_ok.evaluate([run("control")], set(), suites=[broken],
                                        suite_events={7: "push"}, event_name="merge_group")[0], "pass")
        self.assertEqual(ci_ok.evaluate([run("control")], set(), suites=[broken],
                                        suite_events={7: "superseded"}, event_name="pull_request")[0], "pass")
        self.assertEqual(ci_ok.evaluate([run("control")], set(), suites=[broken],
                                        suite_events={7: "workflow_run"}, event_name="pull_request")[0], "pass")

    def test_pr_main_reports_rejected_workflow_path(self) -> None:
        broken = {"id": 7, "status": "completed", "conclusion": "failure", "latest_check_runs_count": 0}
        workflow = {"check_suite_id": 7, "path": ".github/workflows/ci.yml", "event": "push"}
        output = io.StringIO()
        with mock.patch.dict(ci_ok.os.environ, {"REPO": "owner/repo", "SHA": "head", "TOKEN": "test",
                                                "EVENT_NAME": "pull_request"}, clear=True), \
                mock.patch.object(ci_ok, "fetch_suites", return_value=[broken]), \
                mock.patch.object(ci_ok, "fetch", return_value=[run("control")]), \
                mock.patch.object(ci_ok, "_get", return_value={"workflow_runs": [workflow]}), \
                mock.patch.object(ci_ok.time, "sleep"), contextlib.redirect_stdout(output):
            self.assertEqual(ci_ok.main(), 1)
        self.assertIn("::error::failed checks: workflow failed to start", output.getvalue())
        self.assertIn(".github/workflows/ci.yml", output.getvalue())

    def test_startup_failure_superseded_by_a_later_run_is_ignored(self) -> None:
        wr = [{"check_suite_id": 1, "path": ".github/workflows/ci.yml", "event": "pull_request", "created_at": "2026-10-02T16:23:23Z"},
              {"check_suite_id": 2, "path": ".github/workflows/ci.yml", "event": "pull_request", "created_at": "2026-10-03T00:08:00Z"}]
        events = ci_ok.suite_events(wr)
        self.assertEqual(events, {1: "superseded", 2: "pull_request"})
        broken = {"id": 1, "status": "completed", "conclusion": "failure", "latest_check_runs_count": 0}
        later = {"id": 2, "status": "completed", "conclusion": "success", "latest_check_runs_count": 6}
        self.assertEqual(ci_ok.evaluate([run("server")], set(), suites=[broken, later], suite_events=events)[0], "pass")
        events = ci_ok.suite_events([dict(wr[0], check_suite_id=2), dict(wr[1], check_suite_id=1)])
        self.assertEqual(events, {2: "superseded", 1: "pull_request"})
        self.assertEqual(ci_ok.evaluate([run("server")], set(), suites=[dict(broken, id=1), dict(later, id=2)],
                                        suite_events=events)[0], "fail")

    def test_queued_empty_suite_neither_blocks_nor_fails(self) -> None:
        queued = {"id": 4, "status": "queued", "conclusion": None, "latest_check_runs_count": 0}
        self.assertEqual(ci_ok.evaluate([run("a")], set(), suites=[queued])[0], "pass")

    def test_empty_unfinished_suites_need_workflow_events(self) -> None:
        for status in ("queued", "in_progress"):
            empty = {"id": 2, "status": status, "conclusion": None, "latest_check_runs_count": 0}
            self.assertTrue(ci_ok.needs_workflow_events([empty]))
            self.assertFalse(ci_ok.needs_workflow_events([dict(empty, latest_check_runs_count=1)]))
        self.assertTrue(ci_ok.needs_workflow_events([
            {"status": "completed", "conclusion": "failure", "latest_check_runs_count": 0}]))
        self.assertFalse(ci_ok.needs_workflow_events([
            {"status": "completed", "conclusion": "success", "latest_check_runs_count": 0}]))

    def test_empty_gating_replacement_waits_until_it_completes(self) -> None:
        broken = {"id": 1, "status": "completed", "conclusion": "failure", "latest_check_runs_count": 0}
        replacement = {"id": 2, "status": "queued", "conclusion": None, "latest_check_runs_count": 0}
        for event in ci_ok.GATING_EVENTS:
            wr = [{"check_suite_id": 1, "path": ".github/workflows/ci.yml", "event": event,
                   "created_at": "2026-10-08T10:00:00Z"},
                  {"check_suite_id": 2, "path": ".github/workflows/ci.yml", "event": event,
                   "created_at": "2026-10-08T10:01:00Z"}]
            events = ci_ok.suite_events(wr)
            for status in ("queued", "in_progress"):
                self.assertEqual(ci_ok.evaluate([run("a")], set(),
                                               suites=[broken, dict(replacement, status=status)],
                                               suite_events=events),
                                 ("pending", ["workflow has no jobs yet (check suite 2)"]))
            self.assertEqual(ci_ok.evaluate([run("a")], set(),
                                           suites=[broken, dict(replacement, status="completed", conclusion="failure")],
                                           suite_events=events)[0], "fail")
            self.assertEqual(ci_ok.evaluate([run("a"), run("build")], set(),
                                           suites=[broken, dict(replacement, status="completed", conclusion="success",
                                                                latest_check_runs_count=1)],
                                           suite_events=events)[0], "pass")
        for event in ("push", "workflow_run", "superseded"):
            self.assertEqual(ci_ok.evaluate([run("a")], set(), suites=[replacement],
                                           suite_events={2: event})[0], "pass")

    def test_two_stable_polls_cannot_pass_an_empty_gating_replacement(self) -> None:
        import contextlib
        import io
        from unittest import mock

        broken = {"id": 1, "status": "completed", "conclusion": "failure", "latest_check_runs_count": 0}
        replacement = {"id": 2, "status": "queued", "conclusion": None, "latest_check_runs_count": 0}
        completed = dict(replacement, status="completed", conclusion="success", latest_check_runs_count=1)
        env = {"REPO": "o/r", "SHA": "abc", "TOKEN": "t", "CI_OK_SETTLE": "0", "CI_OK_INTERVAL": "1",
               "CI_OK_TIMEOUT_MINUTES": "1"}
        clock = [0.0]
        out = io.StringIO()
        with mock.patch.dict(ci_ok.os.environ, env, clear=True), \
                mock.patch.object(ci_ok.time, "time", side_effect=lambda: clock[0]), \
                mock.patch.object(ci_ok.time, "sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)), \
                mock.patch.object(ci_ok, "fetch_suites", side_effect=[[broken, replacement], [broken, replacement],
                                                                   [broken, completed], [broken, completed]]) as suites, \
                mock.patch.object(ci_ok, "fetch", return_value=[run("a")]), \
                mock.patch.object(ci_ok, "fetch_suite_events", return_value={1: "superseded", 2: "pull_request"}), \
                contextlib.redirect_stdout(out):
            self.assertEqual(ci_ok.main(), 0)
        self.assertEqual(suites.call_count, 4)
        self.assertEqual(out.getvalue().count("waiting on:"), 2)
        self.assertEqual(out.getvalue().count("all checks passed:"), 1)

    def test_failed_suite_with_jobs_is_judged_by_its_check_runs(self) -> None:
        rerun = {"id": 5, "status": "completed", "conclusion": "failure", "latest_check_runs_count": 2}
        self.assertEqual(ci_ok.evaluate([run("a")], set(), suites=[rerun])[0], "pass")

    def test_required_checks_must_succeed(self) -> None:
        runs = [run("a"), run("b", conclusion="skipped")]
        self.assertEqual(ci_ok.evaluate(runs, set(), required={"a"})[0], "pass")
        state, detail = ci_ok.evaluate(runs, set(), required={"a", "b", "c"})
        self.assertEqual((state, detail), ("fail", ["b=skipped (required)", "c=missing (required)"]))

    def test_cancelled_run_superseded_by_a_later_run_of_the_same_check_passes(self) -> None:
        old = dict(run("plain-language", conclusion="cancelled"), id=10, check_suite={"id": 1},
                   started_at="2026-10-04T18:50:00Z")
        new = dict(run("plain-language"), id=20, check_suite={"id": 2}, started_at="2026-10-04T19:55:00Z")
        wr = [{"check_suite_id": 1, "path": ".github/workflows/plain.yml", "event": "pull_request",
               "created_at": "2026-10-04T18:50:00Z"},
              {"check_suite_id": 2, "path": ".github/workflows/plain.yml", "event": "pull_request",
               "created_at": "2026-10-04T19:55:00Z"}]
        events = ci_ok.suite_events(wr)
        self.assertEqual(ci_ok.evaluate([old, new, run("a")], set(), suite_events=events),
                         ("pass", ["a", "plain-language"]))
        self.assertEqual(ci_ok.evaluate([old, new, run("a")], set())[0], "pass")
        self.assertEqual(ci_ok.evaluate([dict(old, conclusion="success"), dict(new, conclusion="failure")],
                                        set(), suite_events=events)[0], "fail")
        self.assertTrue(ci_ok.needs_suite_events([old, new], set()))
        self.assertFalse(ci_ok.needs_suite_events([new, run("a")], set()))

    def test_same_named_jobs_of_two_live_workflows_both_count(self) -> None:
        a = dict(run("build", conclusion="failure"), id=1, check_suite={"id": 1}, started_at="2026-10-04T18:00:00Z")
        b = dict(run("build"), id=2, check_suite={"id": 2}, started_at="2026-10-04T18:01:00Z")
        wr = [{"check_suite_id": 1, "path": ".github/workflows/ci.yml", "event": "pull_request",
               "created_at": "2026-10-04T18:00:00Z"},
              {"check_suite_id": 2, "path": ".github/workflows/web.yml", "event": "pull_request",
               "created_at": "2026-10-04T18:01:00Z"}]
        self.assertEqual(ci_ok.evaluate([a, b], set(), suite_events=ci_ok.suite_events(wr))[0], "fail")
        self.assertEqual(ci_ok.evaluate([a, b], set())[0], "fail")

    def test_same_second_runs_of_the_same_workflow_supersede_by_id(self) -> None:
        old = dict(run("shared-path-window", conclusion="cancelled"), id=10, check_suite={"id": 1},
                   started_at="2026-11-12T02:05:36Z")
        new = dict(run("shared-path-window"), id=20, check_suite={"id": 2},
                   started_at="2026-11-12T02:05:36Z")
        wr = [{"check_suite_id": 1, "id": 43138971656, "path": ".github/workflows/shared-path-window.yml", "event": "pull_request",
               "created_at": "2026-11-12T02:05:36Z"},
              {"check_suite_id": 2, "id": 43138972745, "path": ".github/workflows/shared-path-window.yml", "event": "pull_request",
               "created_at": "2026-11-12T02:05:36Z"}]
        expected = {1: "superseded", 2: "pull_request"}
        for ordered in (wr, list(reversed(wr))):
            events = ci_ok.suite_events(ordered)
            self.assertEqual(events, expected)
            self.assertEqual(ci_ok.evaluate([old, new, run("a")], set(), suite_events=events),
                             ("pass", ["a", "shared-path-window"]))

    def test_exhausted_api_budget_fails_closed_before_the_deadline(self) -> None:
        import io, email.message, urllib.error
        headers = email.message.Message()
        headers["x-ratelimit-remaining"] = "0"
        headers["x-ratelimit-reset"] = "5000"
        err = urllib.error.HTTPError("https://api.github.com/x", 403, "rate limit", headers, io.BytesIO(b""))
        limited = ci_ok.rate_limited(err, now=1000.0)
        self.assertEqual(limited, 5000.0)
        other = urllib.error.HTTPError("https://api.github.com/x", 403, "forbidden", email.message.Message(),
                                       io.BytesIO(b""))
        self.assertIsNone(ci_ok.rate_limited(other, now=1000.0))
        retry = email.message.Message()
        retry["retry-after"] = "60"
        self.assertEqual(ci_ok.rate_limited(
            urllib.error.HTTPError("u", 429, "slow down", retry, io.BytesIO(b"")), now=1000.0), 1060.0)

        calls = []

        def fetch_limited(*_a, **_k):
            calls.append(1)
            raise ci_ok.RateLimited(ci_ok.time.time() + 86400)

        env = {"REPO": "o/r", "SHA": "abc", "TOKEN": "t", "CI_OK_SETTLE": "0", "CI_OK_INTERVAL": "0",
               "CI_OK_TIMEOUT_MINUTES": "1"}
        out = io.StringIO()
        from unittest import mock
        import contextlib
        with mock.patch.dict(ci_ok.os.environ, env), mock.patch.object(ci_ok, "fetch_suites", fetch_limited), \
                contextlib.redirect_stdout(out):
            self.assertEqual(ci_ok.main(), 1)
        self.assertEqual(len(calls), 1)
        self.assertIn("rate limit", out.getvalue())

    def test_low_api_budget_slows_polling(self) -> None:
        self.assertEqual(ci_ok.poll_interval(20, remaining=5000), 20)
        self.assertEqual(ci_ok.poll_interval(20, remaining=None), 20)
        self.assertEqual(ci_ok.poll_interval(20, remaining=150), 60)




@pytest.mark.parametrize("conclusion", ["skipped", "neutral", None])
def test_completed_required_non_success_fails_while_others_are_pending(conclusion):
    assert ci_ok.evaluate([run("required-check", conclusion=conclusion), run("tests", "queued", None)],
                           set(), required={"required-check"}) == (
        "fail", [f"required-check={conclusion or 'missing'} (required)"])


def test_empty_startup_failure_wins_over_pending_checks():
    suite = {"id": 7, "status": "completed", "conclusion": "failure", "latest_check_runs_count": 0}
    state, detail = ci_ok.evaluate([run("tests", "queued", None)], set(), suites=[suite])
    assert state == "fail"
    assert "workflow failed to start (check suite 7" in detail[0]
