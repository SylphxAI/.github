"""Exercise the io prerequisite rules through the shared workflow scanner."""

import os
import unittest
from unittest.mock import patch

from test_workflow_contract import contract

SHA = "a" * 40
PINNED = "org/action@" + SHA
GUARD = "github.event.workflow_run.head_repository.full_name == github.repository"


def workflow(steps=None, runner="sylphx-linux-io", permissions="{contents: read}",
             trigger="pull_request", extra=""):
    steps = steps or "      - uses: " + PINNED
    return (f"on: {trigger}\njobs:\n  review:\n    runs-on: {runner}\n"
            f"    timeout-minutes: 5\n    permissions: {permissions}\n"
            + extra + "    steps:\n" + steps + "\n")


class IoPoolContractTest(unittest.TestCase):
    def scan(self, text):
        return [f for f in contract.scan_workflow("ci.yml", text) if "io pool:" in f.problem]

    def test_pinned_remote_action_and_standard_jobs(self):
        self.assertEqual(self.scan(workflow()), [])
        self.assertEqual(self.scan(workflow("      - run: echo ok", runner="sylphx-linux-standard")), [])

    def test_inline_run_is_always_blocking(self):
        found = self.scan(workflow("      - run: echo unsafe"))
        self.assertEqual(len(found), 1)
        self.assertIn("inline run", found[0].problem)
        self.assertEqual(contract.exit_code(found, "report-only"), 1)

    def test_mutable_and_invalid_remote_pins(self):
        for target in ("org/action@v4", "org/action@main", "org/action@" + "a" * 39,
                       "org/action@" + "z" * 40, "docker://alpine:latest"):
            with self.subTest(target=target):
                self.assertIn("40-hex", self.scan(workflow("      - uses: " + target))[0].problem)

    def test_expression_and_list_runner_are_conservative(self):
        for runner in ("${{ false && 'sylphx-linux-io' || 'sylphx-linux-standard' }}",
                       "[self-hosted, sylphx-linux-io]", "{group: pool, labels: sylphx-linux-io}"):
            self.assertTrue(self.scan(workflow("      - run: echo unsafe", runner=runner)))

    def checkout(self, ref=SHA, repository="SylphxAI/.github", path="shared", extra=""):
        return (f"      - uses: actions/checkout@{SHA}\n" + extra +
                f"        with:\n          repository: {repository}\n          ref: {ref}\n          path: {path}\n")

    def test_local_action_requires_preceding_pinned_shared_checkout(self):
        local = "      - uses: ./shared/.github/actions/pr-review"
        self.assertEqual(self.scan(workflow(self.checkout() + local)), [])
        cases = (local, local + "\n" + self.checkout(),
                 self.checkout(ref="${{ github.sha }}") + local,
                 self.checkout(ref="${{ github.event.workflow_run.head_sha }}") + local,
                 self.checkout(ref="main") + local,
                 self.checkout(repository="org/repo") + local,
                 self.checkout(path="other") + local,
                 self.checkout(extra="        if: false\n") + local,
                 self.checkout() + self.checkout(repository="org/repo") + local)
        for steps in cases:
            with self.subTest(steps=steps):
                self.assertTrue(self.scan(workflow(steps)))

    def test_no_remote_composite_recursion(self):
        self.assertEqual(self.scan(workflow("      - uses: SylphxAI/.github/.github/actions/pr-review@" + SHA)), [])

    def test_workflow_run_fork_routing(self):
        safe = "${{ " + GUARD + " && 'sylphx-linux-io' || 'sylphx-linux-standard' }}"
        self.assertEqual(self.scan(workflow(runner=safe, trigger="[workflow_run]")), [])
        self.assertEqual(self.scan(workflow(trigger="workflow_run", extra="    if: " + GUARD + "\n")), [])
        for runner in ("sylphx-linux-io",
                       "${{ " + GUARD + " || 'sylphx-linux-io' }}",
                       "${{ true && 'sylphx-linux-io' || " + GUARD + " }}",
                       "${{ ! (" + GUARD + ") && 'sylphx-linux-io' || 'standard' }}"):
            with self.subTest(runner=runner):
                self.assertTrue(self.scan(workflow(runner=runner, trigger="{workflow_run: {workflows: [CI], types: [completed]}}")))

    def test_permissions_must_be_explicit_and_minimal(self):
        for permissions in ("write-all", "read-all", "{}", "{contents: write}",
                            "{contents: read, packages: write}", "{contents: read, checks: write}"):
            self.assertTrue(self.scan(workflow(permissions=permissions)))
        inherited = workflow().replace("    permissions: {contents: read}\n", "")
        self.assertTrue(self.scan("permissions: {contents: read}\n" + inherited))
        reviewer = "      - uses: SylphxAI/.github/.github/actions/pr-review@" + SHA
        self.assertEqual(self.scan(workflow(reviewer, permissions="{contents: read, actions: read, pull-requests: read, id-token: write, checks: write, statuses: write}")), [])

    def test_secret_allowlist_and_inherited_env(self):
        reviewer = "      - uses: SylphxAI/.github/.github/actions/pr-review@" + SHA
        token = "\n        with:\n          token: ${{ secrets.REVIEWER_TOKEN }}"
        self.assertTrue(self.scan(workflow(reviewer + token)))
        with patch.dict(os.environ, {"IO_REVIEWER_SECRETS": "REVIEWER_TOKEN"}):
            self.assertEqual(self.scan(workflow(reviewer + token)), [])
            self.assertTrue(self.scan(workflow("      - uses: " + PINNED + token)))
            self.assertTrue(self.scan(workflow(reviewer + token.replace("REVIEWER_TOKEN", "OTHER"))))
            self.assertTrue(self.scan(workflow(reviewer + token.replace("secrets.REVIEWER_TOKEN", "secrets[inputs.name]"))))
        inherited = "env:\n  SECRET: ${{ secrets.DEPLOY_KEY }}\n"
        self.assertTrue(self.scan(inherited + workflow()))

    def test_matrix_io_is_checked(self):
        text = workflow("      - run: echo unsafe", runner="${{ matrix.runner }}",
                        extra="    strategy:\n      matrix:\n        runner: [sylphx-linux-io, sylphx-linux-standard]\n")
        self.assertTrue(self.scan(text))


if __name__ == "__main__":
    unittest.main()
