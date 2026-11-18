"""Hosted CI requires a public repository; self-hosted CI works in either visibility."""
from pathlib import Path
import sys
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from merge_lane import expression, runner_evaluations, merge_lane_findings

WORKFLOWS = {p.name: p.read_text() for p in (ROOT / '.github/workflows').glob('*.yml')}


class PublicHostedRunners(unittest.TestCase):
    def assert_public_runner_route(self, route):
        self.assertEqual(route.errors, ())
        expected = 'ubuntu-latest'
        if (route.file, route.job) == ('project-control.yml', 'ci-range-consumer'):
            expected = ('sylphx-linux-standard-merge' if route.event == 'merge_group'
                        else 'sylphx-linux-standard')
        self.assertEqual(route.values, {expected})

    def test_all_reachable_own_jobs_are_hosted_only_when_public(self):
        public = runner_evaluations(WORKFLOWS, private=False)
        self.assertTrue(public)
        self.assertTrue({'pull_request', 'merge_group', 'workflow_run', 'workflow_dispatch'} <=
                        {e.event for e in public})
        for route in public:
            with self.subTest(file=route.file, job=route.job, event=route.event):
                self.assert_public_runner_route(route)
        private = runner_evaluations(WORKFLOWS, private=True)
        self.assertTrue(private)
        for route in private:
            with self.subTest(file=route.file, job=route.job, event=route.event):
                self.assertEqual(route.errors, ())
                self.assertTrue(route.values)
                self.assertTrue(all(label.startswith('sylphx-linux-') for label in route.values))
        self.assertEqual(merge_lane_findings(WORKFLOWS, private=False, org='SylphxAI',
                                            check_all_events=True), [])

    def test_private_lightweight_guards_use_control_even_without_visibility_payload(self):
        guards = {('project-control.yml', job) for job in
                  ('stack', 'project-control', 'template-plan', 'junit-producer')}
        guards.add(('junit-consumer.yml', 'consume'))
        for payload in (True, False):
            routes = [route for route in runner_evaluations(WORKFLOWS, private=True,
                                                           repository_present=payload)
                      if (route.file, route.job) in guards]
            self.assertEqual({(route.file, route.job) for route in routes}, guards)
            for route in routes:
                with self.subTest(payload=payload, file=route.file, job=route.job, event=route.event):
                    self.assertEqual(route.errors, ())
                    self.assertEqual(route.values, {'sylphx-linux-control'})

    def test_other_public_job_cannot_inherit_consumer_self_hosted_exception(self):
        workflow = yaml.load(WORKFLOWS['project-control.yml'], Loader=yaml.BaseLoader)
        workflow['jobs']['tests']['runs-on'] = 'sylphx-linux-standard'
        mutated = {**WORKFLOWS, 'project-control.yml': yaml.safe_dump(workflow)}
        routes = [route for route in runner_evaluations(mutated, private=False)
                  if (route.file, route.job) == ('project-control.yml', 'tests')]
        self.assertTrue(routes)
        for route in routes:
            with self.subTest(event=route.event):
                self.assertEqual(route.errors, ())
                self.assertEqual(route.values, {'sylphx-linux-standard'})
                with self.assertRaises(AssertionError):
                    self.assert_public_runner_route(route)

    def test_ci_range_consumer_always_uses_self_hosted_event_lanes(self):
        for private in (False, True):
            routes = [route for route in runner_evaluations(WORKFLOWS, private=private)
                      if route.file == 'project-control.yml' and route.job == 'ci-range-consumer']
            self.assertEqual({route.event for route in routes},
                             {'pull_request', 'merge_group', 'push', 'workflow_dispatch'})
            for route in routes:
                with self.subTest(private=private, event=route.event):
                    self.assertEqual(route.errors, ())
                    self.assertEqual(route.values, {'sylphx-linux-standard-merge'
                                                   if route.event == 'merge_group'
                                                   else 'sylphx-linux-standard'})

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
