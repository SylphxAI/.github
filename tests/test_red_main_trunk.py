#!/usr/bin/env python3
"""The red-main handler guards the repository's default branch, not a literal `main`.

A repository whose default branch is `master` (Cubeage/big2-tycoon-keel and
others) was never classified: the handler read, fetched, reverted onto and
filtered by `main`. The branch now comes from the `trunk` input, else the event
payload's default branch, else `main`. Behaviour on a master repository is
exercised in test_red_main_unavailable (the resolve step) and
test_red_main_lifecycle (the lifecycle job); this file pins the wiring.
"""
from pathlib import Path
import re
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/red-main.yml"
STARTER = ROOT / "workflow-templates/red-main.yml"
EXPRESSION = "${{ inputs.trunk || github.event.repository.default_branch || 'main' }}"


class TrunkWiringTest(unittest.TestCase):
    def setUp(self):
        self.document = yaml.safe_load(WORKFLOW.read_text())

    def test_trunk_input_defaults_to_the_repository_default_branch(self):
        trunk = self.document[True]["workflow_call"]["inputs"]["trunk"]
        self.assertEqual(trunk["type"], "string")
        self.assertEqual(trunk["default"], "")

    def test_both_jobs_resolve_the_trunk_the_same_way(self):
        for job in ("lifecycle", "red-main"):
            with self.subTest(job=job):
                self.assertEqual(self.document["jobs"][job]["env"]["TRUNK"], EXPRESSION)

    def test_no_branch_name_is_left_hardcoded(self):
        code = "\n".join(line for line in WORKFLOW.read_text().splitlines()
                         if not line.lstrip().startswith("#"))
        for literal in (r"branch=main", r"== 'main'", r'== "main"', r'" main "', r"-B main",
                        r"origin main", r'base: "main"', r"on_red.*\bmain\b.*conflicts"):
            with self.subTest(literal=literal):
                self.assertIsNone(re.search(literal, code), literal)

    def test_starter_follows_the_default_branch(self):
        caller = yaml.safe_load(STARTER.read_text())
        condition = " ".join(caller["jobs"]["red-main"]["if"].split())
        self.assertIn("github.event.workflow_run.head_branch == github.event.repository.default_branch", condition)
        self.assertNotIn("== 'main'", condition)


if __name__ == "__main__":
    unittest.main()
