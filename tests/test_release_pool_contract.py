"""Protect the dedicated release runner pool through the shared scanner."""

import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from test_workflow_contract import contract


def workflow(trigger="workflow_dispatch", runner="sylphx-linux-release", extra=""):
    return (f"on: {trigger}\njobs:\n  publish:\n    runs-on: {runner}\n"
            "    timeout-minutes: 5\n" + extra + "    steps:\n      - run: echo publish\n")


MAIN = "\n  push:\n    branches: [main]"


class ReleasePoolContractTest(unittest.TestCase):
    def scan(self, text):
        return [finding for finding in contract.scan_workflow("release.yml", text)
                if "release pool:" in finding.problem]

    def test_allowed_trigger_subsets_and_release_label_variants(self):
        triggers = ("workflow_dispatch", "merge_group", "[workflow_dispatch, merge_group]",
                    MAIN, MAIN + "\n  workflow_dispatch:\n  merge_group:",
                    "{push: {branches: [main], paths: ['src/**']}, workflow_dispatch: {}}")
        for trigger in triggers:
            for runner in ("sylphx-linux-release", "sylphx-linux-release-standard",
                           "sylphx-linux-release-xlarge", "sylphx-linux-release2"):
                with self.subTest(trigger=trigger, runner=runner):
                    self.assertEqual(self.scan(workflow(trigger, runner)), [])

    def test_disallowed_events_are_blocking_even_in_report_only_mode(self):
        for event in ("pull_request", "pull_request_target", "workflow_call", "workflow_run",
                      "repository_dispatch", "schedule", "[workflow_dispatch, pull_request]",
                      "{push: {branches: [main]}, schedule: [{cron: '0 * * * *'}]}"):
            with self.subTest(event=event):
                found = self.scan(workflow(event))
                self.assertEqual(len(found), 1)
                self.assertIn("pull_request is never allowed", found[0].problem)
                self.assertEqual(contract.exit_code(found, "report-only"), 1)

    def test_job_guards_do_not_exempt_a_pull_request_workflow(self):
        for condition in ("github.event_name == 'push'", "false",
                          "${{ github.event_name != 'pull_request' }}"):
            with self.subTest(condition=condition):
                self.assertTrue(self.scan(workflow("[pull_request, workflow_dispatch]",
                                                   extra=f"    if: {condition}\n")))

    def test_push_requires_an_explicit_main_only_branch_filter(self):
        triggers = ("push", "[push, workflow_dispatch]", "{push: {}}",
                    "{push: {branches: [develop]}}", "{push: {branches: [main, develop]}}",
                    "{push: {branches: ['*']}}", "{push: {branches: ['main*']}}",
                    "{push: {branches-ignore: [develop]}}", "{push: {tags: ['v*']}}",
                    "{push: {branches: [main], tags: ['v*']}}",
                    "{push: {branches: [main], tags-ignore: ['test*']}}")
        for trigger in triggers:
            with self.subTest(trigger=trigger):
                found = self.scan(workflow(trigger))
                self.assertEqual(len(found), 1)
                self.assertIn("branches: [main]", found[0].problem)

    def test_empty_or_absent_trigger_is_not_release_eligible(self):
        for text in (workflow("[]"), workflow("{}"), workflow().replace("on: workflow_dispatch\n", "")):
            with self.subTest(text=text):
                self.assertTrue(self.scan(text))

    def test_list_mapping_and_expression_selectors_are_checked(self):
        runners = ("[self-hosted, sylphx-linux-release-xlarge]",
                   "{group: delivery, labels: sylphx-linux-release}",
                   "\n      - self-hosted\n      - sylphx-linux-release",
                   "${{ github.event_name == 'workflow_dispatch' && 'sylphx-linux-release' || 'sylphx-linux-standard' }}",
                   "${{ format('sylphx-linux-{0}', 'release') }}")
        for runner in runners:
            with self.subTest(runner=runner):
                self.assertTrue(self.scan(workflow("[pull_request, workflow_dispatch]", runner)))

    def test_matrix_release_selector_is_checked(self):
        extra = "    strategy:\n      matrix:\n        runner: [sylphx-linux-standard, sylphx-linux-release-xlarge]\n"
        self.assertTrue(self.scan(workflow("pull_request", "${{ matrix.runner }}", extra)))
        self.assertEqual(self.scan(workflow("workflow_dispatch", "${{ matrix.runner }}", extra)), [])

    def test_workflow_call_default_cannot_hide_a_release_selector(self):
        trigger = "\n  workflow_call:\n    inputs:\n      runner:\n        type: string\n        default: sylphx-linux-release"
        self.assertTrue(self.scan(workflow(trigger, "${{ inputs.runner }}")))

    def test_unrelated_runner_group_and_matrix_metadata_are_not_labels(self):
        self.assertEqual(self.scan(workflow("pull_request",
            "{group: sylphx-linux-release, labels: sylphx-linux-standard}")), [])
        extra = ("    strategy:\n      matrix:\n        runner: [sylphx-linux-standard]\n"
                 "        description: [sylphx-linux-release]\n")
        self.assertEqual(self.scan(workflow("pull_request", "${{ matrix.runner }}", extra)), [])

    def test_other_runner_pools_keep_their_current_trigger_contract(self):
        for runner in ("sylphx-linux-standard", "sylphx-linux-standard-merge",
                       "sylphx-linux-xlarge", "ubuntu-latest", "other-sylphx-linux-release"):
            with self.subTest(runner=runner):
                self.assertEqual(self.scan(workflow("pull_request", runner)), [])

    def test_directory_scan_resolves_a_caller_supplied_release_label(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            folder = root / ".github/workflows"
            folder.mkdir(parents=True)
            (folder / "ci.yml").write_text(
                "on: pull_request\njobs:\n  caller:\n    uses: ./.github/workflows/called.yml\n"
                "    with:\n      runner: sylphx-linux-release\n")
            (folder / "called.yml").write_text(workflow(
                "\n  workflow_call:\n    inputs:\n      runner:\n        type: string",
                "${{ inputs.runner }}"))
            with patch.dict(os.environ, {"GITHUB_REPOSITORY": "Cubeage/example"}, clear=True):
                found = [f for f in contract.scan_dir(root) if "release pool:" in f.problem]
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0].file, ".github/workflows/called.yml")
            self.assertTrue(found[0].blocking)

    def test_findings_point_to_the_runner_declaration(self):
        found = self.scan(workflow("pull_request"))
        self.assertEqual(found[0].line, 4)
        self.assertEqual(found[0].file, "release.yml")


if __name__ == "__main__":
    unittest.main()
