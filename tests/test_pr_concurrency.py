"""Literal allowlists cannot infer unsafe event-payload absence."""
import importlib.util
import os
from pathlib import Path
import re
import sys
import subprocess
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
CALLEE = '''on: workflow_call
jobs:
  check:
    runs-on: sylphx-linux-control
    timeout-minutes: 1
    steps:
      - uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5
      - run: echo check
'''
CALLEES = {'reusable.yml': CALLEE}


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


def independent_verdict_workflow():
    header = '# pr-concurrency: exception independent-verdict — Admit cleanup without the surviving verdict lock.\n'
    prefix = workflow(RUN, 'false', header=header).split('jobs:\n', 1)[0]
    return prefix + f'''jobs:
  select:
    concurrency:
      group: select-{GROUP}
      cancel-in-progress: {CANCEL}
    runs-on: sylphx-linux-control
    timeout-minutes: 1
    steps:
      - run: echo select-and-cleanup
  tests:
    needs: select
    concurrency:
      group: tests-{GROUP}
      cancel-in-progress: {CANCEL}
    uses: ./.github/workflows/reusable.yml
  ci-ok:
    needs: [select, tests]
    if: ${{{{ always() }}}}
    runs-on: sylphx-linux-control
    timeout-minutes: 1
    steps:
      - run: exit 1
'''


def independent_problems(text):
    return problems(text, workflows=CALLEES)


class PRConcurrencyTest(unittest.TestCase):
    def test_canonical_all_events_and_layout(self):
        self.assertEqual(problems(workflow()), [])
        self.assertTrue(problems(workflow(GROUP.replace(' && ', '\n    && ').replace(', ', ','))))
        # Whitespace in string literals is semantic, not layout.
        self.assertTrue(problems(workflow(GROUP.replace('-run-', '- run-'))))

    def test_literal_delimiters_are_not_normalized_into_expressions(self):
        single = GROUP.replace('(' + GUARD + ')', "github.event_name == 'pull_request'")
        for group, cancel, trigger in ((GROUP, CANCEL, '[pull_request, pull_request_target]'),
                (single, "${{ github.event_name == 'pull_request' }}", 'pull_request')):
            for delimiter in ('$ {{', '${ {'):
                self.assertTrue(problems(workflow(group.replace('${{', delimiter), cancel, trigger)))
                self.assertTrue(problems(workflow(group, cancel.replace('${{', delimiter), trigger)))

    def test_named_reusable_callee_is_disjoint_under_inherited_context(self):
        header = '# pr-concurrency: exception reusable-regenerate — Callee must not share caller group.\nname: Regenerate\n'
        text = workflow('regenerate-' + GROUP, CANCEL, '[pull_request, workflow_call, push]', header)
        self.assertEqual(problems(text), [])
        for broken in (text.replace('regenerate-${{', '${{'),
                       text.replace('name: Regenerate', 'name: Other'),
                       text.replace('workflow_call', 'workflow_dispatch')):
            self.assertTrue(problems(broken))
        for event in ('pull_request', 'pull_request_target', 'push', 'merge_group',
                      'workflow_dispatch', 'schedule', 'release'):
            context = {'github.event_name': event, 'github.workflow': 'CI',
                       'github.event.pull_request.number': 7, 'github.run_id': 101}
            caller = resolve(GROUP, context)
            callee = resolve('regenerate-' + GROUP, context)
            self.assertNotEqual(caller, callee, event)
            self.assertEqual(callee, 'regenerate-' + caller, event)

    def test_five_literal_delimiter_fixtures_through_real_entrypoint(self):
        fixtures = sorted((ROOT / 'tests/fixtures/pr-concurrency').glob('*literal-delimiter-*.yml'))
        self.assertEqual(len(fixtures), 5)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / '.github/workflows'
            folder.mkdir(parents=True)
            event = root / 'event.json'
            event.write_text('{"repository":{"private":false,"visibility":"public"}}')
            env = {k: v for k, v in os.environ.items() if not k.startswith('GITHUB_')}
            env.update(CONTRACT_MODE='report-only', GITHUB_EVENT_PATH=str(event))
            for fixture in fixtures:
                with self.subTest(fixture=fixture.name):
                    text = fixture.read_text()
                    self.assertTrue(problems(text))
                    path = folder / 'ci.yml'
                    path.write_text(text)
                    rejected = subprocess.run([sys.executable, str(ROOT / '.github/actions/workflow-lint/contract.py')],
                                              cwd=root, env=env, capture_output=True, text=True)
                    self.assertEqual(rejected.returncode, 1, rejected.stdout + rejected.stderr)
                    self.assertIn('non-canonical concurrency expression', rejected.stdout)
                    # A valid delimiter, with every other byte preserved, is
                    # the passing control through the very same entrypoint.
                    path.write_text(text.replace('$ {{', '${{'))
                    self.assertEqual(problems(path.read_text()), [])
                    accepted = subprocess.run([sys.executable, str(ROOT / '.github/actions/workflow-lint/contract.py')],
                                              cwd=root, env=env, capture_output=True, text=True)
                    self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
                    self.assertNotIn('non-canonical concurrency expression', accepted.stdout)

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

    def test_independent_verdict_requires_every_work_job_lock(self):
        text = independent_verdict_workflow()
        self.assertEqual(independent_problems(text), [])
        for name in ('select', 'tests'):
            lock = f'    concurrency:\n      group: {name}-{GROUP}\n      cancel-in-progress: {CANCEL}\n'
            for broken in (text.replace(lock, ''),
                           text.replace(name + '-' + GROUP, GROUP),
                           text.replace(name + '-' + GROUP, '${{ github.job }}-' + GROUP),
                           text.replace(name + '-' + GROUP, name + '-' + GROUP.replace('github.run_id', 'github.ref')),
                           text.replace(lock, lock.replace(CANCEL, 'true')),
                           text.replace(lock, lock.replace(CANCEL, 'false'))):
                with self.subTest(job=name, source=broken):
                    self.assertTrue(independent_problems(broken))
        self.assertTrue(independent_problems(text.replace('tests-' + GROUP, 'select-' + GROUP)))
        self.assertTrue(independent_problems(text.replace('tests', 'SELECT')))
        self.assertTrue(independent_problems(text.replace('group: ' + RUN, 'group: ' + GROUP)))
        self.assertTrue(independent_problems(text.replace('  cancel-in-progress: false', '  cancel-in-progress: true', 1)))
        self.assertTrue(independent_problems(text.replace('exception independent-verdict', 'exception unknown')))
        self.assertTrue(independent_problems(text.split('\n', 1)[1]))
        self.assertTrue(independent_problems(text.replace('pull_request_target,', 'pull_request_target, workflow_call,')))

    def test_independent_verdict_is_unlocked_and_depends_on_every_work_job(self):
        text = independent_verdict_workflow()
        for condition in ('always()', '${{ always() }}'):
            self.assertEqual(independent_problems(text.replace('${{ always() }}', condition)), [])
        for needs in ('[select]', '[tests]', '[select, tests, tests]',
                      '[select, tests, absent]', '[select, select]', '[[select], tests]', '{}'):
            with self.subTest(needs=needs):
                self.assertTrue(independent_problems(text.replace('[select, tests]', needs)))
        for broken in (text.replace('    if: ${{ always() }}', '    if: ${{ !cancelled() }}'),
                       text.replace('    if: ${{ always() }}', '    if: ${{ always() && success() }}'),
                       text.replace('    if: ${{ always() }}', f'    concurrency:\n      group: ci-ok-{GROUP}\n      cancel-in-progress: {CANCEL}\n    if: ${{{{ always() }}}}'),
                       text.replace('    if: ${{ always() }}', '    concurrency: null\n    if: ${{ always() }}'),
                       text.replace('    needs: select\n', '    needs: select\n    if: always()\n')):
            self.assertTrue(independent_problems(broken))
        one = text.split('  tests:\n')[0] + '  ci-ok:\n' + text.split('  ci-ok:\n')[1]
        one = one.replace('[select, tests]', 'select')
        self.assertEqual(independent_problems(one), [])
        self.assertTrue(independent_problems(workflow(RUN, 'false', header='# pr-concurrency: exception independent-verdict — No work jobs.\n')))

    def test_independent_job_locks_resolve_pr_and_non_pr_isolation(self):
        decoded = yaml.load(independent_verdict_workflow(), yaml.BaseLoader)
        for event in ('pull_request', 'pull_request_target', 'push', 'merge_group',
                      'workflow_dispatch', 'schedule', 'release'):
            seen = {}
            for run in (101, 102, 103):
                context = {'github.event_name': event, 'github.workflow': 'CI',
                           'github.event.pull_request.number': 7, 'github.run_id': run}
                self.assertEqual(resolve(RUN, context), f'CI-run-{run}')
                for name in ('select', 'tests'):
                    lock = decoded['jobs'][name]['concurrency']
                    seen.setdefault(name, []).append(resolve(lock['group'], context))
                    self.assertEqual(resolve(lock['cancel-in-progress'], context).lower(),
                                     str(event in ('pull_request', 'pull_request_target')).lower())
            self.assertTrue(set(seen['select']).isdisjoint(seen['tests']))
            for name in seen:
                self.assertEqual(len(set(seen[name])), 1 if event.startswith('pull_request') else 3)
            if event.startswith('pull_request'):
                context['github.event.pull_request.number'] = 8
                self.assertNotIn(resolve(decoded['jobs']['select']['concurrency']['group'], context), seen['select'])

    def test_independent_work_conditions_retain_implicit_success(self):
        text = independent_verdict_workflow()
        for name in ('select', 'tests'):
            anchor = f'  {name}:\n'
            permitted = text.replace(anchor, anchor + "    if: needs.scope.outputs.selected == 'true'\n")
            self.assertEqual(independent_problems(permitted), [])
            for condition in ('always() && true', '!cancelled() || always()',
                              '!success()', 'failure()', 'cancelled()',
                              'ALWAYS ()', 'SuCcEsS ( )', '!FAILURE ( )', '!CANCELLED ()'):
                with self.subTest(job=name, condition=condition):
                    broken = text.replace(anchor, anchor + f'    if: ${{{{ {condition} }}}}\n')
                    self.assertTrue(independent_problems(broken))

    def test_independent_caller_jobs_refuse_matrix_expansion(self):
        text = independent_verdict_workflow()
        self.assertEqual(independent_problems(text), [])
        for name in ('select', 'tests', 'ci-ok'):
            for strategy in ('{matrix: {lane: [one, two]}}',
                             '{matrix: "${{ fromJSON(needs.select.outputs.matrix) }}"}',
                             '{matrix: {include: [{lane: one}, {lane: two}]}}',
                             'null', '[]'):
                with self.subTest(job=name, strategy=strategy):
                    broken = text.replace(f'  {name}:\n', f'  {name}:\n    strategy: {strategy}\n')
                    self.assertTrue(independent_problems(broken))
        # Matrix children inside an unlocked callee have no shared job lock.
        callee = CALLEE.replace('  check:\n', '  check:\n    strategy:\n      matrix:\n        lane: [one, two]\n')
        self.assertEqual(problems(text, workflows={'reusable.yml': callee}), [])
        # The ordinary workflow-level lock owns the whole matrix, not siblings.
        canonical = workflow().replace('  ci-ok:\n', '  ci-ok:\n    strategy: {matrix: {lane: [one, two]}}\n')
        self.assertEqual(problems(canonical), [])

    def test_independent_matrix_siblings_resolve_the_same_lock(self):
        decoded = yaml.load(independent_verdict_workflow(), yaml.BaseLoader)
        for event in ('pull_request', 'pull_request_target', 'push', 'merge_group'):
            context = {'github.event_name': event, 'github.workflow': 'CI',
                       'github.event.pull_request.number': 7, 'github.run_id': 101}
            for name in ('select', 'tests'):
                lock = decoded['jobs'][name]['concurrency']['group']
                self.assertEqual(resolve(lock, {**context, 'matrix.lane': 'one'}),
                                 resolve(lock, {**context, 'matrix.lane': 'two'}))

    def test_independent_calls_require_resolved_local_callees(self):
        text = independent_verdict_workflow()
        self.assertEqual(problems(text, workflows=CALLEES), [])
        self.assertEqual(problems(text.replace('reusable.yml', 'reusable.yaml'),
                                  workflows={'reusable.yaml': CALLEE}), [])
        # No reusable call needs no dependency inventory.
        direct = text.replace('    uses: ./.github/workflows/reusable.yml',
                              '    runs-on: sylphx-linux-control\n    steps: [{run: echo test}]')
        self.assertEqual(problems(direct), [])
        for inventory in (None, {}, {'different.yml': CALLEE}, {'reusable.yml': None}):
            with self.subTest(inventory=inventory):
                self.assertTrue(problems(text, workflows=inventory))
        for target in ('owner/repo/.github/workflows/reusable.yml@' + 'a' * 40,
                       './.github/workflows/nested/reusable.yml',
                       './.github/workflows/../reusable.yml',
                       './.github/workflows/reusable.yml@main',
                       './.github/workflows/reusable',
                       './.github/workflows/reusable\\name.yml'):
            with self.subTest(target=target):
                self.assertTrue(problems(text.replace('./.github/workflows/reusable.yml', target),
                                         workflows=CALLEES))

    def test_independent_callees_refuse_every_lock_and_nested_call(self):
        text = independent_verdict_workflow()
        for callee in (CALLEE.replace('jobs:', 'concurrency: tests-' + GROUP + '\njobs:'),
                       CALLEE.replace('jobs:', 'concurrency: null\njobs:'),
                       CALLEE.replace('jobs:', 'concurrency: {}\njobs:'),
                       CALLEE.replace('    runs-on:', '    concurrency: different-but-not-proven-disjoint\n    runs-on:'),
                       CALLEE.replace('    runs-on:', '    concurrency: null\n    runs-on:'),
                       CALLEE + '  sibling:\n    concurrency: collision\n    steps: [{run: echo sibling}]\n',
                       'on: workflow_call\njobs:\n  nested:\n    uses: ./.github/workflows/next.yml\n',
                       CALLEE.replace('workflow_call', 'workflow_dispatch'),
                       'on: workflow_call\njobs: {}\n',
                       'on: workflow_call\njobs: [check]\n',
                       'on: workflow_call\njobs: {check: null}\n',
                       'on: workflow_call\n', 'on: [workflow_call, {}]\njobs: {check: {}}',
                       '', '[]', 'jobs: ['):
            with self.subTest(callee=callee):
                self.assertTrue(problems(text, workflows={'reusable.yml': callee}))
        # A reusable always() verdict cannot smuggle a colliding callee either.
        verdict = text.replace('    runs-on: sylphx-linux-control\n    timeout-minutes: 1\n    steps:\n      - run: exit 1',
                               '    uses: ./.github/workflows/reusable.yml')
        self.assertEqual(problems(verdict, workflows=CALLEES), [])
        self.assertTrue(problems(verdict, workflows={'reusable.yml': 'on: workflow_call\nconcurrency: tests-' + GROUP + '\njobs: {check: {steps: [{run: echo check}]}}'}))

    def test_both_contract_scans_resolve_complete_callee_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / '.github/workflows'
            folder.mkdir(parents=True)
            (folder / 'ci.yml').write_text(independent_verdict_workflow())
            callee = folder / 'reusable.yml'
            for source, count in ((CALLEE, 0), (CALLEE.replace('jobs:', 'concurrency: tests-' + GROUP + '\njobs:'), 1),
                                  (None, 1)):
                if source is None:
                    callee.unlink()
                else:
                    callee.write_text(source)
                for rules in ('all', 'pr-concurrency'):
                    with self.subTest(rules=rules, source=source), patch.dict(os.environ, {}, clear=True):
                        found = [f for f in contract.scan_dir(root, rules=rules)
                                 if 'non-canonical concurrency expression' in f.problem]
                        self.assertEqual(len(found), count)
                        self.assertTrue(all(f.blocking for f in found))
            callee.write_text(CALLEE)
            for name in ('select', 'tests', 'ci-ok'):
                (folder / 'ci.yml').write_text(independent_verdict_workflow().replace(
                    f'  {name}:\n', f'  {name}:\n    strategy: {{matrix: {{lane: [one, two]}}}}\n'))
                for rules in ('all', 'pr-concurrency'):
                    with self.subTest(rules=rules, matrix_job=name), patch.dict(os.environ, {}, clear=True):
                        found = [f for f in contract.scan_dir(root, rules=rules)
                                 if 'non-canonical concurrency expression' in f.problem]
                        self.assertEqual(len(found), 1)
                        self.assertTrue(found[0].blocking)

    def test_independent_verdict_through_scoped_entrypoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / '.github/workflows'
            folder.mkdir(parents=True)
            path = folder / 'ci.yml'
            callee = folder / 'reusable.yml'
            callee.write_text(CALLEE)
            text = independent_verdict_workflow()
            env = {k: v for k, v in os.environ.items() if not k.startswith('GITHUB_')}
            env.update(CONTRACT_MODE='report-only', WORKFLOW_LINT_RULES='pr-concurrency')
            surviving = text.replace('  select:\n', '  select:\n    if: ${{ always() && true }}\n')
            matrices = tuple(text.replace(f'  {name}:\n', f'  {name}:\n    strategy: {{matrix: {{lane: [one, two]}}}}\n')
                             for name in ('select', 'tests', 'ci-ok'))
            for source, expected in ((text, 0), (text.replace('tests-' + GROUP, GROUP), 1),
                                     (surviving, 1), *((source, 1) for source in matrices)):
                path.write_text(source)
                result = subprocess.run([sys.executable, str(ROOT / '.github/actions/workflow-lint/contract.py')],
                                        cwd=root, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                self.assertEqual('non-canonical concurrency expression' in result.stdout, bool(expected))
            path.write_text(text)
            for source in (CALLEE.replace('jobs:', 'concurrency: tests-' + GROUP + '\njobs:'),
                           CALLEE.replace('    runs-on:', '    concurrency: tests-' + GROUP + '\n    runs-on:'),
                           'on: workflow_call\njobs: {nested: {uses: ./.github/workflows/next.yml}}',
                           None):
                if source is None:
                    callee.unlink()
                else:
                    callee.write_text(source)
                rejected = subprocess.run([sys.executable, str(ROOT / '.github/actions/workflow-lint/contract.py')],
                                          cwd=root, env=env, capture_output=True, text=True)
                self.assertEqual(rejected.returncode, 1, rejected.stdout + rejected.stderr)
                self.assertIn('non-canonical concurrency expression', rejected.stdout)
            callee.write_text(CALLEE)
            path.write_text(text.replace('./.github/workflows/reusable.yml',
                                         'owner/repo/.github/workflows/reusable.yml@' + 'a' * 40))
            rejected = subprocess.run([sys.executable, str(ROOT / '.github/actions/workflow-lint/contract.py')],
                                      cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(rejected.returncode, 1, rejected.stdout + rejected.stderr)
            self.assertIn('non-canonical concurrency expression', rejected.stdout)

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


class RuleSelectionTest(unittest.TestCase):
    def test_scoped_scan_never_calls_other_contract_families(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / '.github/workflows'
            folder.mkdir(parents=True)
            path = folder / 'ci.yml'
            path.write_text(workflow(trigger='pull_request'))
            with patch.dict(os.environ, {}, clear=True), \
                    patch.object(contract, 'runner_evaluations', side_effect=AssertionError('runner evaluation')), \
                    patch.object(contract, 'scan_workflow', side_effect=AssertionError('job contracts')), \
                    patch.object(contract, 'gate_coverage', side_effect=AssertionError('ci-ok closure')), \
                    patch.object(contract, 'check_declaration', side_effect=AssertionError('declarations')), \
                    patch.object(contract, 'merge_lane_findings', side_effect=AssertionError('runner routing')):
                self.assertEqual(contract.scan_dir(root, rules='pr-concurrency'), [])
                path.write_text(workflow(None, trigger='pull_request'))
                found = contract.scan_dir(root, rules='pr-concurrency')
                self.assertEqual(len(found), 1)
                self.assertTrue(found[0].blocking)
                with self.assertRaisesRegex(ValueError, 'unknown workflow-lint rules'):
                    contract.scan_dir(root, rules='typo')

    def test_scoped_entrypoint_ignores_unrelated_debt_but_not_concurrency(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / '.github/workflows'
            folder.mkdir(parents=True)
            path = folder / 'ci.yml'
            text = workflow(trigger='[pull_request, merge_group]').replace('  ci-ok:', '  isolated:')
            text = text.replace('    timeout-minutes: 1\n', '')
            text = text.replace('ubuntu-latest', 'unknown-runner')
            text = text.replace('- run: echo ok', '- uses: trstringer/manual-approval@v1\n      - run: k6 run load.js || true')
            path.write_text(text)
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith('GITHUB_') and k not in ('WORKFLOW_LINT_RULES', 'CONTRACT_MODE')}
            env.update(CONTRACT_MODE='report-only')
            command = [sys.executable, str(ROOT / '.github/actions/workflow-lint/contract.py')]
            full = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(full.returncode, 1, full.stdout + full.stderr)
            self.assertIn('manual', full.stdout)
            env['WORKFLOW_LINT_RULES'] = 'pr-concurrency'
            scoped = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(scoped.returncode, 0, scoped.stdout + scoped.stderr)
            self.assertIn('0 violation(s)', scoped.stdout)
            self.assertNotIn('::warning', scoped.stdout)
            path.write_text(text.replace('concurrency:', 'not-concurrency:', 1))
            refused = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
            self.assertIn('non-canonical concurrency expression', refused.stdout)
            for invalid in ('', 'typo', 'pr-concurrency,all'):
                env['WORKFLOW_LINT_RULES'] = invalid
                unknown = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True)
                self.assertNotEqual(unknown.returncode, 0)
                self.assertIn('unknown workflow-lint rules', unknown.stderr)

    def test_composite_selects_only_validator_and_scoped_contract(self):
        action = yaml.safe_load((ROOT / '.github/actions/workflow-lint/action.yml').read_text())
        self.assertEqual(action['inputs']['rules']['default'], 'all')
        for rules in ('all', 'pr-concurrency'):
            context = {'inputs.rules': {rules}, 'steps.restore-binary.outputs.cache-hit': {'false'}}
            selected = [step for step in action['runs']['steps']
                        if not step.get('if') or expression('${{ ' + step['if'] + ' }}', context) == {True}]
            if rules == 'pr-concurrency':
                self.assertEqual([s['name'] for s in selected],
                                 ['Validate rule selection', 'Required workflow contract'])
            else:
                self.assertEqual(len(selected), len(action['runs']['steps']))
            final = selected[-1]
            self.assertEqual(final['env']['WORKFLOW_LINT_RULES'], '${{ inputs.rules }}')
            self.assertIn('$GITHUB_ACTION_PATH/contract.py', final['run'])
        validator = action['runs']['steps'][0]['run']
        for rules, expected in (('all', 0), ('pr-concurrency', 0), ('', 1), ('typo', 1)):
            checked = subprocess.run(['bash', '-euo', 'pipefail', '-c', validator],
                                     env={**os.environ, 'RULES': rules}, capture_output=True, text=True)
            self.assertEqual(checked.returncode, expected, checked.stdout + checked.stderr)


if __name__ == '__main__':
    unittest.main()
