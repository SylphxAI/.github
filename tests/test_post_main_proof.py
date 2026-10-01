#!/usr/bin/env python3
"""Producer/origin regressions shared by range selection and auto-revert."""
import copy
import importlib.util
import json
import pathlib
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = ROOT / ".github/actions/ci-range/post_main.py"
spec = importlib.util.spec_from_file_location("post_main", SOURCE)
proof = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proof)
REPO = "SylphxAI/cloud"
SHA = "a" * 40


def run(id=1, sha=SHA, **fields):
    row = dict(id=id, event="push", head_branch="main", path=".github/workflows/verify.yml",
               repository=dict(id=12, full_name=REPO), head_repository=dict(id=12, full_name=REPO),
               workflow_id=34, check_suite_id=56, head_sha=sha, run_attempt=1,
               conclusion="failure")
    row.update(fields)
    return row


def objects(row, conclusion="success"):
    job = dict(id=77, run_id=row["id"], run_attempt=1, head_sha=row["head_sha"], name="verified",
               status="completed", conclusion=conclusion,
               check_run_url=f"https://api.github.com/repos/{REPO}/check-runs/77")
    check = dict(id=77, app=dict(id=15368, slug="github-actions"), head_sha=row["head_sha"],
                 name="verified", check_suite=dict(id=row["check_suite_id"]), status="completed",
                 conclusion=conclusion, details_url=f"https://github.com/{REPO}/actions/runs/{row['id']}/job/77")
    return job, check


class ProofTest(unittest.TestCase):
    def test_reusable_payload_is_the_same_canonical_helper(self):
        lines = (ROOT / ".github/workflows/red-main.yml").read_text().splitlines()
        start = lines.index("      PROOF_PY: |") + 1
        body = []
        for line in lines[start:]:
            if line.strip() and not line.startswith("        "):
                break
            body.append(line[8:])
        self.assertEqual("\n".join(body).rstrip(), SOURCE.read_text().rstrip())

    def test_only_full_post_main_origin_is_proof(self):
        self.assertTrue(proof.full_push(run(), REPO, "main", "verify.yml", SHA))
        for fields in (dict(event="workflow_dispatch"), dict(event="merge_group"),
                       dict(path=".github/workflows/recording.yml"), dict(head_branch="sylphx-verify/x"),
                       dict(head_sha="b" * 40), dict(head_repository=dict(id=88, full_name="attacker/cloud"))):
            self.assertFalse(proof.full_push(run(**fields), REPO, "main", "verify.yml", SHA), fields)

    def test_optional_publish_failure_does_not_erase_verified_success(self):
        row = run(conclusion="failure")
        job, check = objects(row)
        with patch.object(proof, "paged", return_value=[job]), patch.object(proof, "api", return_value=check):
            self.assertEqual(proof.checked_verdict(row, REPO, "main", "verify.yml"), "success")

    def test_unknown_producer_or_job_membership_never_admits(self):
        row = run()
        job, check = objects(row)
        bad_checks = []
        for field, value in (("app", dict(id=1, slug="github-actions")), ("check_suite", dict(id=1)),
                             ("details_url", "https://example.invalid/run/1"), ("head_sha", "b" * 40)):
            bad = copy.deepcopy(check)
            bad[field] = value
            bad_checks.append(bad)
        for bad in bad_checks:
            with patch.object(proof, "paged", return_value=[job]), patch.object(proof, "api", return_value=bad):
                self.assertEqual(proof.checked_verdict(row, REPO, "main", "verify.yml"), "unknown")
        for field, value in (("run_attempt", 2), ("run_id", 2), ("check_run_url", "https://example.invalid/x")):
            bad = dict(job, **{field: value})
            with patch.object(proof, "paged", return_value=[bad]):
                self.assertEqual(proof.checked_verdict(row, REPO, "main", "verify.yml"), "unknown")

    def test_diagnostic_and_merge_group_success_cannot_mask_main_failure(self):
        genuine = run(id=1)
        rows = [genuine, run(id=2, event="workflow_dispatch"), run(id=3, event="merge_group"),
                run(id=4, path=".github/workflows/recording.yml")]
        job, check = objects(genuine, "failure")
        def api(path):
            if "/runs?" in path:
                return {"workflow_runs": rows}
            if "/jobs?" in path:
                return {"jobs": [job]}
            return check
        with patch.object(proof, "api", side_effect=api):
            self.assertEqual(proof.sha_verdict(REPO, "main", "verify.yml", SHA), "failure")

    def test_base_is_newest_ancestor_not_newest_successful_workflow(self):
        bad, good = "b" * 40, "c" * 40
        rows = [run(id=1, sha=bad), run(id=2, sha=good)]
        # The later diagnostic/recording success is deliberately absent from
        # push_runs; the genuine failure at the closer ancestor still decides.
        with patch.object(proof, "push_runs", return_value=rows), \
                patch.object(proof, "checked_verdict", side_effect=["failure", "success"]), \
                patch.object(proof.subprocess, "run", return_value=type("Result", (),
                             {"returncode": 0, "stdout": f"{bad}\n{good}\n"})()):
            self.assertEqual(proof.last_verified(REPO, "main", "verify.yml"), good)

    def test_newer_genuine_failure_not_hidden_by_older_success(self):
        self.assertEqual(proof.latest_run([run(1), run(2)], SHA)["id"], 2)

    def test_auto_revert_uses_helper_for_event_green_guard_and_baseline(self):
        text = (ROOT / ".github/workflows/red-main.yml").read_text()
        self.assertIn('proof.py" run "$REPO" main "$VERIFY_WORKFLOW" "$run_id"', text)
        self.assertIn('proof.py" sha "$REPO" main "$VERIFY_WORKFLOW" "$HEAD_SHA"', text)
        self.assertIn('proof.py" remote-base "$REPO" main "$VERIFY_WORKFLOW" "$HEAD_SHA"', text)
        self.assertIn('if [ -f "$STATE_DIR/quiet" ]; then exit 0; fi', text)
        self.assertNotIn("status=success&per_page=1", text)
        self.assertNotIn("check_name=$name&filter=latest", text)


if __name__ == "__main__":
    unittest.main()
