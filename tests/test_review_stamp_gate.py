"""Shared review gate policy and git range regressions, without GitHub calls."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("review_stamp_gate", ROOT / ".github/actions/review-stamp-gate/review_stamp_gate.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
CFG = {"context": "ops-security/review", "requireStampLabels": ["security"], "requireStampPaths": [".github/workflows/", "auth/", "scope.json"]}


def status(state="success", creator=42, date="2026-10-01T00:00:00Z", context="ops-security/review"):
    return {"state": state, "creator": {"id": creator} if creator else None, "created_at": date, "context": context}


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

    def test_explicit_non_success_blocks_even_without_scope(self):
        for state in ("failure", "pending", "error", "unknown"):
            for enforce in (True, False):
                self.assertFalse(gate.verdict(status(state), None, {**CFG, "enforceMissing": enforce})[0])

    def test_missing_scoped_default_blocks_and_deferred_passes(self):
        self.assertFalse(gate.verdict(None, "path auth/a", CFG)[0])
        self.assertTrue(gate.verdict(None, None, CFG)[0])
        self.assertTrue(gate.verdict(None, "path auth/a", {**CFG, "enforceMissing": False})[0])
        self.assertTrue(gate.verdict(status(), "path auth/a", CFG)[0])

    def test_scope_paths_are_exact_or_directory_prefix(self):
        self.assertEqual(gate.stamp_required([], ["auth/a"], CFG), "path auth/a")
        self.assertIsNone(gate.stamp_required([], ["authorize/a", "scope.json.bak"], CFG))
        self.assertEqual(gate.stamp_required(["security"], [], CFG), "label security")

    def test_config_validation(self):
        for cfg in ({}, {**CFG, "enforceMissing": "false"}, {**CFG, "requireStampPaths": [1]}):
            with self.assertRaises(ValueError):
                gate.validate_config(cfg)

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
            original = gate.git
            with patch.object(gate, "git", side_effect=lambda *args: subprocess.check_output(["git", "-C", directory, *args], text=True).strip()):
                self.assertEqual(gate.read_config(base, "scope.json"), CFG)
                with self.assertRaises(ValueError):
                    gate.read_config(base, "../scope.json")

    def test_non_merge_group_fails_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                gate.main()
