"""Behavioral tests for the legacy cross-workflow completion boundary."""
from __future__ import annotations

import contextlib
import importlib.util
import io
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


def test_empty_inventory_keeps_legacy_success_behavior():
    assert ci_ok.evaluate([], set()) == ("pass", [])


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
