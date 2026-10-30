"""Effective contribution metadata permissions through the existing source graph."""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('metadata_pins', ROOT / 'scripts/action_pin_drift_check.py')
pins = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pins)
SHA = 'a' * 40
ACTION = '.github/actions/contribution-policy/action.yml'
USE = 'SylphxAI/.github/.github/actions/contribution-policy@' + SHA


class Source:
    def __init__(self, files=None):
        self.files = {ACTION: 'runs:\n  using: composite\n  steps:\n    - run: python3 contribution_policy.py\n',
                      **(files or {})}

    def evidence(self, sha, head):
        return {'ancestor': sha == SHA, 'files': set(self.files)}

    def read_text(self, sha, path):
        return self.files[path]


def workflow(permissions='', job_permissions='', use=USE):
    return permissions + 'jobs:\n  verdict:\n' + job_permissions + '    steps:\n      - uses: ' + use + '\n'


class MetadataPermissions(unittest.TestCase):
    def findings(self, text, files=None, source=None):
        return pins.metadata_permission_errors({'.github/workflows/ci.yml': text, **(files or {})},
                                               source or Source(), SHA)

    def test_missing_caller_read_refuses_and_read_or_stronger_grants_pass(self):
        for permissions, accepted in [
                ('', False), ('permissions:\n  contents: read\n', False),
                ('permissions:\n  pull-requests: none\n', False),
                ('permissions:\n  contents: read # pull-requests: read\n', False),
                ('permissions:\n  contents: read\n  # pull-requests: read\n', False),
                ('permissions: {contents: read} # pull-requests: read\n', False),
                ('permissions:\n  "pull-requests": "read" # actual scope\n', True),
                ("permissions: {'contents': 'read', 'pull-requests': 'write'} # actual map\n", True),
                ('permissions: # required metadata read\n  contents: read\n  pull-requests: read\n', True),
                ('permissions: {contents: read, pull-requests: write}\n', True),
                ('permissions: read-all\n', True), ('permissions: write-all\n', True)]:
            with self.subTest(permissions=permissions):
                findings = self.findings(workflow(permissions))
                self.assertEqual(not findings, accepted)
                if not accepted:
                    self.assertEqual(findings[0]['why'], 'caller workflow lacks pull-requests: read')

    def test_actual_job_map_overrides_workflow_default_without_changing_other_scopes(self):
        top = 'permissions:\n  contents: read\n  pull-requests: read\n  id-token: write\n'
        self.assertTrue(self.findings(workflow(top, '    permissions:\n      contents: read\n')))
        job = '    permissions:\n      contents: write\n      pull-requests: write\n      id-token: write\n      checks: write\n'
        self.assertEqual(self.findings(workflow('permissions:\n  contents: read\n', job)), [])

    def test_local_reusable_checks_both_caller_ceiling_and_callee_job(self):
        for caller_read, callee_read, accepted in [(False, True, False), (True, False, False), (True, True, True)]:
            with self.subTest(caller=caller_read, callee=callee_read):
                caller = 'permissions:\n  contents: read\n' + ('  pull-requests: read\n' if caller_read else '')
                caller += 'jobs:\n  verify:\n    uses: ./.github/workflows/verify.yml\n'
                job = '    permissions:\n      contents: read\n' + ('      pull-requests: read\n' if callee_read else '')
                callee = 'on: workflow_call\n' + workflow('', job)
                findings = self.findings(caller, {'.github/workflows/verify.yml': callee})
                self.assertEqual(not findings, accepted)
                if not accepted:
                    self.assertTrue(any('verify.yml' in entry for entry in findings[0]['dependency_chain']))

    def test_shared_reusable_and_caller_owned_composite_propagate_permissions(self):
        shared = '.github/workflows/verify.yml'
        source = Source({shared: 'on: workflow_call\n' + workflow('permissions:\n  pull-requests: read\n')})
        for grant in ('none', 'read', 'write'):
            with self.subTest(grant=grant):
                caller = ('permissions:\n  pull-requests: ' + grant + '\njobs:\n  verify:\n'
                          '    uses: SylphxAI/.github/' + shared + '@' + SHA + '\n')
                self.assertEqual(not self.findings(caller, source=source), grant != 'none')
        wrapper = '.github/actions/wrapper/action.yml'
        files = {wrapper: 'runs:\n  using: composite\n  steps:\n    - uses: ' + USE + '\n'}
        self.assertTrue(self.findings(workflow(use='./.github/actions/wrapper'), files))
        self.assertEqual(self.findings(workflow('permissions:\n  pull-requests: read\n', use='./.github/actions/wrapper'), files), [])

    def test_reusable_inherits_an_existing_grant_and_unrelated_job_needs_no_grant(self):
        caller = ('permissions:\n  pull-requests: read\njobs:\n  verify:\n'
                  '    uses: ./.github/workflows/verify.yml\n  build:\n'
                  '    permissions:\n      contents: read\n    steps:\n      - run: echo build\n')
        callee = 'on: workflow_call\n' + workflow()
        self.assertEqual(self.findings(caller, {'.github/workflows/verify.yml': callee}), [])


if __name__ == '__main__':
    unittest.main()
