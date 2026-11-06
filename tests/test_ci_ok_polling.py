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


def test_missing_expected_check_waits_instead_of_passing():
    assert ci_ok.evaluate([run("build")], set(), expected={"build", "tests"}) == (
        "pending", ["tests (not registered)"])


def test_cli_exits_on_first_failure_poll_without_stability_delay():
    env = {"REPO": "owner/repo", "SHA": "head", "TOKEN": "test", "CI_OK_EXPECTED": "build,tests",
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
