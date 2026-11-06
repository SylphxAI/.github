"""Public .github CI is free; private callers retain their self-hosted lanes."""
from pathlib import Path
import sys
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from merge_lane import expression, runner_evaluations, merge_lane_findings

WORKFLOWS = {p.name: p.read_text() for p in (ROOT / '.github/workflows').glob('*.yml')}


class PublicHostedRunners(unittest.TestCase):
    def test_all_reachable_own_jobs_are_hosted_only_when_public(self):
        public = runner_evaluations(WORKFLOWS, private=False)
        self.assertTrue(public)
        self.assertTrue({'pull_request', 'merge_group', 'workflow_run', 'workflow_dispatch'} <=
                        {e.event for e in public})
        for route in public:
            with self.subTest(file=route.file, job=route.job, event=route.event):
                self.assertEqual(route.errors, ())
                self.assertEqual(route.values, {'ubuntu-latest'})
        private = runner_evaluations(WORKFLOWS, private=True)
        self.assertTrue(private)
        for route in private:
            with self.subTest(file=route.file, job=route.job, event=route.event):
                self.assertEqual(route.errors, ())
                self.assertTrue(route.values)
                self.assertTrue(all(label.startswith('sylphx-linux-') for label in route.values))
        self.assertEqual(merge_lane_findings(WORKFLOWS, private=False, org='SylphxAI',
                                            check_all_events=True), [])

    def test_review_visibility_is_executing_repository_not_producer_or_fork(self):
        # workflow_run.head_repository / pull_request.head.repo are not the
        # executing repository and must never choose its billing surface.
        workflow = yaml.load(WORKFLOWS['pr-review.yml'], Loader=yaml.BaseLoader)
        selector = workflow['jobs']['review']['runs-on']
        for event in ('workflow_run', 'workflow_dispatch', 'merge_group', 'pull_request'):
            for private in (False, True):
                for head_private in (False, True):
                    context = {
                        'github.event_name': {event},
                        'github.event.repository.private': {private},
                        'github.event.repository.visibility': {'private' if private else 'public'},
                        'github.event.workflow_run.head_repository.private': {head_private},
                        'github.event.pull_request.head.repo.private': {head_private},
                    }
                    self.assertEqual(expression(selector, context),
                                     {'sylphx-linux-standard' if private else 'ubuntu-latest'})
        self.assertEqual(workflow['permissions']['id-token'], 'write')
        action = workflow['jobs']['review']['steps'][-1]
        self.assertEqual(action['uses'], './.github/actions/pr-review')
        self.assertEqual(action['with']['ci-name'], 'pr-review-shared')

    def test_rust_call_without_override_has_identical_old_labels(self):
        # Omitting the new input must preserve both size and merge-lane behavior,
        # not replace the old dynamic selector with a fixed standard label.
        old = "${{ format('sylphx-linux-{0}{1}', inputs.runner-class == 'xlarge' && 'xlarge' || 'standard', github.event_name == 'merge_group' && '-merge' || '') }}"
        for private in (False, True):
            for size in ('standard', 'xlarge'):
                for event in ('pull_request', 'merge_group', 'workflow_dispatch', 'push'):
                    caller = f'''on: {event}
jobs:
  rust:
    uses: ./.github/workflows/rust-ci.yml
    with:
      runner-class: {size}
'''
                    route, = runner_evaluations({'caller.yml': caller,
                                                 'rust-ci.yml': WORKFLOWS['rust-ci.yml']}, private=private)
                    context = {'inputs.runner-class': {size}, 'github.event_name': {event}}
                    self.assertEqual(route.errors, ())
                    self.assertEqual(route.values, expression(old, context))

    def test_private_call_cannot_select_hosted_even_with_override(self):
        for private in (False, True):
            for override in ('ubuntu-latest', 'ubuntu-24.04-16core', 'macos-latest', 'sylphx-linux-xlarge', ''):
                caller = f'''on: [pull_request, merge_group]
jobs:
  rust:
    uses: ./.github/workflows/rust-ci.yml
    with:
      runs-on: '{override}'
'''
                routes = runner_evaluations({'caller.yml': caller,
                                             'rust-ci.yml': WORKFLOWS['rust-ci.yml']}, private=private)
                for route in routes:
                    expected = 'ubuntu-latest' if not private and override == 'ubuntu-latest' else (
                        'sylphx-linux-standard-merge' if route.event == 'merge_group' else 'sylphx-linux-standard')
                    self.assertEqual(route.errors, ())
                    self.assertEqual(route.values, {expected})


if __name__ == '__main__':
    unittest.main()
