#!/usr/bin/env python3
"""Stop the line: while the trunk is red and its way back is in motion, the merge queue admits only the revert or the fix.

Andon for the merge queue (docs/optimistic-merge.md). It runs in a merge group
only. The trunk is red when the newest conclusive push run of the verify
workflow on the trunk failed; a later success clears it. A cancelled, skipped
or superseded run is not an answer and is passed over (owner standards/dx.md:
a red trunk counts only a completed failure of the verifying run).

While red, a merge group is admitted when its pull request is the way back to
green: a revert (branch `auto-revert/*`, a title starting `Revert`, or the
red-main handler's `auto-revert` / `queue-jump:red-main` labels), a live-outage
fix (`queue-jump:outage`, which the handler never reverts either) or a pull
request labelled as the fix.

Any other pull request is refused only while the way back is in motion: a newer
run of the verify workflow on the trunk is still running, or a revert or fix
pull request is open. Otherwise it is admitted with a warning. The red-main
handler reverts only after the same failure repeats on a second completed run,
and never reverts an infrastructure failure, a timed-out run or a run that did
not start; a queue that stayed shut on the first red would wait for a run that
nothing can start. The next change landing is what lets the handler decide. A
person can always pass a change with the fix label.

`mode: watch` is the other half of the andon: on a schedule in the caller's
ci.yml, it alerts when the trunk has stayed red past `watch-limit-minutes`
(30) WHATEVER the handler's verdict was. The handler reports a real failure by
tracing a culprit and reverting, and an infrastructure failure by reporting it
and never reverting; either report lands in a run's summary and an issue a
person may never open, and a trunk once stayed red for three days with every
reader taking the passing handler run as healthy. Watch files ONE issue per
repository, labelled `red-main-red-watchdog`, and comments on it for each
further red window. It reverts nothing and dispatches nothing.

Fail open: a state that cannot be read (API error, no verify run in sight)
admits, with a warning. A gate that jams the queue on its own fault is worse
than the red it guards against.
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.error
import urllib.request

FAILED = {"failure", "timed_out", "action_required", "startup_failure"}
REVERT_LABELS = {"auto-revert", "queue-jump:red-main"}
OUTAGE_LABEL = "queue-jump:outage"
WATCH_LABEL = "red-main-red-watchdog"
WATCH_INFRA_LABEL = "red-main-infra"
# The ONE classifier that owns what an infrastructure failure is; the repair
# handler runs the same file, so the two readers cannot drift.
CLASSIFIER = pathlib.Path(__file__).resolve().parents[2] / "red_main" / "infra.py"
MERGE_GROUP_PR = re.compile(r"^(?:refs/heads/)?gh-readonly-queue/[^/].*?/pr-(\d+)-[0-9a-f]{7,40}$")


def trunk_state(runs: list[dict], branch: str) -> tuple[str, dict | None]:
    """('red'|'green'|'unknown', the deciding run) from push runs, newest first."""
    for run in runs:
        if run.get("event") != "push" or run.get("head_branch") != branch or run.get("status") != "completed":
            continue
        conclusion = run.get("conclusion")
        if conclusion in FAILED:
            return "red", run
        if conclusion == "success":
            return "green", run
        # cancelled, skipped, neutral, stale: superseded or not an answer
    return "unknown", None


def newer_unfinished_run(runs: list[dict], branch: str, deciding: dict | None) -> dict | None:
    """The newest push run on the trunk that has not finished and is newer than `deciding` (runs are newest first)."""
    for run in runs:
        if run is deciding:
            return None
        if run.get("event") == "push" and run.get("head_branch") == branch and run.get("status") != "completed":
            return run
    return None


def pr_number(head_ref: str) -> int | None:
    match = MERGE_GROUP_PR.match(head_ref or "")
    return int(match.group(1)) if match else None


def is_way_back(pull: dict, fix_label: str) -> str | None:
    """Why this pull request is a revert, an outage fix or the fix, or None."""
    labels = {(label.get("name") or "") for label in pull.get("labels", [])}
    head = (pull.get("head") or {}).get("ref") or ""
    title = (pull.get("title") or "").strip()
    if head.startswith("auto-revert/") or labels & REVERT_LABELS:
        return "revert from the red-main handler"
    if title.lower().startswith("revert"):
        return "revert"
    if OUTAGE_LABEL in labels:
        return "live-outage fix"
    if fix_label and fix_label in labels:
        return f"labelled {fix_label}"
    return None


def open_way_back(pulls: list[dict], fix_label: str) -> str | None:
    """Describe an open, non-draft revert or fix pull request (a draft cannot be queued), or None."""
    for pull in pulls:
        why = None if pull.get("draft") else is_way_back(pull, fix_label)
        if why:
            return f"pull request #{pull.get('number')} ({why}) is open"
    return None


def decide(state: str, pull: dict | None, mode: str, fix_label: str, motion: str | None) -> tuple[bool, str]:
    """(admit, message). `pull` None means the pull request could not be read; `motion` says what is already
    on its way to green (a running verify run, an open revert or fix), or None when nothing is."""
    if state != "red":
        return True, f"trunk is {state}: admitted"
    if pull is None:
        return True, "::warning::trunk is red but the pull request could not be read: admitted"
    why = is_way_back(pull, fix_label)
    if why:
        return True, f"trunk is red; this pull request is a way back to green ({why}): admitted"
    if motion is None:
        return True, ("::warning::trunk is red but nothing is on its way to green (no newer Verify run is running "
                      f"and no revert or `{fix_label}` pull request is open): admitted, so that the next completed "
                      "run can confirm the failure and let the red-main handler act")
    message = (f"trunk is red and {motion}: only a revert or a pull request labelled `{fix_label}` is admitted until "
               f"Verify is green again; this pull request is neither. Re-enqueue it once the trunk is green.")
    if mode != "enforce":
        return True, f"::warning::observe mode, would have refused: {message}"
    return False, f"::error::{message}"


def _get(url: str, token: str) -> dict | list:
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    return json.load(urllib.request.urlopen(req, timeout=30))


def _post(url: str, token: str, payload: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST", headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=30))


def red_minutes(run: dict, now: datetime.datetime) -> float | None:
    """How long the decisive red run has been red, or None when its age is unreadable."""
    stamp = run.get("created_at")
    if not isinstance(stamp, str) or not stamp:
        return None
    at = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if at.tzinfo is None:
        return None
    return (now - at).total_seconds() / 60.0


def watch_verdict(repo: str, run: dict, token: str) -> str:
    """`infra <signature>` or `none` for the red run, from the ONE classifier that owns the rule.

    The watchdog has no verdict of its own, so it asks red_main/infra.py - the
    file the repair handler runs - with the same job payload the handler reads,
    rather than guessing from the run's conclusion. The classification is what
    decides which owner the alert reaches: the platform owner for a red the
    platform caused, the repository owner for one a person must forward-fix.
    A read that fails escalates as a person's red: the alert still reaches
    someone rather than being dropped, the silent failure this watchdog exists
    to prevent.
    """
    try:
        jobs = _get(f"https://api.github.com/repos/{repo}/actions/runs/{run['id']}/jobs?per_page=100", token)
    except Exception as exc:
        print(f"::warning::watch: the run's jobs could not be read to classify it ({exc}): escalating it as a person's fix")
        return "none"
    payload = {"mode": "watchdog",
               "jobs": [{"id": j.get("id"), "name": j.get("name"), "conclusion": j.get("conclusion"),
                         "steps": j.get("steps", []),
                         "annotations": [a.get("message", "") for a in j.get("annotations", [])]}
                        for j in jobs.get("jobs", [])]}
    return classify_run(payload)


def classify_run(payload: dict) -> str:
    """Run red_main/infra.py's watchdog mode over one job payload; a person's red when it cannot be read."""
    try:
        done = subprocess.run([sys.executable, str(CLASSIFIER)], input=json.dumps(payload),
                              capture_output=True, text=True, timeout=60)
    except Exception:
        return "none"
    return done.stdout.strip() or "none"


def watch_alert(repo: str, branch: str, run: dict, minutes: float, token: str) -> str:
    """File or comment on the ONE watchdog issue for a trunk red past the limit; return the message."""
    api = f"https://api.github.com/repos/{repo}"
    verdict = watch_verdict(repo, run, token) if run.get("id") else "none"
    infra = verdict.startswith("infra")
    # A red the platform caused is not the repository owner's to forward-fix: it
    # goes to the infra owner under the label the repair handler's own escalation
    # uses, so one read of `red-main-infra` shows every platform-caused red.
    label = WATCH_INFRA_LABEL if infra else "owner:ops"
    label_description = ("red-main infrastructure escalation" if infra else "red-main watchdog alert")
    why = (f"The classifier reads it as infrastructure (`{verdict.removeprefix('infra ').strip()}`), "
           "which the red-main handler never reverts and no repository owner can forward-fix: "
           "the platform must recover or its owner must act. "
           if infra else
           "The classifier reads it as a failure a person must forward-fix; the red-main handler reverts "
           "only after the same unit fails on a second completed run. ")
    body = (f"The trunk `{branch}` of `{repo}` has been red for {minutes:.0f} minutes: "
            f"the newest conclusive {run.get('conclusion')} verify run is {run.get('html_url')} "
            f"on `{(run.get('head_sha') or '')[:9]}`. {why}"
            "A safety net that fails silently is worse than none. Open the run, read the failed step, "
            "and forward-fix it or report the platform failure to the infra owner.")
    try:
        _post(f"{api}/labels", token, {"name": WATCH_LABEL, "color": "b60205",
                                       "description": "red-main watchdog alert"})
        _post(f"{api}/labels", token, {"name": label, "color": "b60205",
                                       "description": label_description})
        open_issues = _get(f"{api}/issues?state=open&labels={WATCH_LABEL}&per_page=1", token)
        number = open_issues[0]["number"] if isinstance(open_issues, list) and open_issues else None
        if number:
            _post(f"{api}/issues/{number}/comments", token, {"body": body})
            return f"::error::trunk red {minutes:.0f} minutes; commented on the watchdog issue #{number}"
        created = _post(f"{api}/issues", token, {
            "title": f"{branch} is red past the watchdog limit", "body": body,
            "labels": [WATCH_LABEL, label]})
        return f"::error::trunk red {minutes:.0f} minutes; watchdog issue #{created.get('number')} opened for {label}"
    except urllib.error.HTTPError as exc:
        return f"::error::trunk red {minutes:.0f} minutes; the watchdog issue needs issues: write ({exc.code})"


def watch(env: dict, repo: str, token: str, branch: str, runs: list[dict], now: datetime.datetime) -> int:
    """Alert when the trunk has been red past the limit, whatever the handler's verdict was."""
    limit = float(env.get("WATCH_LIMIT_MINUTES", "30") or 30)
    state, run = trunk_state(runs, branch)
    if state != "red":
        print(f"watch: trunk is {state}: nothing to alert")
        return 0
    minutes = red_minutes(run, now)
    if minutes is None:
        print("::warning::watch: the red run's age could not be read: not alerting")
        return 0
    if minutes < limit:
        print(f"watch: trunk red {minutes:.0f} minutes (< {limit:.0f}): nothing to alert")
        return 0
    # A way back already in motion is the net working: do not page over it.
    try:
        pulls = _get(f"https://api.github.com/repos/{repo}/pulls?state=open&per_page=100", token)
        motion = open_way_back(pulls, env.get("FIX_LABEL", "main-red-fix"))
    except Exception as exc:
        print(f"::warning::watch: open pull requests could not be read ({exc}): alerting anyway")
        motion = None
    if motion:
        print(f"watch: trunk red {minutes:.0f} minutes but {motion}: nothing to alert")
        return 0
    print(watch_alert(repo, branch, run, minutes, token))
    return 0


def main() -> int:
    env = os.environ
    repo, token, branch = env["REPO"], env["TOKEN"], env.get("BASE_BRANCH", "main").removeprefix("refs/heads/")
    mode, fix_label = env.get("MODE", "enforce"), env.get("FIX_LABEL", "main-red-fix")
    api = f"https://api.github.com/repos/{repo}"
    if mode == "watch":
        # The scheduled half of the andon: no pull request, no merge group.
        try:
            data = _get(f"{api}/actions/workflows/{env['VERIFY_WORKFLOW']}/runs"
                        f"?event=push&branch={branch}&per_page=30", token)
        except Exception as exc:
            print(f"::warning::watch: cannot read {env['VERIFY_WORKFLOW']} runs ({exc}): not alerting")
            return 0
        return watch(env, repo, token, branch, data.get("workflow_runs", []),
                     datetime.datetime.now(datetime.timezone.utc))
    if env.get("EVENT_NAME") != "merge_group":
        print("not a merge group: nothing to gate")
        return 0
    try:
        data = _get(f"{api}/actions/workflows/{env['VERIFY_WORKFLOW']}/runs"
                    f"?event=push&branch={branch}&per_page=30", token)
        runs = data.get("workflow_runs", [])
        state, run = trunk_state(runs, branch)
    except Exception as exc:
        print(f"::warning::cannot read {env['VERIFY_WORKFLOW']} runs ({exc}): admitted")
        return 0
    if state != "red":
        print(f"trunk is {state}" + (f" (run {run['id']})" if run else "") + ": admitted")
        return 0
    print(f"trunk is red: {run.get('html_url')} on {run.get('head_sha', '')[:9]} concluded {run.get('conclusion')}")
    number = pr_number(env.get("MERGE_GROUP_REF", ""))
    pull = None
    if number is not None:
        try:
            pull = _get(f"{api}/pulls/{number}", token)
        except Exception as exc:
            print(f"::warning::cannot read pull request {number} ({exc})")
    motion = None
    if pull is not None and not is_way_back(pull, fix_label):
        running = newer_unfinished_run(runs, branch, run)
        if running:
            motion = f"a newer {env['VERIFY_WORKFLOW']} run is still running ({running.get('html_url')})"
        else:
            try:
                motion = open_way_back(_get(f"{api}/pulls?state=open&per_page=100", token), fix_label)
            except Exception as exc:
                print(f"::warning::cannot list open pull requests ({exc}): admitted")
                return 0
    admit, message = decide(state, pull, mode, fix_label, motion)
    print(message)
    return 0 if admit else 1


if __name__ == "__main__":
    sys.exit(main())
