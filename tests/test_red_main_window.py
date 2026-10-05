#!/usr/bin/env python3
"""The red-main trace starts a failing unit's window at the run where that unit last did not fail.

A trunk that stays red for a long time must not stop the handler from tracing
a unit that breaks later: Keel main was red from 2026-10-03 17:07Z and every
handler run reported "67 unverified commits since 11ac6f715" and reverted
nothing, because the window began at the last fully verified commit.

The job lists are the 29 jobs recorded from SylphxAI/keel run 37172490395 (the
same fixture the confirmation tests use), with conclusions set per scenario;
run ids and the head commit follow Keel's verify run 37185701064 on 61b9f793e.
The scripts run here are the ones the workflow ships. Nothing touches the
network: `gh` is a local fixture server, and the trace step runs for real.
"""
import copy
import hashlib
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/red-main.yml"
HANDLER = yaml.safe_load(WORKFLOW.read_text())["jobs"]["red-main"]
ENV = HANDLER["env"]
STEPS = {step["name"]: step for step in HANDLER["steps"] if "name" in step}
TRACE = STEPS["Trace the culprit among the unverified commits"]["run"]
FIXTURES = ROOT / "tests/fixtures/red-main"
REPO = "SylphxAI/keel"
GPU, WEB, AGGREGATE = "Features (gpu)", "Web release and smoke", "verified"
OLD_TEST = "scene_3d::rounded_clip_keeps_the_corner"
NEW_TEST = "post_tilt_shift::device_tests::edges_match_the_reference"
FIRST_RUN = 37_172_000_000


def load(name, text):
    spec = importlib.util.spec_from_loader(name, loader=None)
    module = importlib.util.module_from_spec(spec)
    exec(compile(text, name, "exec"), module.__dict__)
    return module


def sha(n):
    return hashlib.sha1(f"keel-main-{n}".encode()).hexdigest()


def run_id(n):
    return FIRST_RUN + n


def at(n):
    return (datetime(2026, 10, 3, 17, 0, tzinfo=timezone.utc) + timedelta(minutes=n)).strftime("%Y-%m-%dT%H:%M:%SZ")


def history_row(n, result, **extra):
    row = dict(id=run_id(n), status="completed", conclusion=result, created_at=at(n),
               event="push", path=".github/workflows/verify.yml", workflow_id=2, head_branch="main",
               head_sha=sha(n), check_suite_id=3000 + n, run_attempt=1,
               repository=dict(id=4, full_name=REPO), head_repository=dict(id=4, full_name=REPO))
    row.update(extra)
    return row


def job_rows(rid, failed=(), skipped=()):
    template = json.loads((FIXTURES / "keel-verify-jobs-37172490395.json").read_text())["jobs"]
    rows = []
    for job in copy.deepcopy(template):
        job["run_id"] = rid
        job["check_run_url"] = f"https://api.github.com/repos/{REPO}/check-runs/{job['id']}"
        if job["name"] in failed or (job["name"] == AGGREGATE and failed):
            job["conclusion"] = "failure"
        elif job["name"] in skipped:
            job["conclusion"] = "skipped"
        else:
            job["conclusion"] = "success"
        job["steps"] = [{"name": "Test", "conclusion": job["conclusion"]}]
        rows.append(job)
    return rows


# What the local `gh` serves for every failed job's check run: the runner's
# own error line, so a failed lane fails at step "Test" with exit code 1.
EXIT_1 = [{"annotation_level": "failure", "message": "Process completed with exit code 1."}]


def lane(job):
    """The lane unit of a fixture job that failed at its "Test" step."""
    return ("lane", PROOF.lane_signature({"name": job, "conclusion": "failure",
                                          "steps": [{"name": "Test", "conclusion": "failure"}]}, EXIT_1), job)


def junit_xml(failing=(), passing=()):
    cases = []
    for full in passing:
        klass, _, name = full.rpartition("::")
        cases.append(f'<testcase classname="{klass}" name="{name}"/>')
    for full in failing:
        klass, _, name = full.rpartition("::")
        cases.append(f'<testcase classname="{klass}" name="{name}"><failure message="x"/></testcase>')
    return '<testsuite name="x">' + "".join(cases) + "</testsuite>"


class Scenario:
    """Verify runs c1..cN on main; one run per commit unless `missing`."""

    def __init__(self, last, missing=()):
        self.last = last
        self.history, self.jobs, self.junit = [], {}, {}
        self.missing = set(missing)

    def add(self, n, failed=(), junit=None, conclusion=None):
        if n in self.missing:
            return
        conclusion = conclusion or ("failure" if failed else "success")
        self.history.append(history_row(n, conclusion))
        self.jobs[str(run_id(n))] = job_rows(run_id(n), failed)
        if junit:
            self.junit[str(run_id(n))] = junit


def make_loader(scenario, downloads=None):
    def loader(run):
        mod_jobs = {}
        for row in scenario.jobs.get(str(run["id"]), []):
            mod_jobs[row["name"].replace(",", ";")] = row["conclusion"]

        def junit():
            if downloads is not None:
                downloads.append(run["id"])
            failing, passing, covered = set(), set(), set()
            for lane, xml in scenario.junit.get(str(run["id"]), {}).items():
                import xml.etree.ElementTree as ET
                for case in ET.fromstring(xml).iter("testcase"):
                    full = f'{case.get("classname")}::{case.get("name")}'
                    covered.add(lane)
                    (failing if list(case) else passing).add((full, lane))
            return failing, passing, covered
        return WINDOW.Evidence(mod_jobs, junit)
    return loader


PROOF = load("proof", (ROOT / ".github/actions/ci-range/post_main.py").read_text())
WINDOW = load("window", ENV["WINDOW_PY"])


def eligible(row):
    return PROOF.producer_origin(row, REPO, "main", "verify.yml") == "eligible"


class ClearRuleTest(unittest.TestCase):
    def evidence(self, jobs, failing=(), passing=(), covered=()):
        return WINDOW.Evidence(jobs, lambda: (set(failing), set(passing), set(covered)))

    def test_a_job_that_succeeded_clears_every_unit_of_it(self):
        ev = self.evidence({GPU: "success"})
        self.assertTrue(WINDOW.clears(("lane", GPU, GPU), ev))
        self.assertTrue(WINDOW.clears(("test", NEW_TEST, GPU), ev))

    def test_a_failed_lane_clears_nothing_and_a_skipped_or_absent_job_proves_nothing(self):
        self.assertFalse(WINDOW.clears(("lane", GPU, GPU), self.evidence({GPU: "failure"})))
        self.assertFalse(WINDOW.clears(("lane", GPU, GPU), self.evidence({GPU: "skipped"})))
        self.assertFalse(WINDOW.clears(("test", NEW_TEST, GPU), self.evidence({WEB: "success"})))
        self.assertFalse(WINDOW.clears(("test", NEW_TEST, GPU), self.evidence({GPU: "cancelled"})))

    def test_a_test_clears_when_the_failed_job_shows_it_passing(self):
        ev = self.evidence({GPU: "failure"}, failing=[(OLD_TEST, GPU)],
                           passing=[(NEW_TEST, GPU)], covered=[GPU])
        self.assertTrue(WINDOW.clears(("test", NEW_TEST, GPU), ev))
        self.assertFalse(WINDOW.clears(("test", OLD_TEST, GPU), ev))

    def test_a_test_the_failed_job_does_not_list_is_not_cleared_when_passes_are_listed(self):
        # A lane script stops at its first failing cargo command: a test in a
        # later command never ran, which is not a pass.
        ev = self.evidence({GPU: "failure"}, failing=[(OLD_TEST, GPU)],
                           passing=[("other::ok", GPU)], covered=[GPU])
        self.assertFalse(WINDOW.clears(("test", NEW_TEST, GPU), ev))

    def test_a_producer_that_lists_failures_only_clears_a_test_it_does_not_fail(self):
        ev = self.evidence({GPU: "failure"}, failing=[(OLD_TEST, GPU)], covered=[GPU])
        self.assertTrue(WINDOW.clears(("test", NEW_TEST, GPU), ev))

    def test_a_failed_job_without_junit_clears_no_test(self):
        self.assertFalse(WINDOW.clears(("test", NEW_TEST, GPU), self.evidence({GPU: "failure"})))


class RunSelectionTest(unittest.TestCase):
    def test_only_earlier_settled_genuine_push_runs_count_newest_first(self):
        rows = [
            history_row(1, "success"), history_row(2, "failure"), history_row(3, "cancelled"),
            history_row(4, "success", status="in_progress", conclusion=None),
            history_row(5, "failure", event="workflow_dispatch"),
            history_row(6, "failure", head_branch="sylphx-verify/x"),
            history_row(7, "timed_out"), history_row(9, "failure"),
        ]
        runs = WINDOW.earlier_runs(rows, at(8), eligible)
        self.assertEqual([r["id"] for r in runs], [run_id(7), run_id(2), run_id(1)])


class ContinuousRedTest(unittest.TestCase):
    """Lane A red for 60 commits, then lane B starts failing at commit 61."""

    def setUp(self):
        s = self.scenario = Scenario(70)
        for n in range(1, 71):
            failed = ([GPU] if n >= 3 else []) + ([WEB] if n >= 61 else [])
            s.add(n, failed)
        self.units = [("lane", GPU, GPU), ("lane", WEB, WEB), ("lane", AGGREGATE, AGGREGATE)]
        self.runs = WINDOW.earlier_runs(s.history, at(70), eligible)

    def test_the_window_is_the_commits_since_the_last_run_the_new_unit_passed(self):
        run, cleared, read = WINDOW.find_window(self.units, self.runs, make_loader(self.scenario))
        self.assertEqual(run["head_sha"], sha(60))
        self.assertEqual(cleared, [("lane", WEB, WEB)])
        self.assertEqual(read, 10)

    def test_a_unit_red_since_the_start_has_no_window_in_range(self):
        run, cleared, _ = WINDOW.find_window([("lane", GPU, GPU)], self.runs[:50], make_loader(self.scenario))
        self.assertIsNone(run)
        self.assertEqual(cleared, [])

    def test_the_walk_is_bounded(self):
        _, _, read = WINDOW.find_window([("lane", GPU, GPU)], self.runs, make_loader(self.scenario), limit=7)
        self.assertEqual(read, 7)


class TestLevelTest(unittest.TestCase):
    """The same lane carries an old failing test and a newly failing one."""

    def setUp(self):
        s = self.scenario = Scenario(70)
        others = [f"keel_render_wgpu::t{i}" for i in range(3)]
        for n in range(1, 71):
            if n < 3:
                s.add(n)
            elif n < 67:
                s.add(n, [GPU], junit={GPU: junit_xml([OLD_TEST], others + [NEW_TEST])})
            else:
                s.add(n, [GPU], junit={GPU: junit_xml([OLD_TEST, NEW_TEST], others)})
        self.units = [("test", OLD_TEST, GPU), ("test", NEW_TEST, GPU)]
        self.runs = WINDOW.earlier_runs(s.history, at(70), eligible)

    def test_a_test_that_starts_failing_inside_a_red_lane_gets_its_own_window(self):
        downloads = []
        run, cleared, _ = WINDOW.find_window(self.units, self.runs, make_loader(self.scenario, downloads))
        self.assertEqual(run["head_sha"], sha(66))
        self.assertEqual(cleared, [("test", NEW_TEST, GPU)])
        # junit is read once per run walked, never for a run whose job passed.
        self.assertEqual(len(downloads), 4)

    def test_without_junit_the_lane_gives_no_window(self):
        scenario = Scenario(70)
        for n in range(1, 71):
            scenario.add(n, [GPU] if n >= 3 else [])
        runs = WINDOW.earlier_runs(scenario.history, at(70), eligible)
        run, _, _ = WINDOW.find_window([("lane", GPU, GPU)], runs[:50], make_loader(scenario))
        self.assertIsNone(run)


class JudgeTest(unittest.TestCase):
    def test_a_candidate_fails_only_on_a_target_unit(self):
        target = [("test", NEW_TEST, GPU)]
        self.assertTrue(WINDOW.judge(target, [("test", OLD_TEST, GPU), ("test", NEW_TEST, GPU)]).startswith("fail "))
        self.assertTrue(WINDOW.judge([("lane", WEB, WEB)], [("test", OLD_TEST, GPU)]).startswith("pass "))
        self.assertTrue(WINDOW.judge([lane(WEB)], [lane(WEB)]).startswith("fail "))

    def test_a_target_job_that_failed_some_other_way_is_inconclusive(self):
        # A job that failed with no failing test (a build error), or with a
        # different test or step, may hide the target: it never counts as the
        # target failing, nor as the target passing.
        self.assertTrue(WINDOW.judge([("test", NEW_TEST, GPU)], [("test", OLD_TEST, GPU)]).startswith("pass "))
        self.assertTrue(WINDOW.judge([("test", NEW_TEST, GPU)], [lane(GPU)]).startswith("inconclusive "))
        self.assertTrue(WINDOW.judge([lane(WEB)], [("test", "a::b", WEB)]).startswith("inconclusive "))
        other_step = ("lane", lane(WEB)[1].replace("at Test", "at Serve"), WEB)
        self.assertTrue(WINDOW.judge([lane(WEB)], [other_step]).startswith("inconclusive "))
        # Any target failing the same way decides, whatever else is unclear.
        self.assertTrue(WINDOW.judge([lane(WEB), lane(GPU)], [other_step, lane(GPU)]).startswith("fail "))


class ConfirmedUnitsTest(unittest.TestCase):
    def confirmed(self, prev, cur):
        with tempfile.TemporaryDirectory() as d:
            files = []
            for name, rows in (("prev", prev), ("cur", cur)):
                path = Path(d, name)
                path.write_text("".join("\t".join(r) + "\n" for r in rows))
                files.append(str(path))
            out = Path(d, "out")
            script = Path(d, "confirm.py")
            script.write_text(ENV["CONFIRM_PY"])
            subprocess.run([sys.executable, str(script), "failure", *files, str(out)], check=True,
                           capture_output=True, text=True)
            return [tuple(line.split("\t")) for line in out.read_text().splitlines()]

    def test_only_units_the_previous_run_also_failed_are_confirmed(self):
        # The aggregate job fails whenever anything does: never a unit.
        prev = [("test", OLD_TEST, GPU), lane(WEB), ("lane", AGGREGATE, AGGREGATE)]
        cur = [("test", OLD_TEST, GPU), ("test", NEW_TEST, "Test"), lane(WEB), ("lane", AGGREGATE, AGGREGATE)]
        self.assertEqual(self.confirmed(prev, cur), [("test", OLD_TEST, GPU), lane(WEB)])

    def test_a_lane_row_confirms_nothing_that_failed_another_way(self):
        # A job with no junit on one run and a failing test on the other, or
        # one that failed at another step, is not the same failure.
        prev = [("lane", GPU, GPU), ("lane", lane(WEB)[1].replace("at Test", "at Serve"), WEB)]
        cur = [("test", NEW_TEST, GPU), ("test", "a::b", WEB), lane(WEB)]
        self.assertEqual(self.confirmed(prev, cur), [])


FAKE_GH = r'''
import json, os, pathlib, sys
fx = json.load(open(os.environ["FIXTURE"]))
temp = pathlib.Path(os.environ["RUNNER_TEMP"])
args = sys.argv[1:]
repo = fx["repo"]
def jq():
    return args[args.index("--jq") + 1] if "--jq" in args else ""
if args[0] == "run" and args[1] == "download":
    rid, dest = args[2], args[args.index("--dir") + 1]
    for lane, xml in fx["junit"].get(rid, {}).items():
        d = pathlib.Path(dest, "junit-" + lane)
        d.mkdir(parents=True, exist_ok=True)
        (d / "junit.xml").write_text(xml)
    if not fx["junit"].get(rid):
        print("no artifact matches", file=sys.stderr)
        sys.exit(1)
    sys.exit(0)
path = next(a for a in args if a.startswith("repos/"))
method = args[args.index("--method") + 1] if "--method" in args else "GET"
base = f"repos/{repo}/"
if path.startswith(base + "actions/workflows/verify.yml/runs?"):
    page = int(path.rsplit("page=", 1)[1])
    rows = fx["history"]
    print(json.dumps({"total_count": len(rows), "workflow_runs": rows[(page - 1) * 100: page * 100]}))
elif "/actions/runs/" in path and "/jobs" in path:
    rid = path.split("/actions/runs/")[1].split("/")[0]
    rows = fx["jobs"].get(rid)
    if rows is None:
        print("HTTP 404", file=sys.stderr); sys.exit(1)
    page = int(path.rsplit("page=", 1)[1]) if "page=" in path else 1
    print(json.dumps({"total_count": len(rows), "jobs": rows[(page - 1) * 100: page * 100]}))
elif path.startswith(base + "check-runs/") and "/annotations" in path:
    check = path[len(base + "check-runs/"):].split("/")[0]
    default = [{"annotation_level": "failure", "message": "Process completed with exit code 1."}]
    print(json.dumps(fx.get("annotations", {}).get(check, default) if path.endswith("page=1") else []))
elif path.startswith(base + "actions/runs/"):
    rid = path.rsplit("/", 1)[1]
    if jq() == ".created_at":
        print(fx["created"])
    else:
        print("completed\t" + fx["run_conclusion"][rid])
elif path.startswith(base + "compare/"):
    first, last = path[len(base + "compare/"):].split("...")
    chain = fx["chain"]
    commits = chain[chain.index(first) + 1: chain.index(last) + 1]
    if ".status" == jq():
        print("ahead" if commits else "identical")
    else:
        print(len(commits))
        for c in commits:
            print(c + "\tsubject " + c[-4:])
elif path == base + "git/refs" or path.startswith(base + "git/refs/"):
    pass
elif path.endswith("/dispatches"):
    payload = json.load(sys.stdin)
    with open(temp / "dispatch.log", "a") as fh:
        fh.write(json.dumps(payload) + "\n")
    print(fx["candidate_run"][payload["ref"].rsplit("/", 1)[1]])
else:
    print("unexpected provider command: " + repr(args), file=sys.stderr)
    sys.exit(1)
'''

WRAPPED_PROOF = (ROOT / ".github/actions/ci-range/post_main.py").read_text().replace(
    'if __name__ == "__main__":',
    'last_verified = lambda *a, **k: os.environ["VERIFIED_BASE"]\n\n\nif __name__ == "__main__":')


class TraceStepTest(unittest.TestCase):
    """The real trace step, with a local `gh`, on a trunk that is already red."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.work = self.root / "red-main"
        self.work.mkdir()
        for name, filename in (("LIB", "lib.sh"), ("JUNIT_PY", "junit.py"), ("CULPRIT_PY", "culprit.py"),
                               ("WINDOW_PY", "window.py"), ("CONFIRM_PY", "confirm.py"), ("INFRA_PY", "infra.py")):
            (self.work / filename).write_text(ENV[name])
        # The infra classifier has its own tests; here no candidate is an infra failure.
        with open(self.work / "lib.sh", "a") as fh:
            fh.write('\nclassify_run() { echo none; }\n')
        (self.work / "proof.py").write_text(WRAPPED_PROOF)
        binaries = self.root / "bin"
        binaries.mkdir()
        gh = binaries / "gh"
        gh.write_text(f"#!{sys.executable}\n" + FAKE_GH)
        gh.chmod(0o755)
        self.fixture = self.root / "fixture.json"

    def run_trace(self, scenario, confirmed, candidates, candidate_junit=None, verified_base=0, failed_lanes="",
                  mode="revert", annotations=None):
        chain = [sha(n) for n in range(0, scenario.last + 1)]
        candidate_run = {sha(n): str(9_000 + n) for n in range(1, scenario.last + 1)}
        jobs, junit = dict(scenario.jobs), dict(scenario.junit)
        conclusion = {}
        for n, (failed, xml) in candidates.items():
            rid = candidate_run[sha(n)]
            if failed and isinstance(failed[0], dict):  # recorded job rows
                jobs[rid] = [dict(row, run_id=int(rid)) for row in failed]
                failed = [row["name"] for row in failed if row["conclusion"] == "failure"]
            else:
                jobs[rid] = job_rows(int(rid), failed)
            conclusion[rid] = "failure" if failed else "success"
            if xml:
                junit[rid] = xml
        self.fixture.write_text(json.dumps(dict(
            repo=REPO, history=scenario.history, jobs=jobs, junit=junit, chain=chain,
            created=at(scenario.last), candidate_run=candidate_run, run_conclusion=conclusion,
            annotations=annotations or {})))
        (self.work / "confirmed-units.tsv").write_text("".join("\t".join(r) + "\n" for r in confirmed))
        env = dict(os.environ, RUNNER_TEMP=str(self.root), FIXTURE=str(self.fixture),
                   PATH=f"{self.root / 'bin'}:{os.environ['PATH']}", GITHUB_OUTPUT=str(self.root / "output"),
                   TRUNK="main", REPO=REPO, VERIFY_WORKFLOW="verify.yml", VERIFY_CHECK_NAME=AGGREGATE,
                   MAX_CANDIDATES="8", MAX_REVERT_COMMITS="250", LANE_INPUT="lanes",
                   CANDIDATE_TIMEOUT_MINUTES="20", REVERT_WINDOW="true", HEAD_SHA=sha(scenario.last),
                   RUN_ID=str(run_id(scenario.last)), RUN_URL="u", FAILED_LANES=failed_lanes,
                   TOKEN_MODE="no", MODE=mode, ACTIONS_TOKEN="fixture", GH_TOKEN="fixture",
                   VERIFIED_BASE=sha(verified_base))
        done = subprocess.run(["bash", "-c", TRACE], env=env, cwd=self.root, capture_output=True,
                              text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done

    def state(self, name):
        path = self.work / "state" / name
        return path.read_text().strip() if path.exists() else ""

    def summary(self):
        return (self.work / "summary.md").read_text()

    def dispatched(self):
        path = self.root / "dispatch.log"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_a_wide_window_starts_at_the_new_unit_and_is_reported_not_traced(self):
        s = Scenario(70)
        for n in range(1, 71):
            s.add(n, ([GPU] if n >= 3 else []) + ([WEB] if n >= 61 else []))
        confirmed = [lane(GPU), lane(WEB)]
        self.run_trace(s, confirmed, {}, failed_lanes=f"{GPU},{WEB},{AGGREGATE}")
        summary = self.summary()
        self.assertEqual(self.state("base"), sha(60))
        self.assertEqual(self.state("base-unit-run"), str(run_id(60)))
        self.assertIn("Per-unit baseline: 1 failing unit(s)", summary)
        self.assertIn(f"Unverified commits since `{sha(60)}`: 10.", summary)
        self.assertIn("More than 8 unverified commits", summary)

    def test_a_window_inside_the_candidate_limit_names_the_culprit(self):
        s = Scenario(70)
        for n in range(1, 71):
            s.add(n, ([GPU] if n >= 3 else []) + ([WEB] if n >= 65 else []))
        confirmed = [lane(GPU), lane(WEB)]
        candidates = {n: ([WEB] if n >= 65 else [], None) for n in range(65, 71)}
        self.run_trace(s, confirmed, candidates, failed_lanes=f"{GPU},{WEB},{AGGREGATE}")
        summary = self.summary()
        self.assertIn(f"Unverified commits since `{sha(64)}`: 6.", summary)
        self.assertEqual(self.state("culprit"), sha(65))
        self.assertIn("the culprit", summary)
        # The candidate runs cover the traced lane, not the lane that was already red.
        lanes = {d["inputs"]["lanes"] for d in self.dispatched()}
        self.assertEqual(lanes, {WEB})
        self.assertEqual(len(self.dispatched()), 6)

    def test_a_candidate_that_fails_only_the_old_test_counts_as_passed_for_the_new_one(self):
        others = ["keel_render_wgpu::t0", "keel_render_wgpu::t1"]
        s = Scenario(70, missing=[67])
        for n in range(1, 71):
            if n < 3:
                s.add(n)
            elif n < 68:
                s.add(n, [GPU], junit={GPU: junit_xml([OLD_TEST], others + [NEW_TEST])})
            else:
                s.add(n, [GPU], junit={GPU: junit_xml([OLD_TEST, NEW_TEST], others)})
        confirmed = [("test", OLD_TEST, GPU), ("test", NEW_TEST, GPU)]
        candidates = {
            67: ([GPU], {GPU: junit_xml([OLD_TEST], others + [NEW_TEST])}),
            68: ([GPU], {GPU: junit_xml([OLD_TEST, NEW_TEST], others)}),
            69: ([GPU], {GPU: junit_xml([OLD_TEST, NEW_TEST], others)}),
            70: ([GPU], {GPU: junit_xml([OLD_TEST, NEW_TEST], others)}),
        }
        self.run_trace(s, confirmed, candidates, failed_lanes=f"{GPU},{AGGREGATE}")
        summary = self.summary()
        self.assertIn(f"Unverified commits since `{sha(66)}`: 4.", summary)
        self.assertIn(f"Candidate `{sha(67)[:9]}` failed other units only", summary)
        self.assertEqual(self.state("culprit"), sha(68))

    def test_without_a_clearing_run_the_fully_verified_baseline_still_decides(self):
        s = Scenario(70)
        for n in range(1, 71):
            s.add(n, [GPU])
        confirmed = [lane(GPU)]
        self.run_trace(s, confirmed, {}, verified_base=0, failed_lanes=f"{GPU},{AGGREGATE}")
        summary = self.summary()
        self.assertIn("Per-unit baseline unavailable", summary)
        self.assertIn(f"Unverified commits since `{sha(0)}`: 70.", summary)
        self.assertIn("More than 8 unverified commits", summary)
        self.assertEqual(self.state("base-unit-run"), "")
        self.assertEqual(self.dispatched(), [])

    def test_no_confirmed_unit_keeps_the_old_baseline(self):
        s = Scenario(10)
        for n in range(1, 11):
            s.add(n, [GPU] if n >= 5 else [])
        self.run_trace(s, [], {n: ([GPU], None) for n in range(1, 11)}, verified_base=4,
                       failed_lanes=f"{GPU},{AGGREGATE}")
        self.assertEqual(self.state("base"), sha(4))
        self.assertEqual(self.state("base-unit-run"), "")

    def keel(self, run):
        return json.loads((FIXTURES / f"keel-verify-jobs-{run}.json").read_text())["jobs"]

    def keel_trace(self, candidates):
        """Keel main red on "Web release and smoke" from c5; c1-c4 verified."""
        s = Scenario(10)
        for n in range(1, 11):
            s.add(n, [WEB] if n >= 5 else [])
        # Confirmed on two runs: the job failed at "Mobile web smoke" both times.
        target = ("lane", "failure at Mobile web smoke: Process completed with exit code 1.", WEB)
        self.run_trace(s, [target], candidates, failed_lanes=f"{WEB},{AGGREGATE}",
                       annotations=json.loads((FIXTURES / "keel-check-annotations.json").read_text()))
        self.assertIn(f"Unverified commits since `{sha(4)}`: 6.", self.summary())

    def test_a_candidate_failing_the_same_step_and_error_is_the_culprit(self):
        same, other = self.keel(37250728778), self.keel(37242311505)
        self.keel_trace({5: ([], None), 6: (same, None), 7: (same, None), 8: (other, None),
                         9: (same, None), 10: (same, None)})
        self.assertEqual(self.state("culprit"), sha(6))
        self.assertEqual({d["inputs"]["lanes"] for d in self.dispatched()}, {WEB})

    def test_a_candidate_failing_the_same_job_another_way_is_never_the_culprit(self):
        # c6 fails "Web release and smoke" at "Serve the web pack" (the
        # scene() check of 145c958a), which stops the job before "Mobile web
        # smoke" runs. A job-name match would name c6; it is inconclusive, so
        # no single culprit is named: the window is reverted as one pull
        # request only where the caller allows it (revert-window), otherwise
        # reported.
        same, other = self.keel(37250728778), self.keel(37242311505)
        self.keel_trace({5: ([], None), 6: (other, None), 7: (same, None), 8: (same, None),
                         9: (same, None), 10: (same, None)})
        self.assertEqual(self.state("culprit"), "")
        summary = self.summary()
        self.assertIn(f"Candidate `{sha(6)[:9]}`: the target job {WEB} failed in the candidate, but not the same way",
                      summary)
        self.assertIn("cannot be named with certainty", summary)
        # A candidate that failed only other jobs (here the Windows link step)
        # with the target passing counts as passed.
        passed_other = [dict(row, conclusion="success", steps=[dict(st, conclusion="success") for st in row["steps"]])
                        if row["name"] in (WEB, AGGREGATE) else row for row in other]
        self.keel_trace({5: ([], None), 6: (passed_other, None), 7: (same, None), 8: (same, None),
                         9: (same, None), 10: (same, None)})
        self.assertEqual(self.state("culprit"), sha(7))

    def test_the_trace_step_dispatches_the_traced_lanes_only(self):
        step = STEPS["Trace the culprit among the unverified commits"]["run"]
        self.assertIn('--arg value "$dispatch_lanes"', step)
        self.assertNotIn('--arg value "$FAILED_LANES"', step)

    def test_the_revert_step_does_not_demand_a_verified_check_on_a_per_unit_baseline(self):
        step = STEPS["Revert the culprit or report it"]["run"]
        self.assertIn('if [ -z "$(state_get base-unit-run)" ]; then', step)


if __name__ == "__main__":
    unittest.main()
