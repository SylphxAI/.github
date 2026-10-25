"""Required workflow paths must propagate failures and jobs must be bounded."""

import importlib.util
import pathlib
import sys
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "workflow_contract", ROOT / ".github/actions/workflow-lint/contract.py"
)
contract = importlib.util.module_from_spec(SPEC)
sys.path.insert(0, str(ROOT / ".github/actions/workflow-lint"))
SPEC.loader.exec_module(contract)


def workflow(build="", steps="      - run: pytest", extra=""):
    return (
        "on: pull_request\njobs:\n  build:\n    runs-on: ubuntu-latest\n"
        "    timeout-minutes: 10\n" + build + "    steps:\n" + steps + "\n"
        "  ci-ok:\n    needs: [build]\n    runs-on: ubuntu-latest\n"
        "    timeout-minutes: 1\n    steps:\n      - run: echo ok\n" + extra
    )


class ContractTest(unittest.TestCase):
    def scan(self, text):
        return contract.scan_workflow("ci.yml", text)

    def test_valid(self):
        self.assertEqual(self.scan(workflow()), [])

    def test_missing_timeout(self):
        found = self.scan(workflow().replace("    timeout-minutes: 10\n", ""))
        self.assertEqual(len(found), 1)
        self.assertIn("timeout-minutes", found[0].problem)
        self.assertEqual(found[0].line, 4)

    def test_reusable_call_cannot_have_timeout(self):
        self.assertEqual(self.scan("jobs:\n  call:\n    uses: org/repo/.github/workflows/ci.yml@main\n"), [])

    def test_continue_on_error_in_required_job_and_step(self):
        for value in ("true", "${{ matrix.allow_failure }}"):
            for text in (workflow(build=f"    continue-on-error: {value}\n"),
                         workflow(steps=f"      - run: pytest\n        continue-on-error: {value}")):
                self.assertEqual(len(self.scan(text)), 1)
        self.assertEqual(self.scan(workflow(build="    continue-on-error: false\n")), [])

    def test_failure_mask_in_required_run(self):
        for command in ("pytest || true", "pytest ||true", "pytest || \\\n          true"):
            self.assertEqual(len(self.scan(workflow(steps=f"      - run: |\n          {command}"))), 1)

    def test_comments_and_quoted_text_are_not_commands(self):
        for command in ("echo 'pytest || true'", "echo '||' true", "pytest # || true"):
            self.assertEqual(self.scan(workflow(steps=f"      - run: |\n          {command}")), [])

    def test_transitive_needs_block_sequence_and_scalar(self):
        text = workflow().replace("needs: [build]", "needs:\n      - build")
        text = text.replace("    steps:\n      - run: pytest", "    needs: setup\n    steps:\n      - run: pytest")
        text += "  setup:\n    runs-on: ubuntu-latest\n    timeout-minutes: 1\n    steps:\n      - run: setup || true\n"
        self.assertEqual(len(self.scan(text)), 1)

    def test_optional_jobs_can_mask_failures_but_need_timeout(self):
        extra = "  report:\n    runs-on: ubuntu-latest\n    timeout-minutes: 1\n    continue-on-error: true\n    steps:\n      - run: report || true\n"
        self.assertEqual(self.scan(workflow(extra=extra)), [])
        self.assertEqual(len(self.scan(workflow(extra=extra.replace("    timeout-minutes: 1\n", "")))), 1)

    def test_without_aggregate_every_job_is_required(self):
        self.assertEqual(len(self.scan("jobs:\n  test:\n    timeout-minutes: 1\n    steps:\n      - run: pytest || true\n")), 1)

    def test_flow_style_jobs_are_parsed(self):
        self.assertEqual(len(self.scan("jobs: {test: {steps: [{run: 'pytest || true'}]}}")), 2)

    def test_shared_aggregates_and_api_aggregate(self):
        text = workflow(build="    continue-on-error: true\n").replace("  ci-ok:", "  verdict:")
        text = text.replace("      - run: echo ok", "      - uses: SylphxAI/.github/.github/actions/needs-pass@main")
        self.assertEqual(len(self.scan(text)), 1)
        # An API aggregate without local needs must not leave other jobs unchecked.
        self.assertEqual(len(self.scan(workflow(build="    continue-on-error: true\n").replace("    needs: [build]\n", ""))), 1)

    def test_cyclic_graph_terminates(self):
        text = workflow(build="    needs: ci-ok\n    continue-on-error: true\n")
        self.assertEqual(len(self.scan(text)), 1)

    def test_delivered_or_unreadable_property_skips(self):
        for state in (True, None):
            with patch.dict(contract.os.environ, {"GITHUB_ACTIONS": "true"}, clear=True), \
                    patch.object(contract, "delivered", return_value=state), \
                    patch.object(contract, "scan_dir", side_effect=AssertionError("must skip")), \
                    patch("builtins.print"):
                self.assertEqual(contract.main(), 0)

    def test_main_reports_then_enforces(self):
        findings = self.scan(workflow(build="    continue-on-error: true\n"))
        for mode, expected in (("report-only", 0), ("enforce", 1)):
            with patch.dict(contract.os.environ, {"CONTRACT_MODE": mode}, clear=True), \
                    patch.object(contract, "delivered", return_value=False), \
                    patch.object(contract, "scan_dir", return_value=findings), \
                    patch("builtins.print") as output:
                self.assertEqual(contract.main(), expected)
                self.assertIn("1 violation(s)", output.call_args.args[0])

    def test_modes(self):
        with self.assertRaises(ValueError):
            contract.exit_code([], "unknown")
        findings = self.scan(workflow(build="    continue-on-error: true\n"))
        self.assertEqual(contract.exit_code(findings, "report-only"), 0)
        self.assertEqual(contract.exit_code(findings, "enforce"), 1)
        self.assertEqual(contract.exit_code([], "enforce"), 0)


if __name__ == "__main__":
    unittest.main()
