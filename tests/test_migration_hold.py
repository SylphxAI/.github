#!/usr/bin/env python3
"""Tests for the red-main migration queue hold."""

from __future__ import annotations

import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "migration_hold", ROOT / ".github" / "actions" / "migration-hold" / "migration_hold.py")
mh = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mh)

FIX = "red-main-forward-fix"


class DecideTest(unittest.TestCase):
    def test_no_hold_passes(self):
        self.assertEqual(mh.decide("merge_group", [], [], FIX)[0], "pass")

    def test_open_hold_blocks_a_plain_pull_request(self):
        verdict, message = mh.decide("merge_group", [7], ["owner:ops"], FIX)
        self.assertEqual(verdict, "fail")
        self.assertIn("#7", message)

    def test_forward_fix_label_passes(self):
        self.assertEqual(mh.decide("merge_group", [7, 9], [FIX], FIX)[0], "pass")

    def test_only_the_queue_is_held(self):
        self.assertEqual(mh.decide("pull_request", [7], [], FIX)[0], "pass")
        self.assertEqual(mh.decide("push", [7], [], FIX)[0], "pass")


class PrNumberTest(unittest.TestCase):
    def test_parses_the_queue_ref(self):
        self.assertEqual(mh.pr_number("refs/heads/gh-readonly-queue/main/pr-4123-0a1b2c3d4e"), 4123)

    def test_unparseable(self):
        self.assertIsNone(mh.pr_number(""))
        self.assertIsNone(mh.pr_number("refs/heads/main"))


class ManifestTest(unittest.TestCase):
    def test_action_manifest_parses(self):
        import yaml
        doc = yaml.safe_load((ROOT / ".github/actions/migration-hold/action.yml").read_text())
        self.assertEqual(doc["runs"]["using"], "composite")


if __name__ == "__main__":
    unittest.main()
