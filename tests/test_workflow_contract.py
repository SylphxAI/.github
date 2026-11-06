"""Required workflow paths must propagate failures and jobs must be bounded."""

import importlib.util
import pathlib
import sys
import tempfile
import os
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

    def test_r8_blocks_even_report_only_but_unserved_plain_is_a_notice(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            folder = root / '.github/workflows'
            folder.mkdir(parents=True)
            path = folder / 'merge.yml'
            path.write_text('# optimistic-merge: advisory — Runner-policy fixture.\non: merge_group\njobs:\n  build:\n    timeout-minutes: 5\n    runs-on: sylphx-linux-standard\n')
            with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'Cubeage/example'}, clear=True):
                found = contract.scan_dir(root)
                self.assertEqual(len(found), 1)
                self.assertTrue(found[0].blocking)
                self.assertEqual(contract.exit_code(found, 'report-only'), 1)
                path.write_text(path.read_text().replace('standard', 'xlarge'))
                self.assertEqual(contract.scan_dir(root), [])

    def test_hands_off_skips_before_reading_workflows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            folder = root / '.github/workflows'
            folder.mkdir(parents=True)
            (folder / 'invalid.yml').write_text('invalid: [')
            for repo in ('SylphxAI/bgca', 'SylphxAI/openclaw-sylphx'):
                with patch.dict(os.environ, {'GITHUB_REPOSITORY': repo}, clear=True):
                    self.assertEqual(contract.scan_dir(root), [])

    def test_valid(self):
        self.assertEqual(self.scan(workflow()), [])

    def test_unknown_runner_blocks_even_report_only(self):
        found = self.scan(workflow().replace('runs-on: ubuntu-latest', 'runs-on: ${{ inputs.runner }}', 1))
        self.assertEqual(len(found), 1)
        self.assertIn('pull_request runs-on cannot be evaluated', found[0].problem)
        self.assertIn('inputs.runner', found[0].problem)
        self.assertEqual(contract.exit_code(found, 'report-only'), 1)

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

    def test_polling_aggregate_keeps_all_jobs_required_beside_local_verdict(self):
        text = workflow(extra=(
            "  report:\n    runs-on: ubuntu-latest\n    timeout-minutes: 1\n"
            "    steps:\n      - run: report || true\n"
            "  cross-workflow:\n    runs-on: ubuntu-latest\n    timeout-minutes: 1\n"
            "    steps:\n      - uses: SylphxAI/.github/.github/actions/ci-ok-polling@main\n"
        ))
        found = self.scan(text)
        self.assertEqual(len(found), 1)
        self.assertIn("job report: || true hides a required failure", found[0].problem)

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


class GateCoverageTest(unittest.TestCase):
    def scan(self, text, **others):
        return contract.gate_coverage({"ci.yml": text, **others})

    def test_new_job_outside_gate_fails_for_both_events(self):
        for event in ("pull_request", "merge_group"):
            text = workflow(extra="  extra: {runs-on: ubuntu-latest, steps: [{run: pytest}]}\n").replace("on: pull_request", "on: " + event)
            found = self.scan(text)
            self.assertEqual(len(found), 1)
            self.assertIn(f"job extra: {event} job is outside ci-ok", found[0].problem)
            self.assertEqual(contract.exit_code(found, "report-only"), 1)
            self.assertEqual(self.scan(text.replace("needs: [build]", "needs: [build, extra]")), [])

    def test_transitive_scalar_and_sequence_needs(self):
        text = workflow(build="    needs: setup\n", extra="  setup: {runs-on: ubuntu-latest, steps: [{run: setup}]}\n")
        self.assertEqual(self.scan(text), [])

    def test_reasoned_advisory_and_missing_reason(self):
        for event in ("pull_request", "merge_group"):
            independent = f"on: {event}\njobs: {{report: {{runs-on: ubuntu-latest}}}}\n"
            self.assertEqual(self.scan(workflow(), report=independent).__len__(), 1)
            self.assertEqual(self.scan(workflow(), report="# optimistic-merge: advisory — Nonblocking diagnostic report.\n" + independent), [])
            found = self.scan(workflow(), report="# optimistic-merge: advisory\n" + independent)
            self.assertTrue(any("must include a reason" in f.problem for f in found))
            self.assertTrue(any("job report:" in f.problem for f in found))

    def test_header_declaration_cannot_be_spoofed_by_run_body(self):
        independent = "on: pull_request\njobs:\n  extra:\n    runs-on: ubuntu-latest\n    steps:\n      - run: |\n          # optimistic-merge: advisory — Not a declaration.\n          pytest\n"
        self.assertEqual(len(self.scan(workflow(), extra=independent)), 1)

    def test_no_gate_or_no_dependencies_does_not_mean_everything_is_covered(self):
        self.assertEqual(len(self.scan("on: pull_request\njobs: {test: {runs-on: ubuntu-latest}}")), 1)
        self.assertEqual(len(self.scan(workflow().replace("    needs: [build]\n", ""))), 1)

    def test_postmerge_only_job_and_workflow_are_out_of_scope(self):
        text = workflow(extra="  release: {if: github.event_name == 'push', runs-on: ubuntu-latest}\n")
        self.assertEqual(self.scan(text, release="on: push\njobs: {publish: {runs-on: ubuntu-latest}}"), [])

    def test_local_and_remote_shared_ci_ok_action_aliases(self):
        for action in ("./.github/actions/ci-ok", "SylphxAI/.github/.github/actions/ci-ok@" + "a" * 40):
            text = workflow().replace("  ci-ok:", "  verdict:").replace("      - run: echo ok", "      - uses: " + action)
            self.assertEqual(self.scan(text), [])

    def test_polling_observes_cross_workflow_jobs_but_not_ignored_checks(self):
        for event in ("pull_request", "merge_group"):
            gate = f"on: {event}\njobs:\n  ci-ok:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: SylphxAI/.github/.github/actions/ci-ok-polling@{'a' * 40}\n"
            check = f"on: {event}\njobs: {{test: {{name: Tests, runs-on: ubuntu-latest}}}}"
            self.assertEqual(self.scan(gate, test=check), [])
            found = self.scan(gate + "        with: {ignore: Tests}\n", test=check)
            self.assertEqual(len(found), 1)
            self.assertIn("job test:", found[0].problem)
            self.assertEqual(self.scan(gate + "        with: {ignore: Tests}\n", test="# optimistic-merge: advisory — Optional report.\n" + check), [])

    def test_independent_trigger_is_not_awaited_by_a_reusable_call(self):
        gate = workflow().replace("    steps:\n      - run: pytest", "    uses: ./.github/workflows/test.yml")
        self.assertEqual(self.scan(gate, test="on: workflow_call\njobs: {test: {runs-on: ubuntu-latest}}"), [])
        self.assertEqual(len(self.scan(gate, test="on: [pull_request, workflow_call]\njobs: {test: {runs-on: ubuntu-latest}}")), 1)

    def test_repository_scan_wires_the_rule(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            folder = root / ".github/workflows"
            folder.mkdir(parents=True)
            (folder / "ci.yml").write_text(workflow())
            (folder / "test.yml").write_text("on: [pull_request, merge_group]\njobs: {extra: {runs-on: ubuntu-latest, timeout-minutes: 1}}")
            found = contract.scan_dir(root)
            self.assertEqual(len(found), 2)
            self.assertTrue(all("job extra:" in f.problem and f.blocking for f in found))


if __name__ == "__main__":
    unittest.main()
