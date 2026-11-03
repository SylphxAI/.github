"""Executable counterexamples for the one shared R8 implementation."""
import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from merge_lane import merge_lane_findings, merge_lane_misses

P = 'sylphx-linux-standard'
M = P + '-merge'
SELECTOR = "${{ github.event_name == 'merge_group' && '" + M + "' || '" + P + "' }}"


def workflow(runner=P, condition='', trigger='[pull_request, merge_group]'):
    return f'on: {trigger}\njobs:\n  build:\n    runs-on: "{runner}"\n' + (f'    if: {condition}\n' if condition else '')


class MergeLane(unittest.TestCase):
    def test_all_shared_sizes_and_yaml_forms(self):
        for size in ('small', 'standard', 'large', 'xlarge', '2xlarge'):
            pool = 'sylphx-linux-' + size
            for text in (workflow(pool), f"'on': [merge_group]\n'jobs': {{b: {{'runs-on': {pool}}}}}",
                         f'on: merge_group\njobs:\n  b:\n    runs-on: [self-hosted, {pool}]\n',
                         f'on: merge_group\njobs:\n  b:\n    runs-on: {{group: owned, labels: {pool}}}\n'):
                with self.subTest(size=size, text=text):
                    self.assertEqual(len(merge_lane_misses(text)), 1)
                    self.assertEqual(merge_lane_misses(text.replace(pool, pool + '-merge')), [])

    def test_canonical_selection_and_public_fallback(self):
        self.assertEqual(merge_lane_misses(workflow(SELECTOR)), [])
        value = "${{ github.event.repository.private && 'sylphx-linux-standard' || 'ubuntu-latest' }}"
        self.assertEqual(merge_lane_misses(workflow(value), private=False), [])
        self.assertEqual(merge_lane_misses(workflow(value), private=True), ['build'])
        # Public self-hosted jobs are NOT blanket exempt.
        self.assertEqual(merge_lane_misses(workflow(P), private=False), ['build'])

    def test_comments_wrong_event_size_and_unknown_fallback_do_not_excuse(self):
        self.assertEqual(merge_lane_misses(workflow(P) + '# ' + M), ['build'])
        for value in (SELECTOR.replace('merge_group', 'pull_request'),
                      SELECTOR.replace(M, 'sylphx-linux-xlarge-merge'),
                      "${{ needs.plan.outputs.runner || 'sylphx-linux-standard' }}"):
            with self.subTest(value=value):
                self.assertEqual(merge_lane_misses(workflow(value)), ['build'])

    def test_job_conditions_not_step_conditions(self):
        for condition in ("github.event_name != 'merge_group'", "github.event_name == 'pull_request'",
                          "always() && github.event_name == 'push'", "(github.event_name == 'push' || github.event_name == 'pull_request')"):
            self.assertEqual(merge_lane_misses(workflow(condition=condition)), [])
        for condition in ("github.event_name == 'pull_request' || true", "needs.plan.outputs.run == 'true'",
                          "contains(github.event_name, 'merge_group')"):
            self.assertEqual(merge_lane_misses(workflow(condition=condition)), ['build'])
        self.assertEqual(merge_lane_misses(workflow() + "    steps:\n      - if: github.event_name == 'pull_request'\n        run: echo pr\n"), ['build'])

    def test_local_reuse_propagates_context_and_ancestor_conditions(self):
        caller = "on: [merge_group]\njobs:\n  call:\n    uses: ./.github/workflows/reuse.yml\n"
        reuse = workflow(trigger='[workflow_call]')
        files = {'ci.yml': caller, 'reuse.yml': reuse}
        findings = merge_lane_findings(files)
        self.assertEqual([(f.file, f.job) for f in findings], [('reuse.yml', 'build')])
        files['ci.yml'] += "    if: github.event_name != 'merge_group'\n"
        self.assertEqual(merge_lane_findings(files), [])
        # A reusable workflow by itself is not merge reachable.
        self.assertEqual(merge_lane_misses(reuse), [])

    def test_inputs_defaults_overrides_and_nested_calls(self):
        caller = "on: [merge_group]\njobs:\n  call:\n    uses: ./.github/workflows/reuse.yml\n"
        reuse = "on:\n  workflow_call:\n    inputs:\n      runner:\n        type: string\n        default: sylphx-linux-standard\njobs:\n  b:\n    runs-on: ${{ inputs.runner }}\n"
        files = {'ci.yml': caller, 'reuse.yml': reuse}
        self.assertEqual(len(merge_lane_findings(files)), 1)
        files['ci.yml'] += f'    with:\n      runner: {M}\n'
        self.assertEqual(merge_lane_findings(files), [])
        files['ci.yml'] = caller + f'    with:\n      runner: "{SELECTOR}"\n'
        self.assertEqual(merge_lane_findings(files), [])
        files['reuse.yml'] = reuse.replace('    runs-on: ${{ inputs.runner }}',
            '    uses: ./.github/workflows/nested.yml\n    with:\n      runner: ${{ inputs.runner }}')
        files['nested.yml'] = reuse
        self.assertEqual(merge_lane_findings(files), [])
        # Circular calls terminate without inventing runnable jobs.
        files['nested.yml'] = files['reuse.yml']
        self.assertEqual(merge_lane_findings(files), [])

    def test_matrix_values_resolve_instead_of_blanket_skip(self):
        text = workflow('${{ matrix.runner }}') + f'    strategy:\n      matrix:\n        runner: [{P}, ubuntu-latest]\n'
        self.assertEqual(merge_lane_misses(text), ['build'])
        self.assertEqual(merge_lane_misses(text.replace(P, M)), [])
        text = workflow('${{ matrix.profile.runner }}') + f'    strategy:\n      matrix:\n        include:\n          - profile:\n              runner: {P}\n'
        self.assertEqual(merge_lane_misses(text), ['build'])

    def test_org_serving_contract_blocks_unserved_twins_not_plain_capacity(self):
        for org, served, unserved in (
            ('Cubeage', 'standard', 'xlarge'), ('OzyrixLtd', 'large', 'standard'),
            ('EpiowAI', 'xlarge', '2xlarge')):
            with self.subTest(org=org):
                notes = []
                self.assertEqual(merge_lane_misses(workflow('sylphx-linux-' + served), org=org), ['build'])
                self.assertEqual(merge_lane_misses(workflow('sylphx-linux-' + served + '-merge'), org=org), [])
                self.assertEqual(merge_lane_misses(workflow('sylphx-linux-' + unserved), org=org, notes=notes), [])
                self.assertEqual(len(notes), 1)
                self.assertIn('no same-size merge twin is served', notes[0].problem)
                self.assertEqual(merge_lane_misses(workflow('sylphx-linux-' + unserved + '-merge'), org=org), ['build'])
        for size in ('small', 'standard', 'large', 'xlarge', '2xlarge'):
            self.assertEqual(merge_lane_misses(workflow('sylphx-linux-' + size + '-merge'), org='SylphxAI'), [])

    def test_github_loose_equality_and_numeric_scalars(self):
        for condition in ("${{ '0' == false }}", "${{ '' == null }}", "${{ '1' == true }}",
                          "${{ 'FOO' == 'foo' }}", "${{ '1e2' == 100 }}"):
            self.assertEqual(merge_lane_misses(workflow(condition=condition)), ['build'])
        for condition in ("${{ 'word' == false }}", "${{ 'true' == true }}", "${{ '2' == true }}"):
            self.assertEqual(merge_lane_misses(workflow(condition=condition)), [])
        self.assertEqual(merge_lane_misses(workflow(condition='matrix.count == 0') +
            '    strategy:\n      matrix:\n        count: [0]\n'), ['build'])
        files = {'ci.yml': 'on: merge_group\njobs:\n  call:\n    uses: ./.github/workflows/reuse.yml\n',
                 'reuse.yml': 'on:\n  workflow_call:\n    inputs:\n      count:\n        type: number\n        default: 0\n' +
                    workflow(condition='inputs.count == 0').split('jobs:', 1)[1].join(['jobs:', ''])}
        self.assertEqual(len(merge_lane_findings(files)), 1)

    def test_known_format_and_json_matrix_are_not_opaque(self):
        self.assertEqual(merge_lane_misses(workflow("${{ format('sylphx-linux-{0}', 'standard') }}")), ['build'])
        self.assertEqual(merge_lane_misses(workflow("${{ format('sylphx-linux-{0}-merge', 'standard') }}")), [])
        text = workflow('${{ matrix.runner }}') + '    strategy:\n      matrix: ${{ fromJSON(\'{"runner":["sylphx-linux-standard"]}\') }}\n'
        self.assertEqual(merge_lane_misses(text), ['build'])
        self.assertEqual(merge_lane_misses(text.replace('standard', 'standard-merge')), [])
        text = workflow('${{ matrix.runner }}') + '    strategy:\n      matrix: ${{ fromJSON(inputs.matrix) }}\n'
        caller = 'on: merge_group\njobs:\n  call:\n    uses: ./.github/workflows/reuse.yml\n    with:\n      matrix: \'{"runner":["sylphx-linux-standard"]}\'\n'
        text = text.replace('on: [pull_request, merge_group]', 'on: workflow_call')
        self.assertEqual(len(merge_lane_findings({'ci.yml': caller, 'reuse.yml': text})), 1)

    def test_other_events_control_hosted_and_unresolved_labels(self):
        for runner in ('ubuntu-latest', 'sylphx-linux-control', '${{ inputs.runner }}', '${{ matrix.runner }}'):
            self.assertEqual(merge_lane_misses(workflow(runner)), [])
        self.assertEqual(merge_lane_misses(workflow(trigger='[pull_request]')), [])


if __name__ == '__main__':
    unittest.main()
