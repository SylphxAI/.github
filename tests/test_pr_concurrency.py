"""PR runs coalesce; non-PR runs cannot cancel or evict each other."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest

import yaml
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.github/actions/workflow-lint'))
SPEC = importlib.util.spec_from_file_location('workflow_contract', ROOT / '.github/actions/workflow-lint/contract.py')
contract = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(contract)
from pr_concurrency import evaluate, problems

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
            header = '# pr-concurrency: exempt — ' + reason + '\n'
            self.assertEqual(problems(workflow('${{ github.workflow }}-run-${{ github.run_id }}', 'false', header=header)), [])
            self.assertTrue(problems(workflow(None, header=header)))
        for marker in ('# pr-concurrency: exempt\n', '# pr-concurrency: exempt — \n'):
            self.assertIn('specific reason', problems(workflow(None, header=marker))[0][1])
        self.assertTrue(problems(workflow(None) + '      # pr-concurrency: exempt — not a header\n'))
        self.assertTrue(problems(workflow(None, header='# optimistic-merge: advisory — not this rule\n')))

    def test_pr_waiver_cannot_hide_main_push_or_repeated_dispatch_eviction(self):
        header = '# pr-concurrency: exempt — Every PR qualification must finish.\n'
        for event in ('push', 'workflow_dispatch', 'merge_group'):
            trigger = '[pull_request, ' + event + ']'
            safe = workflow('${{ github.workflow }}-run-${{ github.run_id }}', 'false', trigger, header)
            self.assertEqual(problems(safe), [])
            self.assertTrue(problems(safe.replace('github.run_id', 'github.ref')))
            self.assertTrue(problems(safe.replace('cancel-in-progress: false', 'cancel-in-progress: true')))
        # A reusable call can inherit arbitrary non-PR events under a waiver too.
        self.assertTrue(problems(workflow('${{ github.workflow }}-fixed', 'false',
                                         '[pull_request, workflow_call]', header)))

    def test_payload_changes_cannot_hide_edited_event_eviction(self):
        header = '# pr-concurrency: exempt — Each PR qualification must finish.\n'
        group = "${{ github.event.changes.body && format('{0}-shared', github.workflow) || format('{0}-run-{1}', github.workflow, github.run_id) }}"
        for event in ('issue_comment', 'discussion', 'pull_request_review', 'workflow_call'):
            text = workflow(group, 'false', '[pull_request, ' + event + ']', header)
            self.assertTrue(problems(text), event)
            # The same payload predicate is safe when both branches isolate runs.
            safe = text.replace("format('{0}-shared', github.workflow)",
                                "format('{0}-edited-run-{1}', github.workflow, github.run_id)")
            self.assertEqual(problems(safe), [], event)
        # Reproduce the scheduler collision independently of the linter verdict.
        groups = [evaluate(group, 'issue_comment', {
            'github.workflow': {'CI'}, 'github.run_id': {run},
            'github.event.changes.body': {'edited'}}) for run in ('101', '102', '103')]
        self.assertEqual(groups, ['CI-shared'] * 3)
        # Unmodeled payload selectors cannot inherit an absent-value assumption.
        self.assertTrue(problems(workflow(group.replace('changes.body', 'comment.body'),
                                         'false', '[pull_request, issue_comment]', header)))

    def test_retarget_optouts_preserve_only_the_declared_pr_association(self):
        for name in ('project-control.yml', 'pr-metadata.yml'):
            text = (ROOT / '.github/workflows' / name).read_text()
            self.assertEqual(problems(text), [])
            # The old stable fallback under its reasoned waiver is refused.
            broken = text.replace("format('run-{0}', github.run_id)",
                                  "format('{0}-{1}', github.event_name, github.ref)")
            broken = broken.replace("format('{0}-run-{1}', github.workflow, github.run_id)",
                                    "format('{0}-{1}', github.event_name, github.ref)")
            self.assertTrue(problems(broken), name)
            if name == 'project-control.yml':
                self.assertTrue(problems(text.replace("format('consumer-run-{0}', github.run_id)",
                                                      "format('consumer-{0}', github.ref)")))
                # The special dispatch association cannot leak into generic
                # dispatch: without the named input, no PR-group waiver applies.
                broken = text.replace("format('run-{0}', github.run_id)",
                                      "format('pr-{0}', github.event.inputs.code-pr)")
                self.assertTrue(problems(broken))
                self.assertTrue(problems(text.replace('associated-pr-input code-pr',
                                                      'associated-pr-input undeclared')))
                self.assertTrue(problems(text.replace('# pr-concurrency: associated-pr-input',
                                                      '# unrelated: associated-pr-input')))

    def test_real_retarget_groups_and_all_non_pr_fallback_branches(self):
        for name, title in (('project-control.yml', 'Project control'), ('pr-metadata.yml', 'PR metadata')):
            text = (ROOT / '.github/workflows' / name).read_text()
            concurrency = yaml.load(text, yaml.BaseLoader)['concurrency']
            context = {'github.workflow': {title}, 'github.event.inputs.code-pr': {None},
                       'github.event.inputs.junit-producer-run': {None},
                       'github.event.changes.base': {None}}
            for event in ('push', 'merge_group', 'workflow_dispatch'):
                groups = []
                for run in ('101', '102', '103'):
                    overrides = {**context, 'github.run_id': {run}}
                    groups.append(evaluate(concurrency['group'], event, overrides))
                    self.assertIs(evaluate(concurrency['cancel-in-progress'], event, overrides), False)
                self.assertEqual(len(set(groups)), 3, (name, event))
            pr_context = {**context, 'github.event.pull_request.number': {'7'}, 'github.run_id': {'101'}}
            native = evaluate(concurrency['group'], 'pull_request', pr_context)
            if name == 'project-control.yml':
                self.assertEqual(native, 'Project control-pr-7')
                dispatch = {**context, 'github.event.inputs.code-pr': {'7'}, 'github.run_id': {'102'}}
                self.assertEqual(evaluate(concurrency['group'], 'workflow_dispatch', dispatch), native)
                consumer = {**dispatch, 'github.event.inputs.junit-producer-run': {'producer'}}
                self.assertEqual(evaluate(concurrency['group'], 'workflow_dispatch', consumer),
                                 'Project control-consumer-run-102')
            else:
                self.assertEqual(native, 'pr-metadata-pr-7')
                base_edit = {**pr_context, 'github.event.changes.base': {'changed'}}
                self.assertEqual(evaluate(concurrency['group'], 'pull_request', base_edit),
                                 'pr-metadata-base-101')

    def test_reusable_only_and_workflow_run_review_are_outside_rule(self):
        for trigger in ('workflow_call', '[workflow_call, push]', '[workflow_run, workflow_dispatch]'):
            self.assertEqual(problems(workflow(None, trigger=trigger)), [])
        self.assertTrue(problems(workflow(None, trigger='[pull_request, workflow_call]')))

    def test_mixed_reusable_inherits_all_caller_event_partitions(self):
        trigger = '[pull_request, workflow_call]'
        self.assertEqual(problems(workflow(trigger=trigger)), [])
        self.assertTrue(problems(workflow(cancel='true', trigger=trigger)))
        self.assertTrue(problems(workflow(cancel=CANCEL[:-3] + " || github.event_name == 'schedule' }}", trigger=trigger)))
        self.assertTrue(problems(workflow(cancel="${{ github.event_name != 'merge_group' }}", trigger=trigger)))
        self.assertTrue(problems(workflow(cancel="${{ 'release' == github.event_name || " + GUARD + ' }}', trigger=trigger)))

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
