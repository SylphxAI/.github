"""Event routing preserves code evidence across changes to PR metadata."""
from pathlib import Path
import unittest

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

    def test_metadata_has_no_code_jobs_or_code_verdict(self):
        for path in METADATA:
            workflow = document(path)
            with self.subTest(workflow=path):
                self.assertTrue(accepts(workflow, 'merge_group'))
                self.assertEqual(set(workflow['jobs']), {'pr-metadata'})
                job = workflow['jobs']['pr-metadata']
                self.assertEqual(job['runs-on'], 'sylphx-linux-control')
                self.assertNotIn('if', job)
                self.assertNotIn('continue-on-error', job)
                for step in job['steps']:
                    self.assertNotIn('run', step)
                    self.assertNotIn('continue-on-error', step)
                    self.assertTrue('actions/checkout@' in step['uses'] or
                                    'actions/contribution-policy' in step['uses'])
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


if __name__ == '__main__':
    unittest.main()
