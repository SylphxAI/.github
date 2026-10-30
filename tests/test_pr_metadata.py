"""Event routing preserves code evidence across changes to PR metadata."""
from pathlib import Path
import copy
import importlib.util
import unittest
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
STARTERS = ROOT / 'workflow-templates'
CODE = [ROOT / '.github/workflows/project-control.yml'] + [
    STARTERS / (name + '.yml') for name in ('ci-fast-path', 'optimistic-gate', 'rust-ci')]
METADATA = [ROOT / '.github/workflows/pr-metadata.yml', STARTERS / 'pr-metadata.yml']


def document(path):
    return yaml.safe_load(path.read_text())


def accepts(workflow, event, action=None):
    triggers = workflow[True]
    if event not in triggers:
        return False
    options = triggers[event] or {}
    return action is None or action in options.get('types', ['opened', 'synchronize', 'reopened'])


spec = importlib.util.spec_from_file_location('pr_base_ci', ROOT / '.github/actions/pr-base-ci/pr_base_ci.py')
base_ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base_ci)


class MetadataEvents(unittest.TestCase):
    def test_edit_and_ready_cannot_start_or_cancel_code_workflows(self):
        for code_path in CODE:
            code = document(code_path)
            for action in ('edited', 'ready_for_review'):
                with self.subTest(workflow=code_path.name, action=action):
                    self.assertFalse(accepts(code, 'pull_request', action))
                    # No run enters the code concurrency group, including when
                    # the existing run is queued rather than already running.
                    for metadata_path in METADATA:
                        metadata = document(metadata_path)
                        self.assertTrue(accepts(metadata, 'pull_request', action))
                        self.assertNotEqual(code['concurrency']['group'], metadata['concurrency']['group'])
                        self.assertTrue(metadata['concurrency']['group'].startswith('pr-metadata-'))
                        self.assertNotIn('pr-metadata-', code['concurrency']['group'])

    def test_new_head_still_enters_the_same_cancellable_code_group(self):
        for path in CODE:
            workflow = document(path)
            with self.subTest(workflow=path.name):
                for action in ('opened', 'synchronize', 'reopened'):
                    self.assertTrue(accepts(workflow, 'pull_request', action))
                self.assertEqual(workflow['concurrency']['cancel-in-progress'],
                                 "${{ github.event_name == 'pull_request' }}")
                self.assertTrue(accepts(workflow, 'merge_group'))
                self.assertTrue(accepts(workflow, 'workflow_dispatch'))
                self.assertEqual(set(workflow[True]['workflow_dispatch']['inputs']) &
                                 {'code-merge', 'code-head', 'code-base'},
                                 {'code-merge', 'code-head', 'code-base'})

    def test_metadata_has_no_code_jobs_or_code_verdict(self):
        for path in METADATA:
            workflow = document(path)
            with self.subTest(workflow=path):
                self.assertTrue(accepts(workflow, 'merge_group'))
                self.assertTrue(accepts(workflow, 'workflow_dispatch'))
                self.assertEqual(set(workflow['jobs']), {'pr-metadata'})
                job = workflow['jobs']['pr-metadata']
                self.assertEqual(job['runs-on'], 'sylphx-linux-control')
                self.assertNotIn('if', job)
                self.assertNotIn('continue-on-error', job)
                for step in job['steps']:
                    self.assertNotIn('run', step)
                    self.assertNotIn('continue-on-error', step)
                    self.assertTrue('actions/checkout@' in step['uses'] or
                                    'actions/contribution-policy' in step['uses'] or
                                    'actions/pr-base-ci' in step['uses'])
                self.assertEqual(workflow['permissions'], {'contents': 'read', 'pull-requests': 'read'})

    def test_draft_and_ready_heads_have_the_same_required_code_evidence(self):
        gate = document(STARTERS / 'optimistic-gate.yml')
        self.assertEqual(gate['jobs']['suite']['if'], "github.event_name != 'merge_group'")
        verdict = gate['jobs']['ci-ok']['steps'][0]['with']
        self.assertEqual(verdict['required-unless-merge-group'], 'suite')
        self.assertIn('suite', gate['jobs']['ci-ok']['needs'])
        fast = document(STARTERS / 'ci-fast-path.yml')
        self.assertEqual(fast['jobs']['heavy']['if'], "needs.changes.outputs.code == 'true'")
        for path in CODE:
            self.assertNotIn('github.event.action', document(path)['jobs']['ci-ok'].get('if', ''))
            for job in document(path)['jobs'].values():
                for step in job.get('steps', []):
                    if 'actions/needs-pass' in step.get('uses', ''):
                        self.assertEqual(step['with']['separate-pr-metadata'], 'true')
        for name in ('ci-fast-path', 'optimistic-gate', 'optimistic-verify'):
            for job in document(STARTERS / (name + '.yml'))['jobs'].values():
                for step in job.get('steps', []):
                    if 'actions/checkout@' in step.get('uses', ''):
                        self.assertEqual(step['with']['ref'], '${{ github.event.inputs.code-merge || github.sha }}')

    def test_bot_dispatch_starts_both_required_producers(self):
        bot = document(STARTERS / 'keel-repin.yml')
        repin = bot['jobs']['repin']['steps'][-1]['with']
        self.assertEqual(repin['ci-workflows'].split(), ['pr-metadata.yml', 'ci.yml'])
        settle = bot['jobs']['settle']['steps'][-1]['with']
        self.assertIn('pr-metadata', settle['required-checks'].split())


class BaseRetargeting(unittest.TestCase):
    def event(self):
        return {'action': 'edited', 'number': 7, 'changes': {'base': {'ref': {'from': 'previous'}}},
                'pull_request': {'state': 'open', 'head': {'sha': 'a' * 40, 'ref': 'change',
                    'repo': {'full_name': 'example/repo'}},
                    'base': {'ref': 'main', 'sha': 'b' * 40}, 'merge_commit_sha': 'c' * 40}}

    def test_title_body_and_ready_never_call_the_code_api(self):
        for action, changes in [('edited', {'title': {'from': 'old'}}),
                                ('edited', {'body': {'from': 'old'}}), ('ready_for_review', {})]:
            event = self.event()
            event.update(action=action, changes=changes)
            api = mock.Mock()
            self.assertFalse(base_ci.dispatch(api, event, 'ci.yml', 'example/repo'))
            api.assert_not_called()

    def test_base_edit_dispatches_code_at_the_current_pr_head(self):
        event = self.event()
        api = mock.Mock(side_effect=[copy.deepcopy(event['pull_request']),
                                     {'object': {'sha': 'a' * 40}},
                                     {'parents': [{'sha': 'b' * 40}, {'sha': 'a' * 40}]}, None])
        self.assertTrue(base_ci.dispatch(api, event, 'ci.yml', 'example/repo'))
        self.assertEqual(api.call_args_list, [mock.call('GET', 'pulls/7'),
            mock.call('GET', 'git/ref/heads/change'),
            mock.call('GET', 'git/commits/' + 'c' * 40),
            mock.call('POST', 'actions/workflows/ci.yml/dispatches', {'ref': 'change', 'inputs': {
                'code-merge': 'c' * 40, 'code-head': 'a' * 40, 'code-base': 'b' * 40}})])

    def test_stale_merge_never_dispatches_code_for_the_wrong_base(self):
        event = self.event()
        api = mock.Mock(side_effect=[copy.deepcopy(event['pull_request']),
            {'object': {'sha': 'a' * 40}}, {'parents': [{'sha': 'd' * 40}, {'sha': 'a' * 40}]}])
        with self.assertRaises(ValueError):
            base_ci.dispatch(api, event, 'ci.yml', 'example/repo')
        self.assertTrue(all(call.args[0] == 'GET' for call in api.call_args_list))

    def test_changed_head_and_fork_never_dispatch_a_different_branch(self):
        event = self.event()
        for field, value in [('sha', 'b' * 40), ('repo', {'full_name': 'example/fork'})]:
            current = copy.deepcopy(event['pull_request'])
            current['head'][field] = value
            api = mock.Mock(return_value=current)
            with self.assertRaises(ValueError):
                base_ci.dispatch(api, event, 'ci.yml', 'example/repo')
            self.assertEqual(api.call_args_list, [mock.call('GET', 'pulls/7')])


if __name__ == '__main__':
    unittest.main()
