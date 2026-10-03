"""Shared review gate policy and git range regressions, without GitHub calls."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / ".github/actions/review-stamp-gate"
spec = importlib.util.spec_from_file_location("review_stamp_gate", ACTION / "review_stamp_gate.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
CFG = {
    "context": "ops-security/review", "enforceMissing": True,
    "requireStampLabels": ["security"],
    "requireStampPaths": [".github/workflows/", "auth/", "scope.json"],
    "requireStampPathGlobs": ["**/migrations/**"],
    "productReview": {"context": "product/final", "trustedCreatorIds": [77]},
}
POLICY = json.loads((ACTION / "policy.json").read_text())
POLICY = {**POLICY, "opsReview": {**POLICY["opsReview"], "trustedCreatorIds": [42]}}
QA_POLICY = {**POLICY, "opsReview": {**POLICY["opsReview"], "qaReviewerCreatorIds": [55]}}


def status(state="success", creator=42, date="2026-10-01T00:00:00Z", context="ops-security/review", description="PASS independent review"):
    return {"state": state, "creator": {"id": creator} if creator else None, "created_at": date, "context": context, "description": description}


def review(statuses, files=None, labels=None, repo="SylphxAI/anymd", cfg=CFG, author=123, policy=POLICY):
    return gate.review_verdict(repo, labels or [], files or ["README.md"], statuses, cfg, policy, {42}, author)


class GateTest(unittest.TestCase):
    def test_trusted_ids(self):
        self.assertEqual(gate.trusted_ids("42, 56"), {42, 56})
        for bad in ("", "0", "42,", "bob", "-1"):
            with self.assertRaises(ValueError):
                gate.trusted_ids(bad)

    def test_latest_trusted_context(self):
        statuses = [status("failure", 99), status("pending", context="other"), status(creator=None), status()]
        self.assertEqual(gate.latest_stamp(statuses, CFG["context"], {42})["state"], "success")
        self.assertIsNone(gate.latest_stamp(statuses, CFG["context"], {123}))

    def test_newest_wins_and_ties_keep_api_order(self):
        self.assertEqual(gate.latest_stamp([status(), status("failure", date="2026-10-01T01:00:00Z")], CFG["context"], {42})["state"], "failure")
        self.assertEqual(gate.latest_stamp([status("pending"), status()], CFG["context"], {42})["state"], "pending")

    def test_explicit_ops_veto_blocks_even_ordinary_product_change(self):
        for state in ("failure", "pending", "error", "unknown"):
            self.assertFalse(review([status(state), status(creator=77, context="product/final")])[0])

    def test_missing_stamp_blocks_everywhere(self):
        for repo in ("SylphxAI/anymd", "SylphxAI/kalkas", "SylphxAI/.github", "SylphxAI/cloud"):
            self.assertFalse(review([], repo=repo)[0])

    def test_wrong_stamper_blocks(self):
        self.assertFalse(review([status(creator=99)], files=["auth/a"])[0])
        self.assertFalse(review([status(creator=99, context="product/final")])[0])

    def test_product_ordinary_change_requires_owning_lane_reviewer(self):
        self.assertTrue(review([status(creator=77, context="product/final")])[0])
        self.assertFalse(review([status()])[0])

    def test_class_path_requires_ops_even_in_product_repo(self):
        product = status(creator=77, context="product/final")
        for path in ("auth/a", "src/auth/a", "migrations/001.sql", "database/migrations/001.sql", "payment/pay.ts", "src/billing-core/pay.ts", ".github/workflows/ci.yml"):
            self.assertFalse(review([product], files=[path])[0], path)
            self.assertTrue(review([status(), product], files=[path])[0], path)

    def test_platform_requires_ops_for_every_path(self):
        for repo in POLICY["platformRepositories"]:
            self.assertFalse(review([status(creator=77, context="product/final")], repo=repo)[0])
            self.assertTrue(review([status()], repo=repo)[0])

    def test_ops_requires_pass_description_prefix(self):
        for description in ("LGTM", "pass", "", None):
            self.assertFalse(review([status(description=description)], files=["auth/a"])[0])
        self.assertTrue(review([status(description="PASS: reviewed")], files=["auth/a"])[0])

    def test_labels_can_only_add_ops_requirement_never_downgrade_path(self):
        product = status(creator=77, context="product/final")
        for label in ("security", "money", "money-path", "migration"):
            self.assertFalse(review([product], labels=[label])[0])
            self.assertTrue(review([status()], labels=[label])[0])
        for labels in ([], ["not-security"], ["review:product"], ["security-exempt"]):
            self.assertFalse(review([product], files=["auth/a"], labels=labels)[0])
        relaxed = {**CFG, "requireStampLabels": [], "requireStampPaths": [], "requireStampPathGlobs": []}
        self.assertFalse(review([product], files=["migrations/001.sql"], cfg=relaxed)[0])
        self.assertFalse(review([product], labels=["money"], cfg=relaxed)[0])

    def test_distinct_identity_author_cannot_self_stamp(self):
        self.assertFalse(review([status(creator=77, context="product/final")], author=77)[0])
        self.assertTrue(review([status(creator=77, context="product/final")], author=123)[0])

    def test_shared_desk_identity_records_independence_limit(self):
        cfg = {**CFG, "productReview": {"context": "ops-security/review", "trustedCreatorIds": [8020099]}}
        ok, reason = review([status(creator=8020099)], cfg=cfg, author=8020099)
        self.assertTrue(ok)
        self.assertIn("cannot prove reviewer independence", reason)

    def test_scope_paths_are_exact_or_directory_prefix(self):
        self.assertEqual(gate.stamp_required([], ["auth/a"], CFG), "path auth/a")
        self.assertIsNone(gate.stamp_required([], ["authorize/a", "scope.json.bak"], CFG))
        self.assertEqual(gate.stamp_required(["security"], [], CFG), "label security")

    def test_config_validation_forbids_missing_enforcement_and_empty_reviewer(self):
        for cfg in ({}, {**CFG, "enforceMissing": False}, {**CFG, "requireStampPaths": [1]}, {**CFG, "productReview": {"context": "product/final", "trustedCreatorIds": []}}):
            with self.assertRaises(ValueError):
                gate.validate_config(cfg)
        self.assertEqual(gate.validate_config(CFG), CFG)

    def test_qa_app_approval_passes_alone_where_ops_is_required(self):
        for kwargs in ({"files": ["auth/a"]}, {"labels": ["money"]}, {"repo": "SylphxAI/.github"}, {"repo": "SylphxAI/cloud"}):
            self.assertTrue(review([status(creator=55)], policy=QA_POLICY, **kwargs)[0], kwargs)
        # The same stamp without the QA slot filled is untrusted.
        self.assertFalse(review([status(creator=55)], files=["auth/a"])[0])

    def test_qa_app_counts_as_product_reviewer_in_its_context(self):
        self.assertTrue(review([status(creator=55, context="product/final")], policy=QA_POLICY)[0])
        self.assertFalse(review([status(creator=55)], policy=QA_POLICY)[0])

    def test_ops_stamp_still_passes_with_qa_slot_filled(self):
        self.assertTrue(review([status()], files=["auth/a"], policy=QA_POLICY)[0])
        self.assertTrue(review([status()], repo="SylphxAI/.github", policy=QA_POLICY)[0])

    def test_untrusted_creator_refused_with_qa_slot_filled(self):
        for creator in (99, 8020099, None):
            self.assertFalse(review([status(creator=creator)], files=["auth/a"], policy=QA_POLICY)[0], creator)
            self.assertFalse(review([status(creator=creator, context="product/final")], policy=QA_POLICY)[0], creator)

    def test_qa_stamp_obeys_pass_prefix_and_veto(self):
        self.assertFalse(review([status(creator=55, description="LGTM")], files=["auth/a"], policy=QA_POLICY)[0])
        for state in ("failure", "pending", "error"):
            self.assertFalse(review([status(state, creator=55), status(creator=77, context="product/final")], policy=QA_POLICY)[0], state)
        newest_qa_veto = [status(), status("failure", creator=55, date="2026-10-01T01:00:00Z")]
        self.assertFalse(review(newest_qa_veto, files=["auth/a"], policy=QA_POLICY)[0])

    def test_qa_slot_rejects_malformed_ids(self):
        for bad in ([0], [-1], ["55"], [True], 55, None):
            policy = {**POLICY, "opsReview": {**POLICY["opsReview"], "qaReviewerCreatorIds": bad}}
            with self.assertRaises(ValueError):
                review([status()], policy=policy)
        self.assertEqual(gate.qa_reviewers(POLICY), set())

    def test_actual_policy_and_adopter_require_review(self):
        policy = json.loads((ACTION / "policy.json").read_text())
        self.assertEqual(policy["opsReview"], {"context": "ops-security/review", "trustedCreatorIds": [8020099], "qaReviewerCreatorIds": [], "descriptionPrefix": "PASS"})
        self.assertIn("SylphxAI/.github", policy["platformRepositories"])
        self.assertTrue(gate.validate_config(json.loads((ROOT / ".github/review-stamp.json").read_text()))["enforceMissing"])

    def test_queue_refs_and_complete_group(self):
        a, b, c = "a" * 40, "b" * 40, "c" * 40
        refs = gate.queue_refs_by_sha(f"{a}\trefs/heads/gh-readonly-queue/main/pr-1-{c}\n{b} refs/heads/gh-readonly-queue/main/pr-2-{a}\n{c} refs/heads/feature")
        self.assertEqual(refs, {a: 1, b: 2})
        self.assertEqual(gate.group_pull_requests([a, b, c], set(), refs), ([(1, a), (2, b)], [c]))
        self.assertEqual(gate.group_pull_requests([a, b, c], {c}, refs), ([(1, a), (2, b)], []))

    def test_status_pagination_finds_trusted_stamp_after_first_page(self):
        with patch.object(gate, "api", side_effect=[[status(creator=99)] * 100, [status("failure")]]) as api:
            self.assertEqual(gate.latest_stamp(gate.statuses_for("a" * 40), CFG["context"], {42})["state"], "failure")
            self.assertEqual(api.call_count, 2)

    def test_base_config_cannot_be_loosened_by_queued_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(["git", "init", "-q", directory], check=True)
            path = Path(directory) / "scope.json"
            path.write_text(json.dumps(CFG))
            subprocess.run(["git", "-C", directory, "add", "scope.json"], check=True)
            subprocess.run(["git", "-C", directory, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base"], check=True)
            base = subprocess.check_output(["git", "-C", directory, "rev-parse", "HEAD"], text=True).strip()
            path.write_text(json.dumps({**CFG, "enforceMissing": False}))
            with patch.object(gate, "git", side_effect=lambda *args: subprocess.check_output(["git", "-C", directory, *args], text=True).strip()):
                self.assertEqual(gate.read_config(base, "scope.json"), CFG)
                with self.assertRaises(ValueError):
                    gate.read_config(base, "../scope.json")

    def test_own_repo_gate_runs_from_base_not_queued_head(self):
        steps = yaml.safe_load((ROOT / ".github/workflows/project-control.yml").read_text())["jobs"]["review-stamp"]["steps"]
        gates = [s for s in steps if "review-stamp-gate" in s.get("uses", "")]
        self.assertEqual({(s["uses"], s["if"]) for s in gates}, {
            ("./.review-gate-base/.github/actions/review-stamp-gate", "steps.gate-base.outputs.from == 'base'"),
            ("./.github/actions/review-stamp-gate", "steps.gate-base.outputs.from == 'head'"),
        })
        script = next(s for s in steps if s.get("id") == "gate-base")["run"]
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            run = lambda *a: subprocess.check_output(["git", "-C", directory, "-c", "user.name=T", "-c", "user.email=t@example.invalid", *a], text=True).strip()
            run("init", "-q")
            (repo / "README.md").write_text("x")
            run("add", "README.md"); run("commit", "-qm", "pre-adoption")
            pre = run("rev-parse", "HEAD")
            gate_dir = repo / ".github/actions/review-stamp-gate"
            gate_dir.mkdir(parents=True)
            for name in ("review_stamp_gate.py", "policy.json", "action.yml"):
                (gate_dir / name).write_text((ACTION / name).read_text())
            run("add", "."); run("commit", "-qm", "base")
            base = run("rev-parse", "HEAD")
            # The queued PR adds an untrusted id to both trust lists of its own policy.
            hostile = json.loads((gate_dir / "policy.json").read_text())
            hostile["opsReview"]["trustedCreatorIds"].append(999)
            hostile["opsReview"]["qaReviewerCreatorIds"].append(999)
            (gate_dir / "policy.json").write_text(json.dumps(hostile))
            run("commit", "-qam", "widen trust")

            def stage(base_sha):
                out = repo / "out"
                out.write_text("")
                subprocess.run(["bash", "-c", script], cwd=directory, check=True, capture_output=True,
                               env={**os.environ, "BASE_SHA": base_sha, "GITHUB_OUTPUT": str(out)})
                return out.read_text().strip()

            self.assertEqual(stage(base), "from=base")
            graded = repo / ".review-gate-base/.github/actions/review-stamp-gate"
            spec = importlib.util.spec_from_file_location("base_gate", graded / "review_stamp_gate.py")
            base_gate = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(base_gate)
            policy = json.loads((graded / "policy.json").read_text())
            self.assertNotIn(999, policy["opsReview"]["trustedCreatorIds"])
            self.assertEqual(base_gate.qa_reviewers(policy), set())
            ok, reason = base_gate.review_verdict("SylphxAI/.github", [], [".github/actions/review-stamp-gate/policy.json"],
                                                  [status(creator=999)], CFG, policy, {8020099}, 123)
            self.assertFalse(ok, reason)
            self.assertEqual(stage(pre), "from=head")

    def test_non_merge_group_fails_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                gate.main()
