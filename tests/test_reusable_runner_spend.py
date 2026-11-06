"""Required CI refuses hosted reusable routes without affirmative visibility."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from merge_lane import expression, runner_evaluations
from reusable_runner_spend import findings, library_findings, safe_selector

WORKFLOWS = {p.name: p.read_text() for p in (ROOT / '.github/workflows').glob('*.yml')}
EVENTS = ('pull_request', 'push', 'merge_group', 'workflow_dispatch', 'schedule')
OLD_RUST = "${{ format('sylphx-linux-{0}{1}', inputs.runner-class == 'xlarge' && 'xlarge' || 'standard', github.event_name == 'merge_group' && '-merge' || '') }}"


def rust_routes(event, *, private, override=None, size='standard', repository_present=True):
    caller = f'''on: {event}
jobs:
  rust:
    uses: ./.github/workflows/rust-ci.yml
    with:
      runner-class: {size}
'''
    if override is not None:
        caller += f'      runs-on: {override!r}\n'
    route, = runner_evaluations({'caller.yml': caller, 'rust-ci.yml': WORKFLOWS['rust-ci.yml']},
                                private=private, repository_present=repository_present)
    if route.errors:
        raise AssertionError(route.errors)
    return route.values


class ReusableRunnerSpend(unittest.TestCase):
    def test_entire_reusable_library_and_all_actions_match_spend_contract(self):
        # unittest discovery in Project control makes this a required CI check.
        self.assertEqual(library_findings(ROOT), [])

    def test_private_caller_passing_literal_ubuntu_latest_stays_self_hosted(self):
        for event in EVENTS:
            for present in (False, True):
                with self.subTest(event=event, repository_present=present):
                    expected = 'sylphx-linux-standard-merge' if event == 'merge_group' else 'sylphx-linux-standard'
                    self.assertEqual(rust_routes(event, private=True, override='ubuntu-latest',
                                                 repository_present=present), {expected})

    def test_public_caller_with_hosted_override_gets_ubuntu_latest(self):
        for event in EVENTS:
            with self.subTest(event=event):
                self.assertEqual(rust_routes(event, private=False, override='ubuntu-latest'), {'ubuntu-latest'})

    def test_missing_repository_never_proves_public_even_for_public_caller(self):
        for event in EVENTS:
            with self.subTest(event=event):
                result = rust_routes(event, private=False, override='ubuntu-latest', repository_present=False)
                self.assertTrue(all(label.startswith('sylphx-linux-') for label in result))

    def test_no_input_preserves_old_size_and_merge_labels(self):
        for private in (True, False):
            for event in EVENTS:
                for size in ('standard', 'xlarge'):
                    for present in (True, False):
                        with self.subTest(private=private, event=event, size=size, present=present):
                            old = expression(OLD_RUST, {'inputs.runner-class': {size}, 'github.event_name': {event}})
                            self.assertEqual(rust_routes(event, private=private, size=size,
                                                         repository_present=present), old)

    def test_unknown_visibility_and_inputs_cannot_declare_public(self):
        import yaml
        selector = yaml.load(WORKFLOWS['rust-ci.yml'], Loader=yaml.BaseLoader)['jobs']['rust']['runs-on']
        for visibility in (None, '', 'private', 'internal', 'unknown'):
            for override in ('ubuntu-latest', 'public', 'macos-latest', 'windows-latest', ''):
                for event in EVENTS:
                    result = expression(selector, {'github.event.repository.visibility': {visibility},
                                        'inputs.runs-on': {override}, 'inputs.runner-class': {'standard'},
                                        'github.event_name': {event}})
                    self.assertTrue(all(label.startswith('sylphx-linux-') for label in result))

    def test_existing_self_hosted_input_families_are_preserved_and_hosted_is_ignored(self):
        import yaml
        for name, key in (('image-lane.yml', 'runner-class'), ('changes.yml', 'runner')):
            document = yaml.load(WORKFLOWS[name], Loader=yaml.BaseLoader)
            for job in document['jobs'].values():
                if 'runs-on' not in job:
                    continue
                for value in ('sylphx-linux-standard', 'sylphx-linux-standard-merge',
                              'sylphx-linux-large-merge', 'sylphx-linux-xlarge',
                              'ubuntu-latest', 'macos-latest', 'ubuntu-24.04-16core', ''):
                    result = expression(job['runs-on'], {'inputs.' + key: {value}})
                    self.assertEqual(result, {value if value.startswith('sylphx-linux-') else 'sylphx-linux-standard'})

    def test_all_reusable_visibility_routes_fail_closed_when_payload_is_absent(self):
        import yaml
        for name in ('rust-ci.yml', 'junit-consumer.yml', 'publish-npm.yml'):
            document = yaml.load(WORKFLOWS[name], Loader=yaml.BaseLoader)
            for job in document['jobs'].values():
                if 'runs-on' not in job:
                    continue
                result = expression(job['runs-on'], {'github.event.repository.visibility': {None},
                                    'inputs.runs-on': {'ubuntu-latest'}, 'inputs.runner-class': {'standard'},
                                    'github.event_name': {'schedule'}})
                self.assertEqual(result, {'sylphx-linux-standard'})

    def test_guard_rejects_hosted_unknown_and_null_truthiness_selectors(self):
        bad = [
            'ubuntu-latest', 'windows-latest', 'macos-latest', 'ubuntu-24.04-16core',
            "${{ inputs.runner }}", "${{ matrix.runner }}", "${{ fromJSON(inputs.runner) }}",
            "${{ !github.event.repository.private && 'ubuntu-latest' || 'sylphx-linux-standard' }}",
            "${{ github.event.repository.private == false && 'ubuntu-latest' || 'sylphx-linux-standard' }}",
            "${{ github.event.repository.private && 'sylphx-linux-standard' || 'ubuntu-latest' }}",
            "${{ inputs.visibility == 'public' && 'ubuntu-latest' || 'sylphx-linux-standard' }}",
            "${{ github.event.repository.visibility == 'public' && inputs.runner || 'sylphx-linux-standard' }}",
            "${{ format('sylphx-linux-standard') || 'ubuntu-latest' }}",
            "${{ format('sylphx-linux-standard') && 'ubuntu-latest' }}",
            "${{ format('sylphx-linux-standard' && 'ubuntu-latest') }}",
            "${{ startsWith(inputs.runner, 'sylphx-linux-') && inputs.other || 'sylphx-linux-standard' }}",
            ['ubuntu-latest'], {'labels': 'ubuntu-latest'},
        ]
        for selector in bad:
            with self.subTest(selector=selector):
                self.assertFalse(safe_selector(selector))

    def test_guard_accepts_only_constructively_safe_selector_forms(self):
        good = [
            'sylphx-linux-standard', ['self-hosted', 'macos', 'sylphx', 'standard'], OLD_RUST,
            "${{ startsWith(inputs.runner, 'sylphx-linux-') && inputs.runner || 'sylphx-linux-standard' }}",
            "${{ github.event.repository.visibility == 'public' && 'ubuntu-latest' || 'sylphx-linux-standard' }}",
            "${{ github.event.repository.visibility == 'public' && inputs.runs-on == 'ubuntu-latest' && 'ubuntu-latest' || 'sylphx-linux-standard' }}",
        ]
        for selector in good:
            with self.subTest(selector=selector):
                self.assertTrue(safe_selector(selector))

    def test_guard_covers_mapping_list_and_scalar_workflow_call_triggers(self):
        for trigger in ('\n  workflow_call:', '[workflow_call]', 'workflow_call'):
            source = f'on: {trigger}\njobs:\n  unsafe:\n    runs-on: ubuntu-latest\n'
            self.assertEqual(len(findings({'unsafe.yml': source}, {})), 1)
        self.assertEqual(findings({'own.yml': 'on: push\njobs:\n  own:\n    runs-on: ubuntu-latest\n'}, {}), [])

    def test_unknown_nested_reusable_workflow_cannot_hide_runner_selection(self):
        caller = 'on: workflow_call\njobs:\n  nested:\n    uses: other/repo/.github/workflows/ci.yml@main\n'
        self.assertEqual(len(findings({'caller.yml': caller}, {})), 1)
        local = caller.replace('other/repo/.github/workflows/ci.yml@main', './.github/workflows/ci.yml')
        callee = 'on: workflow_call\njobs:\n  job:\n    runs-on: sylphx-linux-standard\n'
        self.assertEqual(findings({'caller.yml': local, 'ci.yml': callee}, {}), [])
        self.assertEqual(len(findings({'caller.yml': local, 'ci.yml': callee.replace('sylphx-linux-standard', 'ubuntu-latest')}, {})), 1)

    def test_composites_inherit_runner_and_cannot_hide_a_runs_on_declaration(self):
        good = 'runs:\n  using: composite\n  steps:\n    - run: echo ok\n      shell: bash\n'
        bad = good + 'jobs:\n  hidden:\n    runs-on: ubuntu-latest\n'
        self.assertEqual(findings({}, {'good/action.yml': good}), [])
        self.assertEqual(len(findings({}, {'bad/action.yml': bad})), 1)


if __name__ == '__main__':
    unittest.main()
