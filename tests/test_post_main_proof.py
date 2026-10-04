#!/usr/bin/env python3
"""Producer/origin regressions shared by range selection and auto-revert."""
import copy
import importlib.util
import io
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


def objects(row, conclusion="success", job_id=300, check_id=30):
    # Run, job and check identities deliberately differ, as in the provider
    # contract exercised by cloud's legacy release proof fixtures.
    job = dict(id=job_id, run_id=row["id"], run_attempt=row["run_attempt"],
               head_sha=row["head_sha"], name="verified", status="completed", conclusion=conclusion,
               check_run_url=f"https://api.github.com/repos/{REPO}/check-runs/{check_id}")
    check = dict(id=check_id, app=dict(id=15368, slug="github-actions"), head_sha=row["head_sha"],
                 name="verified", check_suite=dict(id=row["check_suite_id"]), status="completed",
                 conclusion=conclusion,
                 details_url=f"https://github.com/{REPO}/actions/runs/{row['id']}/job/{job_id}")
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

    def test_partial_null_and_invalid_producers_are_unknown_not_ineligible(self):
        partials = [None, {}, dict(run(), repository=None), dict(run(), head_repository=None)]
        for field in ("id", "workflow_id", "check_suite_id", "run_attempt", "event", "path",
                      "head_branch", "head_sha", "repository", "head_repository"):
            missing = run()
            missing.pop(field)
            partials.extend([missing, run(**{field: None})])
        partials.extend([run(id=0), run(id=True), run(head_sha="invalid"),
                         run(head_branch=""), run(path="invalid"), run(event="")])
        for row in partials:
            with self.subTest(row=row), patch.object(proof, "paged") as jobs:
                self.assertEqual(proof.checked_verdict(row, REPO, "main", "verify.yml"), "unknown")
                jobs.assert_not_called()

    def test_partial_newer_history_never_exposes_an_older_success(self):
        newer = run(2, check_suite_id=None)
        with patch.object(proof, "api", return_value={"total_count": 2, "workflow_runs": [run(1), newer]}), \
                patch.object(proof, "paged", wraps=proof.paged) as pages:
            self.assertEqual(proof.sha_verdict(REPO, "main", "verify.yml", SHA), "unknown")
            self.assertEqual(pages.call_count, 1)  # No older producer's jobs are read.
        for invalid in (None, {}, run(2, head_sha=None), run(2, run_attempt=None)):
            with self.subTest(row=invalid), patch.object(proof, "api", return_value={"total_count": 2, "workflow_runs": [run(1), invalid]}):
                with self.assertRaisesRegex(ValueError, "identity unavailable"):
                    proof.sha_verdict(REPO, "main", "verify.yml", SHA)

    def test_history_envelope_must_be_complete_and_consistent(self):
        def rows(start, n):
            return [run(i) for i in range(start, start + n)]
        bad = ({"workflow_runs": [run(1)]}, {"total_count": None, "workflow_runs": [run(1)]},
               {"total_count": "1", "workflow_runs": [run(1)]}, {"total_count": -1, "workflow_runs": []},
               {"total_count": True, "workflow_runs": [run(1)]},
               {"total_count": 2, "workflow_runs": [run(1)]},      # omitted newest
               {"total_count": 1, "workflow_runs": [run(1), run(2)]},
               {"total_count": 1, "workflow_runs": [run(1), run(1)]},
               {"total_count": 1, "workflow_runs": None})
        for body in bad:
            with self.subTest(body=body), patch.object(proof, "api", return_value=body):
                with self.assertRaises(ValueError):
                    proof.paged("p", "workflow_runs")
        pages = [dict(total_count=101, workflow_runs=rows(1, 100)), dict(total_count=101, workflow_runs=rows(101, 1))]
        with patch.object(proof, "api", side_effect=pages):
            self.assertEqual(len(proof.paged("p", "workflow_runs")), 101)
        pages = [dict(total_count=101, workflow_runs=rows(1, 100)), dict(total_count=102, workflow_runs=rows(101, 1))]
        with patch.object(proof, "api", side_effect=pages):
            with self.assertRaises(ValueError):
                proof.paged("p", "workflow_runs")
        with patch.object(proof, "api", side_effect=[dict(total_count=600, workflow_runs=rows(i * 100 + 1, 100)) for i in range(5)]):
            with self.assertRaisesRegex(ValueError, "exhausted"):
                proof.paged("p", "workflow_runs")

    def test_omitted_newest_same_sha_producer_never_exposes_older_proof(self):
        old = run(1)
        job, check = objects(old, "success")
        for verdict in ("success", "failure"):
            job, check = objects(old, verdict)
            def api(path):
                if "/runs?" in path:   # provider advertises two rows but returns only the older
                    return {"total_count": 2, "workflow_runs": [old]}
                return {"total_count": 1, "jobs": [job]} if "/jobs?" in path else check
            with self.subTest(older=verdict), patch.object(proof, "api", side_effect=api):
                with self.assertRaisesRegex(ValueError, "inconsistent"):
                    proof.sha_verdict(REPO, "main", "verify.yml", SHA)
                with self.assertRaises(ValueError):
                    proof.last_verified(REPO, "main", "verify.yml", SHA, remote=True)

    def test_newest_same_sha_found_on_later_page(self):
        newest = run(300)
        job, check = objects(newest, "failure")
        pages = [dict(total_count=101, workflow_runs=[run(i) for i in range(1, 101)]),
                 dict(total_count=101, workflow_runs=[newest])]
        def api(path):
            if "/runs?" in path:
                return pages[int(path.rsplit("page=", 1)[1]) - 1]
            return {"total_count": 1, "jobs": [job]} if "/jobs?" in path else check
        with patch.object(proof, "api", side_effect=api):
            self.assertEqual(proof.sha_verdict(REPO, "main", "verify.yml", SHA), "failure")

    def test_ineligible_producer_is_distinct_from_unavailable_proof(self):
        for fields in (dict(event="workflow_dispatch"), dict(event="merge_group"),
                       dict(path=".github/workflows/recording.yml")):
            with self.subTest(fields=fields), patch.object(proof, "paged") as jobs:
                self.assertEqual(proof.checked_verdict(run(**fields), REPO, "main", "verify.yml"), "ineligible")
                jobs.assert_not_called()
        with patch.object(proof, "paged", return_value=[]):
            self.assertEqual(proof.checked_verdict(run(), REPO, "main", "verify.yml"), "unknown")

    def test_unavailable_proof_cannot_supply_a_base(self):
        row = run(sha="b" * 40)
        for failure in ("unknown", RuntimeError("HTTP 502")):
            with self.subTest(failure=failure), patch.object(proof, "push_runs", return_value=[row]), \
                    patch.object(proof, "api", return_value={"parents": [{"sha": row["head_sha"]}]}), \
                    patch.object(proof, "checked_verdict", side_effect=failure if isinstance(failure, Exception) else None,
                                 return_value=failure):
                if isinstance(failure, Exception):
                    with self.assertRaisesRegex(RuntimeError, "HTTP 502"):
                        proof.last_verified(REPO, "main", "verify.yml", SHA, remote=True)
                else:
                    self.assertEqual(proof.last_verified(REPO, "main", "verify.yml", SHA, remote=True), "")

    def test_optional_publish_failure_does_not_erase_verified_success(self):
        row = run(conclusion="failure")
        job, check = objects(row)
        with patch.object(proof, "paged", return_value=[job]), patch.object(proof, "api", return_value=check):
            self.assertEqual(proof.checked_verdict(row, REPO, "main", "verify.yml"), "success")

    def test_distinct_job_and_check_ids_preserve_success_and_failure(self):
        row = run()
        for verdict in ("success", "failure"):
            with self.subTest(verdict=verdict):
                job, check = objects(row, verdict)
                self.assertEqual((row["id"], job["id"], check["id"]), (1, 300, 30))
                with patch.object(proof, "paged", return_value=[job]), \
                        patch.object(proof, "api", return_value=check) as read:
                    self.assertEqual(proof.checked_verdict(row, REPO, "main", "verify.yml"), verdict)
                    read.assert_called_once_with(f"repos/{REPO}/check-runs/30")

    def test_check_url_shape_is_validated_before_any_check_fetch(self):
        row = run()
        job, check = objects(row)
        url = job["check_run_url"]
        invalid = (None, 30, url.replace("https:", "http:"), url.replace("api.github.com", "evil.invalid"),
                   url.replace("api.github.com", "user@api.github.com"),
                   url.replace("api.github.com", "api.github.com:443"),
                   url.replace(REPO, "attacker/cloud"), url + "?x=1", url + "#x", url + "/",
                   url.replace("/30", "/0"), url.replace("/30", "/-30"),
                   url.replace("/30", "/030"), url.replace("/30", "/30/31"))
        for value in invalid:
            with self.subTest(url=value), patch.object(proof, "paged", return_value=[dict(job, check_run_url=value)]), \
                    patch.object(proof, "api", return_value=check) as read:
                self.assertEqual(proof.checked_verdict(row, REPO, "main", "verify.yml"), "unknown")
                read.assert_not_called()

    def test_failed_lane_selection_requires_valid_complete_job_evidence(self):
        job = dict(id=300, run_id=9, name="rust", status="completed", conclusion="failure")
        for invalid in (dict(name=None), dict(name=""), dict(name="rust\nother"),
                        dict(run_id=None), dict(run_id=8), dict(status="mystery"),
                        dict(status="in_progress"), dict(conclusion=None), dict(conclusion="mystery")):
            with self.subTest(invalid=invalid), patch.object(proof, "paged", return_value=[dict(job, **invalid)]):
                with self.assertRaises(ValueError):
                    proof.failed_lanes(REPO, "9")
        with patch.object(proof, "paged", return_value=[job, dict(job, id=301, name="lint", conclusion="success")]):
            self.assertEqual(proof.failed_lanes(REPO, "9"), "rust")
        # A comma only matters in a lane that is joined into the list: a passing
        # lane may carry one, and a failing one is written with ';'.
        with patch.object(proof, "paged", return_value=[job, dict(job, id=301, name="a (b, c)", conclusion="success")]):
            self.assertEqual(proof.failed_lanes(REPO, "9"), "rust")
        with patch.object(proof, "paged", return_value=[dict(job, name="a (b, c)")]):
            self.assertEqual(proof.failed_lanes(REPO, "9"), "a (b; c)")
        with patch.object(proof, "api", return_value={"total_count": 2, "jobs": []}):
            with self.assertRaisesRegex(ValueError, "inconsistent"):
                proof.failed_lanes(REPO, "9")

    def test_check_id_cli_uses_the_same_strict_link_for_annotations(self):
        job, _ = objects(run())
        output = io.StringIO()
        with patch.object(proof.sys, "argv", ["proof.py", "check-id", REPO]), \
                patch.object(proof.sys, "stdin", io.StringIO(json.dumps(job))), \
                patch.object(proof.sys, "stdout", output):
            proof.main()
        self.assertEqual(output.getvalue(), "30\n")
        with patch.object(proof.sys, "argv", ["proof.py", "check-id", REPO]), \
                patch.object(proof.sys, "stdin", io.StringIO(json.dumps(dict(job, check_run_url="https://evil.invalid/30")))):
            with self.assertRaisesRegex(ValueError, "invalid job check_run_url"):
                proof.main()

    def test_linked_check_and_actual_job_must_both_match(self):
        row = run()
        job, check = objects(row)
        for field, value in (("id", job["id"]),
                             ("details_url", check["details_url"].replace("/job/300", "/job/30")),
                             ("details_url", check["details_url"].replace("/job/300", "/job/301")),
                             ("conclusion", "failure")):
            with self.subTest(field=field, value=value), patch.object(proof, "paged", return_value=[job]), \
                    patch.object(proof, "api", return_value=dict(check, **{field: value})):
                self.assertEqual(proof.checked_verdict(row, REPO, "main", "verify.yml"), "unknown")

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
                return {"total_count": len(rows), "workflow_runs": rows}
            if "/jobs?" in path:
                return {"total_count": 1, "jobs": [job]}
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

    def test_newer_genuine_failure_not_hidden_by_older_run_rerun(self):
        old = run(1, run_attempt=99)
        newest = run(2, run_attempt=1)
        old_job, old_check = objects(old, "success", job_id=900, check_id=90)
        job, check = objects(newest, "failure")
        # The older run's rerun gets newer job/check ids, but cannot replace
        # the newest approved run as the proof for this SHA.
        self.assertGreater(old_job["id"], job["id"])
        self.assertGreater(old_check["id"], check["id"])
        for rows in ([old, newest], [newest, old]):
            with self.subTest(rows=rows), patch.object(proof, "push_runs", return_value=rows), \
                    patch.object(proof, "paged", return_value=[job]) as jobs, \
                    patch.object(proof, "api", return_value=check):
                self.assertEqual(proof.sha_verdict(REPO, "main", "verify.yml", SHA), "failure")
                jobs.assert_called_once_with(f"repos/{REPO}/actions/runs/2/jobs?filter=latest", "jobs")

    def test_latest_attempt_is_selected_only_within_the_newest_run(self):
        rows = [run(1, run_attempt=99), run(2, run_attempt=1), run(2, run_attempt=2)]
        self.assertEqual(proof.latest_run(rows, SHA), rows[2])

    def test_auto_revert_uses_helper_for_event_green_guard_and_baseline(self):
        text = (ROOT / ".github/workflows/red-main.yml").read_text()
        self.assertIn('read_proof run "$REPO" main "$VERIFY_WORKFLOW" "$run_id"', text)
        self.assertIn('read_proof sha "$REPO" main "$VERIFY_WORKFLOW" "$HEAD_SHA"', text)
        self.assertIn('proof.py" remote-base "$REPO" main "$VERIFY_WORKFLOW" "$HEAD_SHA"', text)
        self.assertIn('if [ -f "$STATE_DIR/quiet" ]; then exit 0; fi', text)
        self.assertNotIn("status=success&per_page=1", text)
        self.assertNotIn("check_name=$name&filter=latest", text)


if __name__ == "__main__":
    unittest.main()
