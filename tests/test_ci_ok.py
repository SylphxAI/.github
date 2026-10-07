#!/usr/bin/env python3
"""Tests for the ci-ok aggregate gate's decision."""

from __future__ import annotations

import importlib.util
import pathlib
import unittest

SPEC = importlib.util.spec_from_file_location(
    "ci_ok", pathlib.Path(__file__).resolve().parents[1] / ".github" / "actions" / "ci-ok" / "ci_ok.py")
ci_ok = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci_ok)


def run(name: str, status: str = "completed", conclusion: str | None = "success") -> dict:
    return {"name": name, "status": status, "conclusion": conclusion}


class CiOkTest(unittest.TestCase):
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

    def test_other_apps_do_not_gate_by_default(self) -> None:
        deploy = {"name": "sylphx/deploy", "status": "in_progress", "conclusion": None, "app": {"slug": "sylphx-ai"}}
        mine = {"name": "a", "status": "completed", "conclusion": "success", "app": {"slug": "github-actions"}}
        self.assertEqual(ci_ok.evaluate([mine, deploy], set())[0], "pass")
        self.assertEqual(ci_ok.evaluate([mine, deploy], set(), actions_only=False)[0], "pending")

    def test_zero_other_checks_fails(self) -> None:
        state, detail = ci_ok.evaluate([run("ci-ok", "in_progress", None)], {"ci-ok"})
        self.assertEqual((state, detail), ("fail", ["no other check ran on this commit"]))
        self.assertEqual(ci_ok.evaluate([], {"ci-ok"}, allow_none=True), ("pass", []))

    def test_workflow_that_failed_to_start_fails(self) -> None:
        # Cubeage/voidbite-keel#1: ci.yml failed to start (a suite concluded
        # failure with 0 check runs) while the other workflows passed.
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
        # Cubeage/fun-big2-tw#280: a broken red-main.yml (workflow_run) posted
        # failed empty suites on the merge-group SHA; it is not a gate check.
        runs = [run("a")]
        broken = {"id": 7, "status": "completed", "conclusion": "failure", "latest_check_runs_count": 0}
        self.assertEqual(ci_ok.evaluate(runs, set(), suites=[broken], suite_events={7: "workflow_run"})[0], "pass")
        self.assertEqual(ci_ok.evaluate(runs, set(), suites=[broken], suite_events={7: "push"})[0], "pass")
        self.assertEqual(ci_ok.evaluate(runs, set(), suites=[broken], suite_events={7: "merge_group"})[0], "fail")
        self.assertEqual(ci_ok.evaluate(runs, set(), suites=[broken], suite_events={})[0], "fail")
        self.assertEqual(ci_ok.evaluate(runs, set(), suites=[broken], suite_events=None)[0], "fail")

    def test_startup_failure_superseded_by_a_later_run_is_ignored(self) -> None:
        # Cubeage/block-sort-keel#2: ci.yml failed to start at 16:23, then a
        # re-trigger ran it with 6 jobs at 00:08 on the same head SHA.
        wr = [{"check_suite_id": 1, "path": ".github/workflows/ci.yml", "event": "pull_request", "created_at": "2026-10-02T16:23:23Z"},
              {"check_suite_id": 2, "path": ".github/workflows/ci.yml", "event": "pull_request", "created_at": "2026-10-03T00:08:00Z"}]
        events = ci_ok.suite_events(wr)
        self.assertEqual(events, {1: "superseded", 2: "pull_request"})
        broken = {"id": 1, "status": "completed", "conclusion": "failure", "latest_check_runs_count": 0}
        later = {"id": 2, "status": "completed", "conclusion": "success", "latest_check_runs_count": 6}
        self.assertEqual(ci_ok.evaluate([run("server")], set(), suites=[broken, later], suite_events=events)[0], "pass")
        # The latest attempt failed to start: still a failure.
        events = ci_ok.suite_events([dict(wr[0], check_suite_id=2), dict(wr[1], check_suite_id=1)])
        self.assertEqual(events, {2: "superseded", 1: "pull_request"})
        self.assertEqual(ci_ok.evaluate([run("server")], set(), suites=[dict(broken, id=1), dict(later, id=2)],
                                        suite_events=events)[0], "fail")

    def test_queued_empty_suite_neither_blocks_nor_fails(self) -> None:
        queued = {"id": 4, "status": "queued", "conclusion": None, "latest_check_runs_count": 0}
        self.assertEqual(ci_ok.evaluate([run("a")], set(), suites=[queued])[0], "pass")

    def test_failed_suite_with_jobs_is_judged_by_its_check_runs(self) -> None:
        # A re-run that passed leaves the suite's latest check runs green.
        rerun = {"id": 5, "status": "completed", "conclusion": "failure", "latest_check_runs_count": 2}
        self.assertEqual(ci_ok.evaluate([run("a")], set(), suites=[rerun])[0], "pass")

    def test_required_checks_must_succeed(self) -> None:
        runs = [run("a"), run("b", conclusion="skipped")]
        self.assertEqual(ci_ok.evaluate(runs, set(), required={"a"})[0], "pass")
        state, detail = ci_ok.evaluate(runs, set(), required={"a", "b", "c"})
        self.assertEqual((state, detail), ("fail", ["b=skipped (required)", "c=missing (required)"]))

    def test_cancelled_run_superseded_by_a_later_run_of_the_same_check_passes(self) -> None:
        # Cubeage/big2-tycoon#15 @575a2304: plain-language was cancelled at
        # 18:53 by a newer run of its workflow, which succeeded at 19:58 on the
        # same head; the check-runs list returns both, and ci-ok failed twice.
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
        # Without `actions: read` the older cancelled run of the same name still yields.
        self.assertEqual(ci_ok.evaluate([old, new, run("a")], set())[0], "pass")
        # The newest run is what counts: a later failure still fails.
        self.assertEqual(ci_ok.evaluate([dict(old, conclusion="success"), dict(new, conclusion="failure")],
                                        set(), suite_events=events)[0], "fail")
        self.assertTrue(ci_ok.needs_suite_events([old, new], set()))
        self.assertFalse(ci_ok.needs_suite_events([new, run("a")], set()))

    def test_same_named_jobs_of_two_live_workflows_both_count(self) -> None:
        # Two workflows that each have a `build` job: neither is superseded.
        a = dict(run("build", conclusion="failure"), id=1, check_suite={"id": 1}, started_at="2026-10-04T18:00:00Z")
        b = dict(run("build"), id=2, check_suite={"id": 2}, started_at="2026-10-04T18:01:00Z")
        wr = [{"check_suite_id": 1, "path": ".github/workflows/ci.yml", "event": "pull_request",
               "created_at": "2026-10-04T18:00:00Z"},
              {"check_suite_id": 2, "path": ".github/workflows/web.yml", "event": "pull_request",
               "created_at": "2026-10-04T18:01:00Z"}]
        self.assertEqual(ci_ok.evaluate([a, b], set(), suite_events=ci_ok.suite_events(wr))[0], "fail")
        self.assertEqual(ci_ok.evaluate([a, b], set())[0], "fail")

    def test_exhausted_api_budget_fails_closed_before_the_deadline(self) -> None:
        # With the token's rate limit spent past the deadline, the gate fails
        # at once with a clear message instead of polling until it times out.
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


if __name__ == "__main__":
    unittest.main()
