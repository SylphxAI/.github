"""Self-cancelled required failure remains recoverable, never deployable.

Recorded provider objects bind the failing job and its check to a real run.
Only producer branch/path are adapted in positive cases: the disposable probe
ran outside main. The hooks and job/check/attempt identities are unchanged.
"""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "tests/fixtures/red-main/fail-fast"
REPO = "SylphxAI/cloud"
spec = importlib.util.spec_from_file_location("proof", ROOT / ".github/red_main/proof.py")
proof = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proof)


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

    def test_two_self_cancelled_failures_confirm_the_same_unit(self):
        notes = json.loads((DATA / "annotations.json").read_text())
        normalized = self.normalized()[0]
        # Force-cancel replaces annotations. Do not invent the ordinary run's
        # missing error text or weaken the existing same-error requirement.
        ordinary = dict(normalized, conclusion="failure")
        self.assertNotEqual(proof.lane_signature(ordinary, [{"annotation_level": "failure", "message": "assertion x failed"}]),
                            proof.lane_signature(normalized, notes))
        with patch.object(proof, "api", side_effect=self.api):
            units = proof.failed_units(REPO, self.identity)
            self.assertEqual(proof.failed_lanes(REPO, self.identity), self.jobs[0]["name"])
        self.assertIn("failure at Synthetic required failure: The run was canceled forcefully by @github-actions[bot].", units)
        with tempfile.TemporaryDirectory() as tmp:
            previous, current, confirmed = [Path(tmp) / x for x in ("prev", "cur", "confirmed")]
            row = f"lane\t{units.split(chr(9))[1]}\t{self.jobs[0]['name']}\n"
            previous.write_text(row)
            current.write_text(row)
            done = subprocess.run([sys.executable, str(ROOT / ".github/red_main/confirm.py"),
                                   "failure", str(previous), str(current), str(confirmed)],
                                  capture_output=True, text=True, check=True)
            self.assertTrue(done.stdout.startswith("yes "), done.stdout)
            self.assertEqual(confirmed.read_text(), row)

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
                             "p=sys.argv[2]\n"
                             "data=json.loads(os.environ['PROVIDER'])\n"
                             "if os.environ.get('UNAVAILABLE')=='yes': sys.exit(1)\n"
                             "key='jobs' if '/jobs?' in p else 'notes' if '/annotations?' in p else 'check' if '/check-runs/' in p else 'run'\n"
                             "v=data[key]\n"
                             "if '--jq' in sys.argv: v=[n['message'] for n in v]\n"
                             "print(json.dumps(v))\n")
            forge.chmod(0o755)
            env = dict(os.environ, RUNNER_TEMP=tmp, REPO=REPO, ACTIONS_TOKEN="fixture",
                       PATH=f"{root}:{os.environ['PATH']}", ID=self.identity)
            def classify(notes, unavailable=False):
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
            infra = classify([dict(annotation_level="failure", message="Failed to restore: 403 Forbidden")])
            self.assertEqual(infra.returncode, 0, infra.stderr)
            self.assertEqual(infra.stdout.strip(), "infra cache-403")
            self.assertNotEqual(classify(notes, unavailable=True).returncode, 0)


if __name__ == "__main__":
    unittest.main()
