"""Self-cancelled required failure remains recoverable, never deployable.

Recorded provider objects bind the failing job and its check to a real run.
Only producer branch/path are adapted in positive cases: the disposable probe
ran outside main. The hooks and job/check/attempt identities are unchanged.
"""
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "tests/fixtures/red-main/fail-fast"
REPO = "SylphxAI/cloud"
spec = importlib.util.spec_from_file_location("proof", ROOT / ".github/red_main/proof.py")
proof = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proof)


def log_bytes(failed=None, hook=None, extra=()):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("required/4_Synthetic required failure.txt", failed if failed is not None else (DATA / "failed-step.txt").read_bytes())
        archive.writestr("required/5_Cancel failed current run.txt", hook if hook is not None else (DATA / "hook-step.txt").read_bytes())
        for name, data in extra:
            archive.writestr(name, data)
    return output.getvalue()


def logs(*args):
    return zipfile.ZipFile(io.BytesIO(log_bytes()))


class FailFastTest(unittest.TestCase):
    def setUp(self):
        self.run = json.loads((DATA / "run.json").read_text())
        self.raw_run = copy.deepcopy(self.run)
        self.run.update(head_branch="main", path=".github/workflows/verify.yml")
        self.jobs = json.loads((DATA / "jobs.json").read_text())["jobs"]
        self.check = json.loads((DATA / "check.json").read_text())
        self.identity = str(self.run["id"])

    def api(self, path):
        if "/jobs?" in path:
            return dict(total_count=len(self.jobs), jobs=self.jobs)
        if path == f"repos/{REPO}/actions/runs/{self.identity}":
            return self.run
        if path == f"repos/{REPO}/check-runs/{self.check['id']}":
            return self.check
        if "/annotations?" in path:
            return json.loads((DATA / "annotations.json").read_text())
        raise AssertionError(path)

    def verdict(self):
        with patch.object(proof, "api", side_effect=self.api):
            return proof.checked_verdict(self.run, REPO, "main", "verify.yml")

    def normalized(self):
        with patch.object(proof, "api", side_effect=self.api):
            return proof.failed_jobs(REPO, self.identity)

    def hook(self):
        return next(s for s in self.jobs[0]["steps"] if s["name"] == proof.FAIL_FAST_STEP)

    def failure(self):
        return next(s for s in self.jobs[0]["steps"] if s["conclusion"] == "failure")

    def test_real_cancelled_run_has_no_aggregate_but_an_authenticated_failure(self):
        self.assertEqual(self.run["conclusion"], "cancelled")
        self.assertEqual([j["conclusion"] for j in self.jobs], ["cancelled", "cancelled"])
        self.assertNotIn("verified", [j["name"] for j in self.jobs])
        self.assertEqual(self.verdict(), "failure")
        normalized = self.normalized()
        self.assertEqual(len(normalized), 1)
        self.assertEqual(normalized[0]["name"], self.jobs[0]["name"])
        self.assertEqual(normalized[0]["conclusion"], "failure")
        self.assertNotIn(proof.FAIL_FAST_STEP, [s["name"] for s in normalized[0]["steps"]])
        self.assertEqual(self.jobs[0]["conclusion"], "cancelled")  # reader never mutates provider objects

    def test_probe_branch_and_dispatch_are_not_main_failure_proofs(self):
        self.run = self.raw_run
        self.assertEqual(self.verdict(), "ineligible")
        self.run.update(head_branch="main", path=".github/workflows/verify.yml", event="workflow_dispatch")
        self.assertEqual(self.verdict(), "ineligible")
        with self.assertRaisesRegex(ValueError, "unauthenticated"):
            self.normalized()

    def test_plain_cancelled_siblings_and_failure_without_successful_hook_are_not_red_or_green(self):
        original = copy.deepcopy(self.jobs)
        mutations = [lambda: self.hook().update(conclusion="cancelled"),
                     lambda: self.hook().update(conclusion="failure"),
                     lambda: self.hook().update(status="in_progress"),
                     lambda: self.hook().update(name="renamed hook"),
                     lambda: self.jobs[0]["steps"].append(copy.deepcopy(self.hook())),
                     lambda: self.failure().update(conclusion="cancelled"),
                     lambda: self.failure().update(status="in_progress")]
        for change in mutations:
            self.jobs = copy.deepcopy(original)
            change()
            with self.subTest(jobs=self.jobs):
                self.assertEqual(self.verdict(), "cancelled")
                self.assertEqual(self.normalized(), [])

    def test_hook_itself_or_later_cleanup_can_never_be_the_failure_evidence(self):
        failed = self.failure()
        failed.update(conclusion="success")
        self.jobs[0]["steps"].append(dict(name="Cleanup", status="completed", conclusion="failure"))
        self.assertEqual(self.verdict(), "cancelled")
        self.assertEqual(self.normalized(), [])
        failed.update(conclusion="failure")
        self.assertNotIn("Cleanup", [s["name"] for s in self.normalized()[0]["steps"]])

    def test_job_check_attempt_producer_mismatches_are_unknown_not_failure(self):
        original_job, original_check = copy.deepcopy(self.jobs[0]), copy.deepcopy(self.check)
        changes = [("job", "run_id", self.run["id"] + 1), ("job", "run_attempt", 2),
                   ("job", "head_sha", "a" * 40), ("job", "check_run_url", "https://evil.invalid/1"),
                   ("check", "app", dict(id=1, slug="other")),
                   ("check", "check_suite", dict(id=1)), ("check", "head_sha", "a" * 40),
                   ("check", "details_url", "https://github.com/other/run"),
                   ("check", "conclusion", "success"), ("check", "status", "in_progress")]
        for target, key, value in changes:
            self.jobs[0], self.check = copy.deepcopy(original_job), copy.deepcopy(original_check)
            (self.jobs[0] if target == "job" else self.check)[key] = value
            with self.subTest(target=target, key=key):
                self.assertEqual(self.verdict(), "unknown")
                with self.assertRaises(ValueError):
                    self.normalized()

    def test_success_still_requires_the_original_authenticated_aggregate(self):
        # Advisory cancellation must not erase a valid aggregate success.
        job = copy.deepcopy(self.jobs[0])
        job.update(name="verified", conclusion="success")
        self.jobs = [job]
        self.check.update(name="verified", conclusion="success")
        self.assertEqual(self.verdict(), "success")
        self.check["app"]["id"] = 1
        self.assertEqual(self.verdict(), "unknown")

    def test_original_step_errors_confirm_an_ordinary_diagnostic_candidate(self):
        normalized = self.normalized()[0]
        ordinary_notes = [dict(annotation_level="failure", message="Process completed with exit code 1.")]
        with patch.object(proof, "api", side_effect=self.api), patch.object(proof, "attempt_logs", side_effect=logs):
            units = proof.failed_units(REPO, self.identity)
            self.assertEqual(proof.failed_lanes(REPO, self.identity), self.jobs[0]["name"])
        ordinary = proof.lane_signature(normalized, ordinary_notes)
        self.assertEqual(units, f"required\t{ordinary}")
        self.assertNotIn("forcefully", units)
        with tempfile.TemporaryDirectory() as tmp:
            previous, current, confirmed = [Path(tmp) / x for x in ("prev", "cur", "confirmed")]
            row = f"lane\t{ordinary}\t{self.jobs[0]['name']}\n"
            previous.write_text(row)
            current.write_text(row)
            done = subprocess.run([sys.executable, str(ROOT / ".github/red_main/confirm.py"),
                                   "failure", str(previous), str(current), str(confirmed)],
                                  capture_output=True, text=True, check=True)
            self.assertTrue(done.stdout.startswith("yes "), done.stdout)
            self.assertEqual(confirmed.read_text(), row)
            current.write_text(row.replace("exit code 1", "exit code 2"))
            done = subprocess.run([sys.executable, str(ROOT / ".github/red_main/confirm.py"),
                                   "failure", str(previous), str(current), str(confirmed)],
                                  capture_output=True, text=True, check=True)
            self.assertTrue(done.stdout.startswith("no"), done.stdout)

    def test_original_logs_are_mandatory_for_diagnostics_but_not_failure_or_success_proof(self):
        with patch.object(proof, "api", side_effect=self.api), patch.object(proof, "attempt_logs", side_effect=ValueError("logs unavailable")):
            self.assertEqual(proof.checked_verdict(self.run, REPO, "main", "verify.yml"), "failure")
            with self.assertRaisesRegex(ValueError, "logs unavailable"):
                proof.failed_units(REPO, self.identity)

    def test_step_archive_mapping_is_unique_bounded_and_excludes_hook_and_cleanup_errors(self):
        root = self.jobs[0]
        def recovered(data):
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                return proof.original_failure_notes(archive, [root], self.jobs)
        hook_error = b"2026-11-06T20:22:18Z ##[error]hook must not become a diagnostic\n"
        cleanup = ("required/6_Complete job.txt", b"2026-11-06T20:22:18Z ##[error]cleanup must not become a diagnostic\n")
        notes = recovered(log_bytes(hook=hook_error, extra=[cleanup]))[root["id"]]
        self.assertEqual([n["message"] for n in notes], ["Process completed with exit code 1."])
        cases = [log_bytes(failed=b"no error annotation"), log_bytes(failed=b"\xff"),
                 log_bytes(extra=[("required/4_ambiguous.txt", b"other")])]
        for data in cases:
            with self.subTest(data=data[:20]), self.assertRaises((ValueError, UnicodeError)):
                recovered(data)
        for excluded in ["required/4_Synthetic required failure.txt", "required/5_Cancel failed current run.txt"]:
            data = io.BytesIO()
            with zipfile.ZipFile(io.BytesIO(log_bytes())) as source, zipfile.ZipFile(data, "w") as target:
                for name in source.namelist():
                    if name != excluded:
                        target.writestr(name, source.read(name))
            with self.subTest(missing=excluded), self.assertRaises(ValueError):
                recovered(data.getvalue())
        # A valid directory with damaged stored bytes must fail the CRC check.
        damaged = io.BytesIO()
        with zipfile.ZipFile(damaged, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("required/4_Synthetic required failure.txt", (DATA / "failed-step.txt").read_bytes())
            archive.writestr("required/5_Cancel failed current run.txt", (DATA / "hook-step.txt").read_bytes())
        payload = damaged.getvalue().replace(b"Process completed", b"Process corruptxx", 1)
        with self.assertRaises(zipfile.BadZipFile):
            recovered(payload)
        for limit in ("LOG_MEMBER_LIMIT", "LOG_SELECTED_LIMIT"):
            with patch.object(proof, limit, 100), self.assertRaisesRegex(ValueError, "byte limit"):
                recovered(log_bytes())
        self.jobs[1]["name"] = "re:quired/"  # collides after provider sanitization
        with self.assertRaisesRegex(ValueError, "directory ambiguous"):
            recovered(log_bytes())
        self.jobs.pop()
        root["steps"][3]["number"] = 5
        with self.assertRaisesRegex(ValueError, "step numbers"):
            recovered(log_bytes())

    def test_log_download_caps_bytes_timeout_members_and_rejects_bad_archives(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "logs.zip"
            archive.write_bytes(log_bytes())
            forge = root / "gh"
            forge.write_text("#!/usr/bin/env python3\nimport os,sys,time\nfrom pathlib import Path\n"
                             "assert sys.argv[1:3] == ['api', '--allow-escape-sequences']\n"
                             f"assert sys.argv[3].endswith('/{self.identity}/attempts/1/logs')\n"
                             "if os.environ.get('SLOW'): time.sleep(2)\n"
                             "sys.stdout.buffer.write(Path(os.environ['LOG_ARCHIVE']).read_bytes())\n")
            forge.chmod(0o755)
            env = dict(os.environ, PATH=f"{root}:{os.environ['PATH']}", LOG_ARCHIVE=str(archive))
            with patch.dict(os.environ, env):
                with proof.attempt_logs(REPO, self.run) as received:
                    self.assertEqual(received.namelist(), logs().namelist())
                with patch.object(proof, "LOG_ARCHIVE_LIMIT", 100), self.assertRaisesRegex(ValueError, "byte limit"):
                    with proof.attempt_logs(REPO, self.run):
                        self.fail("oversized archive admitted")
                with patch.object(proof, "LOG_MEMBER_COUNT", 1), self.assertRaisesRegex(ValueError, "member limit"):
                    with proof.attempt_logs(REPO, self.run):
                        self.fail("too many members admitted")
                with patch.dict(os.environ, SLOW="yes"), patch.object(proof, "LOG_TIMEOUT", 0.05), self.assertRaisesRegex(ValueError, "timed out"):
                    with proof.attempt_logs(REPO, self.run):
                        self.fail("late producer admitted")
                archive.write_bytes(log_bytes()[:-10])
                with self.assertRaises((ValueError, zipfile.BadZipFile)):
                    with proof.attempt_logs(REPO, self.run):
                        self.fail("truncated archive admitted")

    def test_prev_selects_authenticated_cancelled_failure_and_skips_plain_cancel(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("prev.py", "proof.py"):
                (root / name).write_text((ROOT / ".github/red_main" / name).read_text())
            # The shipped predecessor program imports proof; fake only its API.
            with (root / "proof.py").open("a") as out:
                out.write("\nfrom pathlib import Path\n"
                          "def api(path):\n"
                          "    data=json.loads(Path(__file__).with_name('provider.json').read_text())\n"
                          "    return data['jobs'] if '/jobs?' in path else data['check']\n")
            body = dict(total_count=2, workflow_runs=[self.run, dict(self.run, id=self.run["id"] - 1,
                        conclusion="success", created_at="2026-11-06T20:00:00Z")])
            def previous():
                (root / "provider.json").write_text(json.dumps(dict(jobs=dict(total_count=len(self.jobs), jobs=self.jobs), check=self.check)))
                return subprocess.run([sys.executable, str(root / "prev.py"), "2026-11-06T21:00:00Z",
                                       REPO, "main", "verify.yml", "verified"], input=json.dumps(body),
                                      capture_output=True, text=True)
            done = previous()
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertTrue(done.stdout.startswith(f"{self.identity}\tfailure\t"), done.stdout)
            self.hook()["conclusion"] = "cancelled"
            self.assertTrue(previous().stdout.startswith(f"{self.run['id'] - 1}\tsuccess\t"))
            self.hook()["conclusion"] = "success"
            self.check["app"]["id"] = 1
            self.assertNotEqual(previous().returncode, 0)

    def test_window_uses_newest_authenticated_failure_but_not_plain_cancellation(self):
        spec = importlib.util.spec_from_file_location("window", ROOT / ".github/red_main/window.py")
        window = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(window)
        sibling = self.jobs[1]
        sibling.update(conclusion="success")
        plain = dict(self.run, id=self.run["id"] + 1)
        target = ("lane", "any signature", sibling["name"])
        with patch.object(proof, "api", side_effect=self.api):
            loader = window.live_loader(proof, REPO)
            chosen, cleared, read = window.find_window([target], [plain, self.run], loader)
            self.assertEqual(chosen["id"], self.run["id"])
            self.assertEqual(cleared, [target])
            self.assertEqual(read, 2)
            evidence = loader(self.run)
            root = self.jobs[0]["name"]
            self.assertEqual(evidence.jobs[root], "failure")
            self.assertFalse(window.clears(("lane", "any signature", root), evidence))
            evidence._junit = lambda: ({("test::bad", root)}, {("test::ok", root)}, {root})
            self.assertTrue(window.clears(("test", "test::ok", root), evidence))
            self.assertFalse(window.clears(("test", "test::bad", root), evidence))
            sibling["conclusion"] = "cancelled"
            self.assertFalse(window.clears(target, loader(self.run)))
            sibling["conclusion"] = "success"
            self.check["app"]["id"] = 1
            self.assertEqual(loader(self.run).jobs, {})

    def test_shipped_infra_reader_classifies_only_the_authenticated_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "red-main"
            work.mkdir()
            for name in ("lib.sh", "infra.py", "proof.py"):
                (work / name).write_text((ROOT / ".github/red_main" / name).read_text())
            forge = root / "gh"
            forge.write_text("#!/usr/bin/env python3\nimport json,os,sys\n"
                             "p=next(x for x in sys.argv if x.startswith('repos/'))\n"
                             "data=json.loads(os.environ['PROVIDER'])\n"
                             "if os.environ.get('UNAVAILABLE')=='yes': sys.exit(1)\n"
                             "if p.endswith('/logs'): sys.stdout.buffer.write(open(os.environ['LOG_ARCHIVE'],'rb').read()); sys.exit(0)\n"
                             "key='jobs' if '/jobs?' in p else 'notes' if '/annotations?' in p else 'check' if '/check-runs/' in p else 'run'\n"
                             "v=data[key]\n"
                             "if '--jq' in sys.argv: v=[n['message'] for n in v]\n"
                             "print(json.dumps(v))\n")
            forge.chmod(0o755)
            env = dict(os.environ, RUNNER_TEMP=tmp, REPO=REPO, ACTIONS_TOKEN="fixture",
                       PATH=f"{root}:{os.environ['PATH']}", ID=self.identity)
            archive = root / "logs.zip"
            env["LOG_ARCHIVE"] = str(archive)
            def classify(notes, unavailable=False, failed=None):
                archive.write_bytes(log_bytes(failed=failed))
                env['PROVIDER'] = json.dumps(dict(jobs=dict(total_count=len(self.jobs), jobs=self.jobs),
                                                  run=self.run, check=self.check, notes=notes))
                env['UNAVAILABLE'] = 'yes' if unavailable else 'no'
                return subprocess.run(['bash', '-c', '. "$RUNNER_TEMP/red-main/lib.sh"; classify_run "$ID"'],
                                      env=env, capture_output=True, text=True)
            notes = json.loads((DATA / "annotations.json").read_text())
            code = classify(notes)
            self.assertEqual(code.returncode, 0, code.stderr)
            self.assertEqual(code.stdout.strip(), "none")
            self.failure()["name"] = "Restore cache"
            infra = classify(notes, failed=b"2026-11-06T20:22:17Z ##[error]Failed to restore: 403 Forbidden\n")
            self.assertEqual(infra.returncode, 0, infra.stderr)
            self.assertEqual(infra.stdout.strip(), "infra cache-403")
            missing_logs = classify(notes, failed=b"no original error")
            self.assertNotEqual(missing_logs.returncode, 0)
            self.assertEqual(missing_logs.stdout, "")
            self.assertNotEqual(classify(notes, unavailable=True).returncode, 0)


if __name__ == "__main__":
    unittest.main()
