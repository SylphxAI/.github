"""The declaration is resolved at lint time and cannot drift from its source."""
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.github/actions/workflow-lint'))
import expected_checks as compiler
import contract


def simple(name='test', trigger='on: [pull_request, merge_group]', extra=''):
    return f'{trigger}\njobs:\n  test:\n    name: {name}\n    runs-on: ubuntu-latest\n    timeout-minutes: 5\n    steps: [{{run: true}}]\n{extra}'


class ExpectedChecksContractTest(unittest.TestCase):
    def compile(self, workflows, **kwargs):
        return compiler.compile_declaration(workflows, **kwargs)

    def test_resolved_schema_combines_identical_event_filters(self):
        self.assertEqual(self.compile({'test.yml': simple()}), {'version': 2, 'checks': [{
            'name': 'test', 'workflow': 'test.yml', 'events': ['merge_group', 'pull_request'],
            'paths': None, 'paths_ignore': None}]})

    def test_event_specific_path_filters_are_not_shared(self):
        trigger = 'on:\n  pull_request:\n    paths: ["src/**", "!src/generated/**", "src/generated/kept.ts"]\n  merge_group:'
        records = self.compile({'test.yml': simple(trigger=trigger)})['checks']
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]['events'], ['merge_group'])
        self.assertIsNone(records[0]['paths'])
        self.assertEqual(records[1]['paths'], ['src/**', '!src/generated/**', 'src/generated/kept.ts'])

    def test_paths_ignore_is_literal_and_preserves_order(self):
        record, = self.compile({'test.yml': simple(trigger='on:\n  pull_request:\n    paths-ignore: ["docs/**", "**.md"]')})['checks']
        self.assertEqual(record['paths'], ['**'])
        self.assertEqual(record['paths_ignore'], ['docs/**', '**.md'])

    def test_strict_closure_is_not_duplicated_in_independent_declaration(self):
        extra = '  ci-ok:\n    needs: test\n    if: always()\n    steps: [{run: true}]\n'
        self.assertEqual(self.compile({'test.yml': simple(extra=extra)})['checks'], [])

    def test_reasoned_advisory_is_not_required_and_body_spoof_cannot_hide_checks(self):
        self.assertEqual(self.compile({'test.yml': '# optimistic-merge: advisory — separate optional evidence\n' + simple()})['checks'], [])
        self.assertEqual(len(self.compile({'test.yml': '# optimistic-merge: advisory\n' + simple()})['checks']), 1)
        text = simple().replace('steps: [{run: true}]', 'steps:\n      - run: |\n          # optimistic-merge: advisory — spoof')
        self.assertEqual(len(self.compile({'test.yml': text})['checks']), 1)

    def test_postmerge_and_event_disabled_jobs_are_not_expected(self):
        self.assertEqual(self.compile({'test.yml': simple(trigger='on: push')})['checks'], [])
        text = simple(trigger='on: [pull_request, merge_group]').replace('    name: test', "    if: github.event_name == 'pull_request'\n    name: test")
        record, = self.compile({'test.yml': text})['checks']
        self.assertEqual(record['events'], ['pull_request'])

    def test_conditional_child_is_represented_by_always_running_aggregate(self):
        extra = '  verified:\n    name: verified\n    needs: test\n    if: always() && !cancelled()\n    steps: [{run: true}]\n'
        text = simple(extra=extra).replace('    name: test', "    if: needs.scope.outputs.selected == 'true'\n    name: test")
        self.assertEqual([r['name'] for r in self.compile({'test.yml': text})['checks']], ['verified'])
        with self.assertRaisesRegex(compiler.DeclarationError, 'always-running required aggregate'):
            self.compile({'test.yml': text.replace('    if: always() && !cancelled()\n', '')})

    def test_unaggregated_conditional_terminal_refuses(self):
        with self.assertRaisesRegex(compiler.DeclarationError, 'conditional check'):
            self.compile({'test.yml': simple().replace('    name: test', "    if: github.event.pull_request.draft == false\n    name: test")})

    def test_required_dynamic_and_matrix_names_refuse(self):
        for text in [simple('${{ inputs.name }}'), simple().replace('    name: test', '    strategy:\n      matrix:\n        version: [1, 2]\n    name: test')]:
            with self.subTest(text=text), self.assertRaisesRegex(compiler.DeclarationError, 'literal, non-matrix'):
                self.compile({'test.yml': text})

    def test_optional_matrix_child_under_aggregate_is_not_a_required_matrix_name(self):
        text = simple(extra='  verified:\n    needs: test\n    if: always()\n    steps: [{run: true}]\n')
        text = text.replace('    name: test', '    if: inputs.selected\n    strategy:\n      matrix:\n        version: [1, 2]\n    name: test')
        self.assertEqual([r['name'] for r in self.compile({'test.yml': text})['checks']], ['verified'])

    def test_duplicate_names_cannot_alias_across_workflows(self):
        with self.assertRaisesRegex(compiler.DeclarationError, 'ambiguous check name'):
            self.compile({'one.yml': simple(), 'two.yml': simple()})
        records = self.compile({'one.yml': simple(trigger='on: pull_request'), 'two.yml': simple(trigger='on: merge_group')})['checks']
        self.assertEqual(len(records), 2)

    def test_local_reusable_check_names_include_caller_and_callee(self):
        caller = 'on: pull_request\njobs:\n  call:\n    name: caller\n    uses: ./.github/workflows/reusable.yml\n'
        called = simple('callee', trigger='on: workflow_call')
        records = self.compile({'caller.yml': caller, 'reusable.yml': called})['checks']
        self.assertEqual(records, [{'name': 'caller / callee', 'workflow': 'caller.yml', 'events': ['pull_request'], 'paths': None, 'paths_ignore': None}])

    def test_reusable_strict_gate_does_not_require_its_matrix_children(self):
        caller = 'on: pull_request\njobs:\n  call:\n    uses: ./.github/workflows/reusable.yml\n'
        called = simple('${{ matrix.name }}', trigger='on: workflow_call', extra='  ci-ok:\n    if: always()\n    needs: test\n    steps: [{run: true}]\n')
        called = called.replace('    name:', '    strategy: {matrix: {name: [one, two]}}\n    name:', 1)
        self.assertEqual(self.compile({'caller.yml': caller, 'reusable.yml': called})['checks'][0]['name'], 'call / ci-ok')

    def test_remote_reusables_are_resolved_only_at_full_sha(self):
        pin = 'a' * 40
        target = 'org/repo/.github/workflows/test.yml@' + pin
        text = 'on: pull_request\njobs:\n  call:\n    uses: ' + target + '\n'
        requested = []
        def remote(value):
            requested.append(value)
            return simple('callee', trigger='on: workflow_call')
        self.assertEqual(self.compile({'caller.yml': text}, resolve_remote=remote)['checks'][0]['name'], 'call / callee')
        self.assertEqual(requested, [target])
        with self.assertRaisesRegex(compiler.DeclarationError, 'immutable SHA'):
            self.compile({'caller.yml': text.replace(pin, 'main')}, resolve_remote=remote)

    def test_nested_reusable_names_and_recursion(self):
        caller = 'on: pull_request\njobs:\n  call:\n    uses: ./.github/workflows/nested.yml\n'
        nested = 'on: workflow_call\njobs:\n  inside:\n    uses: ./.github/workflows/leaf.yml\n'
        workflows = {'caller.yml': caller, 'nested.yml': nested, 'leaf.yml': simple('leaf', trigger='on: workflow_call')}
        self.assertEqual(self.compile(workflows)['checks'][0]['name'], 'call / inside / leaf')
        workflows['leaf.yml'] = nested
        with self.assertRaisesRegex(compiler.DeclarationError, 'recursive reusable'):
            self.compile(workflows)

    def test_enrollment_is_explicit_but_every_source_or_data_drift_blocks(self):
        workflows = {'test.yml': simple()}
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            folder = root / '.github/workflows'
            folder.mkdir(parents=True)
            (folder / 'test.yml').write_text(workflows['test.yml'])
            self.assertEqual(compiler.check_declaration(root, workflows), [])
            path = root / compiler.DECLARATION
            valid = self.compile(workflows)
            path.write_text(json.dumps(valid))
            self.assertEqual(compiler.check_declaration(root, workflows), [])
            path.write_text(json.dumps({'version': 2, 'checks': []}))
            self.assertTrue(compiler.check_declaration(root, workflows))
            found = [f for f in contract.scan_dir(root) if f.file == compiler.DECLARATION]
            self.assertEqual(len(found), 1)
            self.assertTrue(found[0].blocking)
            self.assertEqual(contract.exit_code(found, 'report-only'), 1)
            path.write_text(json.dumps(valid))
            for mutated in [simple('renamed'), simple(trigger='on: pull_request'), simple(trigger='on:\n  pull_request:\n    paths: ["src/**"]')]:
                self.assertTrue(compiler.check_declaration(root, {'test.yml': mutated}))
            path.write_text('not-json')
            self.assertTrue(compiler.check_declaration(root, workflows))

    def test_invalid_filter_combinations_refuse(self):
        for trigger in ['on:\n  pull_request:\n    paths: ["x/**"]\n    paths-ignore: ["x/generated/**"]',
                        'on:\n  pull_request:\n    paths: "src/**"',
                        'on:\n  pull_request:\n    paths: ["${{ inputs.paths }}"]']:
            with self.subTest(trigger=trigger), self.assertRaises(compiler.DeclarationError):
                self.compile({'test.yml': simple(trigger=trigger)})


if __name__ == '__main__':
    unittest.main()
