"""Literal allowlists cannot infer unsafe event-payload absence."""
import importlib.util
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.github/actions/workflow-lint'))
SPEC = importlib.util.spec_from_file_location('workflow_contract', ROOT / '.github/actions/workflow-lint/contract.py')
contract = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(contract)
from pr_concurrency import problems
from merge_lane import expression

# Independent expected declarations: do not import the allowlist's constants.
GUARD = "github.event_name == 'pull_request' || github.event_name == 'pull_request_target'"
GROUP = "${{ (" + GUARD + ") && format('{0}-pr-{1}', github.workflow, github.event.pull_request.number) || format('{0}-run-{1}', github.workflow, github.run_id) }}"
CANCEL = '${{ ' + GUARD + ' }}'
HEADER = '# pr-concurrency: exception complete-evidence — Every qualification must finish.\n'
RUN = '${{ github.workflow }}-run-${{ github.run_id }}'


def workflow(group=GROUP, cancel=CANCEL, trigger='[pull_request, pull_request_target, merge_group, push, schedule, workflow_dispatch, release]', header=''):
    block = '' if group is None else f'concurrency:\n  group: {group}\n  cancel-in-progress: {cancel}\n'
    return header + f'on: {trigger}\n' + block + 'jobs:\n  ci-ok:\n    runs-on: ubuntu-latest\n    timeout-minutes: 1\n    steps:\n      - run: echo ok\n'


def resolve(value, context):
    """Independent engine for finite executed examples, not admission logic."""
    def replace(match):
        found = expression('${{' + match[1] + '}}', {k: {v} for k, v in context.items()})
        assert len(found) == 1, found
        return str(next(iter(found)))
    return re.sub(r'\$\{\{(.*?)\}\}', replace, value)


class PRConcurrencyTest(unittest.TestCase):
    def test_canonical_all_events_and_layout(self):
        self.assertEqual(problems(workflow()), [])
        self.assertEqual(problems(workflow(GROUP.replace(' && ', '\n    && ').replace(', ', ','))), [])
        # Whitespace in string literals is semantic, not layout.
        self.assertTrue(problems(workflow(GROUP.replace('-run-', '- run-'))))

    def test_existing_single_pr_canonical_variant(self):
        single = GROUP.replace('(' + GUARD + ')', "github.event_name == 'pull_request'")
        cancel = "${{ github.event_name == 'pull_request' }}"
        self.assertEqual(problems(workflow(single, cancel, '[pull_request, push, merge_group]')), [])
        for trigger in ('[pull_request, pull_request_target]', '[pull_request, workflow_call]'):
            self.assertTrue(problems(workflow(single, cancel, trigger)))

    def test_equivalent_but_noncanonical_expression_is_refused(self):
        equivalent = "${{ github.workflow }}-${{ (" + GUARD + ") && format('pr-{0}', github.event.pull_request.number) || format('run-{0}', github.run_id) }}"
        self.assertIn('non-canonical concurrency expression', problems(workflow(equivalent))[0][1])

    def test_mapping_and_scalar_event_syntax(self):
        for trigger in ('pull_request', '\n  pull_request:\n    types: [opened, synchronize]\n  push:\n    branches: [main]'):
            self.assertEqual(problems(workflow(trigger=trigger)), [])

    def test_missing_workflow_concurrency_and_job_only_concurrency(self):
        self.assertTrue(problems(workflow(None)))
        self.assertTrue(problems(workflow(None).replace('    runs-on:', '    concurrency: test\n    runs-on:')))

    def test_ref_fallback_evicts_pending_even_with_no_active_cancel(self):
        group = '${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}'
        for header in ('', HEADER):
            self.assertTrue(problems(workflow(group, 'false', header=header)))

    def test_non_pr_cancel_or_target_noncancel_is_refused(self):
        for cancel in ('true', 'false', "${{ github.event_name == 'pull_request' }}"):
            self.assertTrue(problems(workflow(cancel=cancel)))

    def test_identity_mutations_and_unknown_constructs_are_refused(self):
        for group in (GROUP + '-${{ github.sha }}', GROUP + '-${{ github.run_id }}',
                      GROUP.replace('github.workflow', "'same'"), GROUP.replace('-run-', '-pr-'),
                      GROUP.replace('github.run_id', 'inputs.group'),
                      GROUP.replace("github.event_name == 'pull_request'", 'github.event.pull_request.number == 1'),
                      GROUP.replace("format('{0}-pr-{1}'", "unknown('{0}-pr-{1}'")):
            self.assertTrue(problems(workflow(group)))

    def test_named_complete_evidence_literal_only(self):
        self.assertEqual(problems(workflow(RUN, 'false', header=HEADER)), [])
        for group, cancel in ((None, 'false'), (RUN, 'true'), (RUN.replace('github.run_id', 'github.ref'), 'false')):
            self.assertTrue(problems(workflow(group, cancel, header=HEADER)))
        for header in ('# pr-concurrency: exempt — arbitrary escape hatch\n',
                       '# pr-concurrency: exception complete-evidence — \n',
                       HEADER.replace('complete-evidence', 'unknown')):
            self.assertTrue(problems(workflow(RUN, 'false', header=header)))
        self.assertTrue(problems(workflow(None) + '      ' + HEADER))

    def test_pr_exception_cannot_hide_main_push_or_dispatch_eviction(self):
        for event in ('push', 'workflow_dispatch', 'merge_group'):
            text = workflow(RUN, 'false', '[pull_request, ' + event + ']', HEADER)
            self.assertEqual(problems(text), [])
            self.assertTrue(problems(text.replace('github.run_id', 'github.ref')))

    def test_edited_comment_payload_counterexample(self):
        group = "${{ github.event.changes.body && format('{0}-shared', github.workflow) || format('{0}-run-{1}', github.workflow, github.run_id) }}"
        for event in ('issue_comment', 'discussion', 'pull_request_review', 'workflow_call'):
            for header in (HEADER, '# pr-concurrency: exempt — Each PR qualification must finish.\n'):
                self.assertTrue(problems(workflow(group, 'false', '[pull_request, ' + event + ']', header)))
        groups = [resolve(group, {'github.event.changes.body': 'edited', 'github.workflow': 'CI',
                                  'github.run_id': run}) for run in (101, 102, 103)]
        self.assertEqual(groups, ['CI-shared'] * 3)

    def test_inherited_dispatch_input_counterexample(self):
        group = "${{ github.event.inputs.flag && format('{0}-shared', github.workflow) || format('{0}-run-{1}', github.workflow, github.run_id) }}"
        for header in (HEADER, '# pr-concurrency: exempt — Each PR qualification must finish.\n'):
            self.assertTrue(problems(workflow(group, 'false', '[pull_request, workflow_call]', header)))
        groups = [resolve(group, {'github.event.inputs.flag': 'yes', 'github.workflow': 'CI',
                                  'github.run_id': run}) for run in (101, 102, 103)]
        self.assertEqual(groups, ['CI-shared'] * 3)

    def test_reusable_only_and_workflow_run_review_outside_rule(self):
        for trigger in ('workflow_call', '[workflow_call, push]', '[workflow_run, workflow_dispatch]'):
            self.assertEqual(problems(workflow(None, trigger=trigger)), [])

    def test_mixed_reusable_requires_canonical_two_pr_form(self):
        trigger = '[pull_request, workflow_call]'
        self.assertEqual(problems(workflow(trigger=trigger)), [])
        for cancel in ('true', "${{ github.event_name != 'merge_group' }}",
                       CANCEL[:-3] + " || github.event_name == 'schedule' }}"):
            self.assertTrue(problems(workflow(cancel=cancel, trigger=trigger)))

    def test_real_named_retarget_alternatives_and_mutations(self):
        for name in ('project-control.yml', 'pr-metadata.yml'):
            text = (ROOT / '.github/workflows' / name).read_text()
            self.assertEqual(problems(text), [])
            self.assertTrue(problems(text.replace('github.run_id', 'github.ref')))
            self.assertTrue(problems(text.replace('exception ', 'exempt ')))
            self.assertTrue(problems(text.replace('  merge_group:', '  workflow_call:')))
            self.assertTrue(problems(text.replace('name: ', 'name: Different ', 1)))
            if name == 'project-control.yml':
                self.assertTrue(problems(text.replace('type: string', 'type: boolean')))

    def test_real_retarget_non_pr_runs_have_distinct_fallbacks(self):
        for name, title in (('project-control.yml', 'Project control'), ('pr-metadata.yml', 'PR metadata')):
            group = yaml.load((ROOT / '.github/workflows' / name).read_text(), yaml.BaseLoader)['concurrency']['group']
            for event in ('push', 'merge_group', 'workflow_dispatch'):
                groups = [resolve(group, {'github.event_name': event, 'github.workflow': title,
                    'github.event.inputs.code-pr': None, 'github.event.inputs.junit-producer-run': None,
                    'github.event.changes.base': None, 'github.event.pull_request.number': None,
                    'github.run_id': run}) for run in (101, 102, 103)]
                self.assertEqual(len(set(groups)), 3, (name, event))

    def test_real_contract_scan_is_blocking_and_reads_yaml_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / '.github/workflows'
            folder.mkdir(parents=True)
            path = folder / 'ci.yaml'
            path.write_text(workflow(None, trigger='pull_request'))
            with patch.dict(os.environ, {}, clear=True):
                rule = [f for f in contract.scan_dir(root) if 'non-canonical concurrency expression' in f.problem]
                self.assertEqual(len(rule), 1)
                self.assertTrue(rule[0].blocking)
                self.assertEqual(contract.exit_code(rule, 'report-only'), 1)
                path.write_text(workflow(trigger='pull_request'))
                self.assertEqual([f for f in contract.scan_dir(root) if 'non-canonical concurrency expression' in f.problem], [])


if __name__ == '__main__':
    unittest.main()
