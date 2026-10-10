#!/usr/bin/env python3
"""Execute the real handler scripts under API exceptions and unknown proof.

All provider commands and proof reads use local fixtures. No network or git
writes are allowed. Unknown proof must retain recovery without authorizing a
baseline, quarantine or destructive revert.
"""
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
DOCUMENT = yaml.safe_load(WORKFLOW.read_text())
HANDLER = DOCUMENT["jobs"]["red-main"]
STEPS = {step["name"]: step for step in HANDLER["steps"] if "name" in step}
SHA = "a" * 40
RUN = dict(id=1, event="push", path=".github/workflows/verify.yml", workflow_id=2,
           head_branch="main", head_sha=SHA, check_suite_id=3, run_attempt=1,
           repository=dict(id=4, full_name="SylphxAI/cloud"),
           head_repository=dict(id=4, full_name="SylphxAI/cloud"),
           html_url="https://github.com/SylphxAI/cloud/actions/runs/1", conclusion="failure")

FAKE_GH = '''
import json, os, pathlib, sys
args = sys.argv[1:]
if args[:2] == ["run", "rerun"]:
    pathlib.Path(os.environ["RUNNER_TEMP"], "rerun.called").touch()
    sys.exit(0)
path = args[1] if len(args) > 1 else ""
if "/pulls?" in path or path.startswith("search/issues?"):
    print(0)
elif "/jobs?" in path:
    if "/runs/9/" in path and "PREVIOUS_JOB_HISTORY" in os.environ:
        reads = pathlib.Path(os.environ["RUNNER_TEMP"], "previous-jobs-read")
        if reads.exists():
            print(os.environ["PREVIOUS_JOB_HISTORY"])
            sys.exit(0)
        reads.touch()
    if (os.environ.get("JOBS_UNAVAILABLE") == "yes"
            or ("/runs/2/" in path and os.environ.get("NEWEST_UNAVAILABLE") == "yes")):
        print("HTTP 502: jobs unavailable", file=sys.stderr)
        sys.exit(1)
    if "--jq" in args:
        print("rust")
    else:
        key = "NEWEST_JOB_FIXTURE" if "/runs/2/" in path else "JOB_FIXTURE"
        print(json.dumps({"total_count": 1, "jobs": [json.loads(os.environ[key])]} if key in os.environ else {"total_count": 0, "jobs": []}))
elif "/actions/workflows/" in path:
    with open(os.path.join(os.environ["RUNNER_TEMP"], "workflow-paths"), "a") as stream:
        stream.write(path + "\\n")
    if os.environ.get("HISTORY_UNAVAILABLE") == "yes":
        print("HTTP 502: history unavailable", file=sys.stderr)
        sys.exit(1)
    if "HISTORY_PAGES" in os.environ:
        print(json.dumps(json.loads(os.environ["HISTORY_PAGES"])[int(path.rsplit("page=", 1)[1]) - 1]))
        sys.exit(0)
    print(os.environ.get("HISTORY_FIXTURE", '{"total_count": 0, "workflow_runs": []}'))
elif "/check-runs/" in path:
    print(os.environ["CHECK_FIXTURE"])
elif path.endswith("actions/runs/9"):
    print(os.environ["PREVIOUS_RUN_FIXTURE"])
elif path.endswith("actions/runs/1"):
    if os.environ.get("RUN_UNAVAILABLE") == "yes":
        print("HTTP 502: run unavailable", file=sys.stderr)
        sys.exit(1)
    if "--jq" in args:
        query = args[args.index("--jq") + 1]
        print(2 if query == ".run_attempt" else "2026-09-30T10:00:00Z" if query == ".created_at" else "completed\\tfailure")
    else:
        print(os.environ["RUN_FIXTURE"])
else:
    print("unexpected provider command: " + repr(args), file=sys.stderr)
    sys.exit(1)
'''

FAKE_PROOF = '''
import os, sys
if sys.argv[1] == "history":
    import subprocess
    sys.exit(subprocess.run([sys.executable, os.path.join(os.environ["RUNNER_TEMP"], "red-main/real_proof.py"), *sys.argv[1:]]).returncode)
value = (os.environ.get("BASE_RESULT", "") if sys.argv[1] == "remote-base" else
         os.environ.get("NEWEST_RESULT", os.environ["PROOF_RESULT"]) if sys.argv[1] == "sha" else os.environ["PROOF_RESULT"])
if value == "exception":
    print("HTTP 502: jobs/check proof unavailable", file=sys.stderr)
    sys.exit(1)
print(value)
'''


class UnavailableHandlerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / "red-main"
        self.work.mkdir()
        for name, filename in (("LIB", "lib.sh"),
                               ("PREV_PY", "prev.py"), ("CONFIRM_PY", "confirm.py")):
            (self.work / filename).write_text(HANDLER["env"][name])
        (self.work / "proof.py").write_text(FAKE_PROOF)
        (self.work / "real_proof.py").write_text((ROOT / ".github/actions/ci-range/post_main.py").read_text())
        binaries = self.root / "bin"
        binaries.mkdir()
        gh = binaries / "gh"
        gh.write_text(f"#!{sys.executable}\n" + FAKE_GH)
        gh.chmod(0o755)
        git = binaries / "git"
        git.write_text('#!/bin/bash\ntouch "$RUNNER_TEMP/git.called"\nexit 1\n')
        git.chmod(0o755)
        self.env = dict(os.environ, RUNNER_TEMP=str(self.root),
                        RUN_STORE_JUNIT=str(ROOT / ".github/actions/run-store/get-junit.py"),
                        PATH=f"{binaries}:{os.environ['PATH']}",
                        GITHUB_OUTPUT=str(self.root / "output"),
                        TRUNK="main", REPO="SylphxAI/cloud", VERIFY_WORKFLOW="verify.yml", VERIFY_CHECK_NAME="verified",
                        EVENT_RUN_ID="1", RUN_ID="1", HEAD_SHA=SHA, SHORT_SHA=SHA[:9],
                        RUN_URL=RUN["html_url"], RUN_FIXTURE=json.dumps(RUN),
                        MODE="notify", ACTIONS_TOKEN="fixture", GH_TOKEN="fixture",
                        INFRA="no", INFRA_SIGNATURE="", INITIAL_PROOF="unknown",
                        PROOF_RESULT="unknown")

    def execute(self, name):
        return subprocess.run(["bash", "-c", STEPS[name]["run"]], env=self.env,
                              cwd=self.root, capture_output=True, text=True, timeout=10)

    def state(self, name):
        path = self.work / "state" / name
        return path.read_text().strip() if path.exists() else ""

    def summary(self):
        path = self.work / "summary.md"
        return path.read_text() if path.exists() else ""

    def use_real_proof(self):
        (self.work / "proof.py").write_text((ROOT / ".github/actions/ci-range/post_main.py").read_text())

    def history_of(self, branch):
        self.env["EVENT_RUN_ID"] = ""
        self.env["PROOF_RESULT"] = "failure"
        self.env["HISTORY_FIXTURE"] = json.dumps(dict(total_count=1, workflow_runs=[dict(RUN, head_branch=branch, status="completed")]))

    def test_dispatch_on_a_master_repository_classifies_its_newest_failed_run(self):
        self.history_of("master")
        self.env["TRUNK"] = "master"
        result = self.execute("Resolve the verify run that failed")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.work / "state/quiet").exists(), result.stdout)
        self.assertEqual(self.state("run-id"), "1")
        self.assertIn("branch=master&", (self.root / "workflow-paths").read_text())

    def test_dispatch_never_takes_another_branch_for_the_trunk(self):
        self.history_of("main")
        self.env["TRUNK"] = "master"
        result = self.execute("Resolve the verify run that failed")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("on master was found", self.state("quiet"))

    def test_real_helper_partial_and_null_producers_never_quiet(self):
        self.use_real_proof()
        partials = [None, {}, dict(RUN, repository=None), dict(RUN, head_repository=None)]
        for field in ("id", "event", "path", "workflow_id", "head_branch", "head_sha",
                      "check_suite_id", "run_attempt", "repository", "head_repository"):
            missing = dict(RUN)
            missing.pop(field)
            partials.extend([missing, dict(RUN, **{field: None})])
        for row in partials:
            with self.subTest(row=row):
                self.env["RUN_FIXTURE"] = json.dumps(row)
                result = self.execute("Resolve the verify run that failed")
                self.assertFalse((self.work / "state/quiet").exists(), result.stdout)
                self.assertIn("Unverified", self.summary())
                if row and row.get("id") == 1 and row.get("head_sha") == SHA:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(self.state("proof-verdict"), "unknown")
                else:
                    self.assertNotEqual(result.returncode, 0)

    def test_real_helper_valid_nonproof_origin_is_terminal(self):
        self.use_real_proof()
        self.env["RUN_FIXTURE"] = json.dumps(dict(RUN, event="workflow_dispatch"))
        result = self.execute("Resolve the verify run that failed")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ineligible", self.state("quiet"))

    def test_resolver_proof_exception_and_unknown_keep_recovery_active(self):
        for result in ("exception", "unknown"):
            with self.subTest(proof=result):
                self.env["PROOF_RESULT"] = result
                resolved = self.execute("Resolve the verify run that failed")
                self.assertEqual(resolved.returncode, 0, resolved.stderr)
                self.assertFalse((self.work / "state/quiet").exists())
                self.assertEqual(self.state("proof-verdict"), "unknown")
                self.assertEqual(self.state("run-id"), "1")
                self.assertIn("proof-verdict=unknown", (self.root / "output").read_text())
                self.assertIn("Unverified", self.summary())

    def test_resolver_ineligible_and_authenticated_success_are_terminal(self):
        for result in ("ineligible", "success"):
            with self.subTest(proof=result):
                self.env["PROOF_RESULT"] = result
                resolved = self.execute("Resolve the verify run that failed")
                self.assertEqual(resolved.returncode, 0, resolved.stderr)
                self.assertIn(result, self.state("quiet"))
                (self.work / "state/quiet").unlink()

    def test_resolver_missing_jobs_does_not_silence_unknown_proof(self):
        self.env["JOBS_UNAVAILABLE"] = "yes"
        result = self.execute("Resolve the verify run that failed")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.work / "state/quiet").exists())
        self.assertEqual(self.state("run-id"), "1")
        self.assertIn("failed lanes could not be listed", self.summary())

    def test_unavailable_run_metadata_escalates_instead_of_quiet(self):
        self.env["RUN_UNAVAILABLE"] = "yes"
        result = self.execute("Resolve the verify run that failed")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.work / "state/quiet").exists())
        self.assertIn("escalating", self.summary())
        self.assertEqual(STEPS["Alert that the red-main handler itself failed"]["if"], "failure()")

    def test_green_guard_exception_and_unknown_are_not_success(self):
        for proof in ("exception", "unknown", "failure", "ineligible"):
            with self.subTest(proof=proof):
                self.env["PROOF_RESULT"] = proof
                result = self.execute("Stop when the work is already done or already in hand")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse((self.work / "state/quiet").exists())
        self.env["PROOF_RESULT"] = "success"
        result = self.execute("Stop when the work is already done or already in hand")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("authenticated passing", self.state("quiet"))

    def test_unknown_initial_proof_escalates_and_never_reruns(self):
        for proof in ("exception", "unknown", "success"):
            with self.subTest(proof=proof):
                self.env["INITIAL_PROOF"] = proof
                result = self.execute("Decide the verdict of the failed run")
                self.assertFalse((self.root / "rerun.called").exists())
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("verdict=unknown", (self.root / "output").read_text())
                self.assertFalse((self.work / "state/quiet").exists())
                self.assertIn("Unverified", self.summary())

    def test_authenticated_failure_is_real_and_never_reruns(self):
        for proof in ("failure", "timed_out"):
            with self.subTest(proof=proof):
                self.env["INITIAL_PROOF"] = proof
                result = self.execute("Decide the verdict of the failed run")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse((self.root / "rerun.called").exists())
                self.assertIn("verdict=real", (self.root / "output").read_text())

    def test_infrastructure_failure_is_reported_without_revert_or_rerun(self):
        self.env.update(INITIAL_PROOF="failure", INFRA="yes", INFRA_SIGNATURE="runner-lost")
        result = self.execute("Decide the verdict of the failed run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "rerun.called").exists())
        self.assertIn("verdict=infra", (self.root / "output").read_text())
        self.assertIn("no revert and no rerun", self.summary())

    def test_final_revert_guard_refreshes_latest_run_and_baseline(self):
        state = self.work / "state"
        state.mkdir(exist_ok=True)
        (state / "proof-verdict").write_text("failure")
        (state / "base").write_text("b" * 40)
        for newest in ("unknown", "exception", "success"):
            with self.subTest(newest=newest):
                self.env["NEWEST_RESULT"] = newest
                result = self.execute("Revert the culprit or report it")
                self.assertEqual(result.returncode == 0, newest == "success")
                self.assertFalse((self.root / "git.called").exists())
        self.env["NEWEST_RESULT"] = "failure"
        result = self.execute("Revert the culprit or report it")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("baseline no longer has authenticated success", self.summary())
        self.assertFalse((self.root / "git.called").exists())

    def test_previous_history_missing_is_distinct_from_unavailable(self):
        history = {"total_count": 1, "workflow_runs": [dict(id=9, status="completed", conclusion="failure",
                                          created_at="2026-09-30T09:00:00Z", html_url="fixture")]}
        self.env["HISTORY_FIXTURE"] = json.dumps(history)
        for proof in ("unknown", "exception"):
            with self.subTest(proof=proof):
                self.env["PROOF_RESULT"] = proof
                result = self.execute("Confirm the same unit failed on two consecutive completed runs")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("previous run 9 has unavailable proof", self.summary())
                self.assertFalse((self.work / "state/quiet").exists())
        self.env["HISTORY_UNAVAILABLE"] = "yes"
        unavailable = self.execute("Confirm the same unit failed on two consecutive completed runs")
        self.assertNotEqual(unavailable.returncode, 0)
        self.assertIn("previous-run history is unavailable", self.summary())
        del self.env["HISTORY_UNAVAILABLE"]
        for invalid in ("null", "{}", '{"workflow_runs": null}', '{"workflow_runs": [{"id": 9}]}'):
            self.env["HISTORY_FIXTURE"] = invalid
            malformed = self.execute("Confirm the same unit failed on two consecutive completed runs")
            self.assertNotEqual(malformed.returncode, 0)
        self.env["HISTORY_FIXTURE"] = '{"total_count": 0, "workflow_runs": []}'
        absent = self.execute("Confirm the same unit failed on two consecutive completed runs")
        self.assertEqual(absent.returncode, 0, absent.stderr)
        self.assertIn("confirmed=no", (self.root / "output").read_text())

    def test_predecessor_on_page_two_is_found_and_exhaustion_escalates(self):
        old = dict(id=9, status="completed", conclusion="success", created_at="2026-09-30T09:00:00Z")
        newer = [dict(id=1000 + i, status="in_progress", conclusion=None,
                      created_at="2026-09-30T11:00:00Z") for i in range(100)]
        self.env["HISTORY_PAGES"] = json.dumps([dict(total_count=101, workflow_runs=newer),
                                                dict(total_count=101, workflow_runs=[old])])
        self.env["PROOF_RESULT"] = "success"
        result = self.execute("Confirm the same unit failed on two consecutive completed runs")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("confirmed=no", (self.root / "output").read_text())
        self.assertIn("was 'success'", self.summary())
        # History that never reaches a predecessor within the bound is unknown, not absent.
        self.env["HISTORY_PAGES"] = json.dumps([dict(total_count=600, workflow_runs=[dict(row, id=row["id"] + i * 100) for row in newer]) for i in range(5)])
        result = self.execute("Confirm the same unit failed on two consecutive completed runs")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("previous-run history is unavailable", self.summary())

    def test_authenticated_previous_failure_with_partial_jobs_escalates(self):
        self.use_real_proof()
        previous = dict(RUN, id=9, status="completed", conclusion="failure",
                        created_at="2026-09-30T09:00:00Z")
        job = dict(id=300, run_id=9, run_attempt=1, head_sha=SHA, name="verified",
                   status="completed", conclusion="failure",
                   check_run_url="https://api.github.com/repos/SylphxAI/cloud/check-runs/30")
        check = dict(id=30, app=dict(id=15368, slug="github-actions"), head_sha=SHA,
                     name="verified", check_suite=dict(id=3), status="completed", conclusion="failure",
                     details_url="https://github.com/SylphxAI/cloud/actions/runs/9/job/300")
        self.env.update(PREVIOUS_RUN_FIXTURE=json.dumps(previous), JOB_FIXTURE=json.dumps(job),
                        CHECK_FIXTURE=json.dumps(check),
                        HISTORY_FIXTURE=json.dumps(dict(total_count=1, workflow_runs=[previous])),
                        PREVIOUS_JOB_HISTORY='{"total_count": 2, "jobs": []}')
        result = self.execute("Confirm the same unit failed on two consecutive completed runs")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertTrue((self.root / "previous-jobs-read").exists())  # Authenticated first read succeeded.
        self.assertFalse(list((self.work / "junit-previous").glob("*")))
        output = (self.root / "output").read_text() if (self.root / "output").exists() else ""
        self.assertNotIn("confirmed=no", output)
        self.assertNotIn("confirmed=yes", output)
        self.assertIn("previous failing lanes are unavailable", self.summary())
        self.assertFalse((self.root / "git.called").exists())

    def test_unreadable_failed_job_detail_escalates_at_confirmation(self):
        # How the failed jobs failed (steps, error lines) cannot be read: the
        # junit step records it and goes on (the infra verdict still runs),
        # and the confirmation escalates instead of matching job names.
        (self.work / "junit.py").write_text(HANDLER["env"]["JUNIT_PY"])
        self.env.update(PROOF_RESULT="exception", FAILED_LANES="rust")
        read = self.execute("Read the failing tests from the run's junit artifacts")
        self.assertEqual(read.returncode, 0, read.stderr)
        self.assertIn("HTTP 502", self.state("units-unavailable"))
        self.env["PROOF_RESULT"] = "failure"
        result = self.execute("Confirm the same unit failed on two consecutive completed runs")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("failed jobs of run 1 failed could not be read", self.summary())
        output = (self.root / "output").read_text() if (self.root / "output").exists() else ""
        self.assertNotIn("confirmed=", output)

    def test_invalid_previous_status_escalates(self):
        for status in ("", "mystery"):
            self.env["HISTORY_FIXTURE"] = json.dumps(dict(total_count=1, workflow_runs=[dict(
                id=9, status=status, conclusion="failure", created_at="2026-09-30T09:00:00Z")]))
            result = self.execute("Confirm the same unit failed on two consecutive completed runs")
            self.assertNotEqual(result.returncode, 0)

    def test_unavailable_baseline_is_never_a_revert_base(self):
        for base in ("exception", "", "unknown"):
            with self.subTest(base=base):
                self.env["BASE_RESULT"] = base
                result = self.execute("Trace the culprit among the unverified commits")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.state("base"), "")
                self.assertFalse((self.work / "state/quiet").exists())
                self.assertFalse((self.root / "git.called").exists())

    def test_destructive_guard_requires_failure_and_baseline(self):
        state = self.work / "state"
        state.mkdir(exist_ok=True)
        for proof, base in (("unknown", "b" * 40), ("failure", "")):
            with self.subTest(proof=proof, base=base):
                (state / "proof-verdict").write_text(proof)
                (state / "base").write_text(base)
                result = self.execute("Revert the culprit or report it")
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.root / "git.called").exists())

    def test_preflight_api_exception_returns_unknown_without_skipping_handler(self):
        output = self.root / "preflight-output"
        env = dict(self.env, PROOF_MODE="run", PROOF_IDENTITY="1", PROOF_BRANCH="main",
                   PROOF_WORKFLOW="verify.yml", GITHUB_REPOSITORY="SylphxAI/cloud",
                   GITHUB_OUTPUT=str(output), JOBS_UNAVAILABLE="yes")
        result = subprocess.run([sys.executable, str(ROOT / ".github/actions/ci-range/ci_range.py")],
                                env=env, cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output.read_text(), "verdict=unknown\n")
        self.assertIn("proof unavailable", result.stderr)


if __name__ == "__main__":
    unittest.main()
