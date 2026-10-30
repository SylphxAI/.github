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

    def test_repository_workflows_have_no_manual_gates(self):
        # Existing timeout/failure-mask findings have a report-only rollout.
        self.assertEqual([f for f in contract.scan_dir(ROOT) if ": manual " in f.problem], [])

    def test_manual_approval_actions_are_forbidden(self):
        problem = "job build: manual approval gate is forbidden; deployments and checks must run automatically"
        for action in ("trstringer/manual-approval@v1", "org/manual-approval@" + "a" * 40,
                       "./.github/actions/manual-approval"):
            with self.subTest(action=action):
                found = self.scan(workflow(steps=f"      - uses: {action}"))
                self.assertEqual(found, [contract.Finding("ci.yml", 7, problem, blocking=True)])

    def test_manual_booking_in_job_and_step_conditions_and_run(self):
        problem = (
            "job build: manual maintenance-window booking (window:booked) is forbidden; "
            "use rolling updates or workload-scheduled automatic windows"
        )
        for marker in ("window:booked", "booked-maintenance-window", "maintenance-window"):
            condition = "contains(github.event.pull_request.labels.*.name, '" + marker + "')"
            cases = (
                (workflow(build=f"    if: {condition}\n"), 6),
                (workflow(steps=f"      - if: {condition}\n        run: deploy"), 7),
                (workflow(steps=f"      - run: |\n          test label = {marker}\n          deploy"), 7),
            )
            for text, line in cases:
                with self.subTest(marker=marker, text=text):
                    self.assertEqual(self.scan(text), [contract.Finding("ci.yml", line, problem, blocking=True)])

    def test_optional_jobs_cannot_require_manual_gates(self):
        extra = (
            "  deploy:\n    runs-on: ubuntu-latest\n    timeout-minutes: 5\n"
            "    if: contains(github.event.pull_request.labels.*.name, 'window:booked')\n"
            "    steps:\n      - uses: trstringer/manual-approval@v1\n"
        )
        found = self.scan(workflow(extra=extra))
        self.assertEqual(len(found), 2)
        self.assertTrue(all(f.problem.startswith("job deploy:") for f in found))

    def test_reusable_workflow_calls_cannot_require_booking(self):
        found = self.scan(
            "jobs:\n  deploy:\n    uses: org/repo/.github/workflows/deploy.yml@main\n"
            "    if: contains(github.event.pull_request.labels.*.name, 'window:booked')\n"
        )
        self.assertEqual(len(found), 1)
        self.assertIn("manual maintenance-window booking", found[0].problem)

    def test_automatic_rollouts_and_unrelated_text_pass(self):
        self.assertEqual(self.scan(workflow(
            build="    name: Replace window:booked and manual-approval\n",
            steps=("      # Retire the window:booked gate and trstringer/manual-approval.\n"
                   "      - uses: org/automatic-deploy@v1\n"
                   "      - if: github.ref == 'refs/heads/main'\n"
                   "        run: |\n"
                   "          deploy --rolling\n"
                   "          schedule --from-workload --batch --announce --execute"),
        )), [])

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

    def test_manual_gates_always_fail_and_emit_errors(self):
        for steps in ("      - uses: trstringer/manual-approval@v1",
                      "      - run: check window:booked"):
            # The same job also has a legacy finding: report-only keeps it a warning.
            findings = self.scan(workflow(build="    continue-on-error: true\n", steps=steps))
            for mode in ("report-only", "enforce"):
                with self.subTest(steps=steps, mode=mode), \
                        patch.dict(contract.os.environ, {"CONTRACT_MODE": mode}, clear=True), \
                        patch.object(contract, "delivered", return_value=False), \
                        patch.object(contract, "scan_dir", return_value=findings), \
                        patch("builtins.print") as output:
                    self.assertEqual(contract.main(), 1)
                    annotations = [c.args[0] for c in output.call_args_list[:-1]]
                    self.assertEqual(len(annotations), 2)
                    self.assertTrue(any(a.startswith("::error ") and ": manual " in a for a in annotations))
                    legacy = next(a for a in annotations if "continue-on-error" in a)
                    self.assertTrue(legacy.startswith("::warning " if mode == "report-only" else "::error "))


if __name__ == "__main__":
    unittest.main()
