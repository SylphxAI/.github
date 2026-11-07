"""PR runs coalesce; non-PR runs cannot cancel or evict each other."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.github/actions/workflow-lint'))
SPEC = importlib.util.spec_from_file_location('workflow_contract', ROOT / '.github/actions/workflow-lint/contract.py')
contract = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(contract)
from pr_concurrency import problems

GUARD = "github.event_name == 'pull_request' || github.event_name == 'pull_request_target'"
GROUP = "${{ (" + GUARD + ") && format('{0}-pr-{1}', github.workflow, github.event.pull_request.number) || format('{0}-run-{1}', github.workflow, github.run_id) }}"
CANCEL = '${{ ' + GUARD + ' }}'


def workflow(group=GROUP, cancel=CANCEL, trigger='[pull_request, pull_request_target, merge_group, push, schedule, workflow_dispatch, release]', header=''):
    block = '' if group is None else f'concurrency:\n  group: {group}\n  cancel-in-progress: {cancel}\n'
    return header + f'on: {trigger}\n' + block + 'jobs:\n  ci-ok:\n    runs-on: ubuntu-latest\n    timeout-minutes: 1\n    steps:\n      - run: echo ok\n'


class PRConcurrencyTest(unittest.TestCase):
    def test_canonical_all_events(self):
        self.assertEqual(problems(workflow()), [])

    def test_safe_existing_pr_only_guard(self):
        text = workflow(trigger='[pull_request, push, merge_group]').replace('(' + GUARD + ')', "github.event_name == 'pull_request'").replace(CANCEL, "${{ github.event_name == 'pull_request' }}")
        self.assertEqual(problems(text), [])

    def test_equivalent_interpolation(self):
        group = "${{ github.workflow }}-${{ (" + GUARD + ") && format('pr-{0}', github.event.pull_request.number) || format('run-{0}', github.run_id) }}"
        self.assertEqual(problems(workflow(group)), [])

    def test_mapping_and_scalar_event_syntax(self):
        for trigger in ('pull_request', '\n  pull_request:\n    types: [opened, synchronize]\n  push:\n    branches: [main]'):
            self.assertEqual(problems(workflow(trigger=trigger)), [])

    def test_missing_workflow_concurrency_and_job_only_concurrency(self):
        text = workflow(None)
        self.assertTrue(problems(text))
        self.assertTrue(problems(text.replace('    runs-on:', '    concurrency: test\n    runs-on:')))

    def test_ref_fallback_evicts_pending_even_with_no_active_cancel(self):
        group = '${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}'
        self.assertTrue(problems(workflow(group, 'false')))
        self.assertTrue(problems(workflow(group)))

    def test_cancel_true_can_cancel_non_pr(self):
        self.assertTrue(problems(workflow(cancel='true')))

    def test_target_must_cancel_too(self):
        self.assertTrue(problems(workflow(cancel="${{ github.event_name == 'pull_request' }}")))

    def test_sha_run_id_or_ref_cannot_split_same_pr_group(self):
        for suffix in ('-${{ github.sha }}', '-${{ github.run_id }}', '-${{ github.ref }}'):
            self.assertTrue(problems(workflow(GROUP + suffix)))

    def test_cross_workflow_or_pr_run_identity_collision(self):
        for group in (GROUP.replace('github.workflow', "'same'"),
                      GROUP.replace('-run-', '-pr-'), GROUP.replace('github.run_id', 'github.event.pull_request.number')):
            self.assertTrue(problems(workflow(group)))

    def test_unknowns_and_id_predicates_fail_closed(self):
        for group in (GROUP.replace('github.run_id', 'inputs.group'),
                      GROUP.replace("github.event_name == 'pull_request'", 'github.event.pull_request.number == 1'),
                      GROUP.replace("format('{0}-pr-{1}'", "unknown('{0}-pr-{1}'")):
            self.assertTrue(problems(workflow(group)))

    def test_only_reasoned_header_exemption_counts(self):
        for reason in ('Preserve serialized publication of immutable artifacts.',
                       'Complete qualification evidence must survive later commits.'):
            self.assertEqual(problems(workflow(None, header='# pr-concurrency: exempt — ' + reason + '\n')), [])
        for marker in ('# pr-concurrency: exempt\n', '# pr-concurrency: exempt — \n'):
            self.assertIn('specific reason', problems(workflow(None, header=marker))[0][1])
        self.assertTrue(problems(workflow(None) + '      # pr-concurrency: exempt — not a header\n'))
        self.assertTrue(problems(workflow(None, header='# optimistic-merge: advisory — not this rule\n')))

    def test_reusable_only_and_workflow_run_review_are_outside_rule(self):
        for trigger in ('workflow_call', '[workflow_call, push]', '[workflow_run, workflow_dispatch]'):
            self.assertEqual(problems(workflow(None, trigger=trigger)), [])
        self.assertTrue(problems(workflow(None, trigger='[pull_request, workflow_call]')))

    def test_real_contract_scan_blocks_report_only_and_reads_yaml_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / '.github/workflows'
            folder.mkdir(parents=True)
            path = folder / 'ci.yaml'
            path.write_text(workflow(None, trigger='pull_request'))
            with patch.dict(os.environ, {}, clear=True):
                found = contract.scan_dir(root)
                rule = [f for f in found if 'PR concurrency' in f.problem]
                self.assertEqual(len(rule), 1)
                self.assertTrue(rule[0].blocking)
                self.assertEqual(contract.exit_code(rule, 'report-only'), 1)
                path.write_text(workflow(trigger='pull_request'))
                self.assertEqual([f for f in contract.scan_dir(root) if 'PR concurrency' in f.problem], [])


if __name__ == '__main__':
    unittest.main()
