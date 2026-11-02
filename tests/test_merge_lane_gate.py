"""The required R8 boundary reads exact Git trees, not a caller's checkout."""
import contextlib
import copy
import datetime
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / '.github/actions/workflow-lint'
sys.path.insert(0, str(ACTION))
spec = importlib.util.spec_from_file_location('merge_lane_gate', ACTION / 'merge_lane_gate.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
SHA = 'a' * 40
PLAIN = 'on: merge_group\njobs:\n  build:\n    runs-on: sylphx-linux-standard\n'
GOOD = PLAIN.replace('standard', 'standard-merge')


def response(text=GOOD):
    return {'data': {'repository': {'isPrivate': True, 'commit': {'oid': SHA}, 'object': {'entries': [
        {'name': 'ci.yml', 'type': 'blob', 'mode': 33188,
         'object': {'text': text, 'isTruncated': False, 'isBinary': False}}]}}}}


class TreeRead(unittest.TestCase):
    def api(self, result):
        return types.SimpleNamespace(repository='SylphxAI/example', root='https://api.github.com',
                                     read=Mock(return_value=result))

    def test_exact_commit_tree_and_visibility(self):
        api = self.api(response())
        self.assertEqual(gate.read_workflows(api, SHA), ({'ci.yml': GOOD}, True))
        args = api.read.call_args.args
        self.assertEqual(args[1]['variables'], {'owner': 'SylphxAI', 'name': 'example',
                                               'revision': SHA + ':.github/workflows', 'sha': SHA})
        self.assertIn('isTruncated isBinary', args[1]['query'])

    def test_unreadable_truncated_symlink_or_wrong_revision_fail(self):
        cases = [{'errors': [{'message': 'failure'}]}, {'data': {'repository': None}}]
        for key, value in (('isPrivate', None), ('commit', {'oid': 'b' * 40}), ('object', None)):
            item = response()
            item['data']['repository'][key] = value
            cases.append(item)
        for key, value in (('type', 'tree'), ('mode', 40960), ('mode', None)):
            item = response()
            item['data']['repository']['object']['entries'][0][key] = value
            cases.append(item)
        for key, value in (('text', None), ('isTruncated', True), ('isBinary', True),
                           ('isTruncated', None), ('isBinary', None)):
            item = response()
            item['data']['repository']['object']['entries'][0]['object'][key] = value
            cases.append(item)
        item = response()
        item['data']['repository']['object']['entries'] = []
        cases.append(item)
        for item in cases:
            with self.subTest(item=item), self.assertRaises(ValueError):
                gate.read_workflows(self.api(item), SHA)

    def test_native_merge_revision_not_pr_head(self):
        event = {'pull_request': {'head': {'sha': 'b' * 40}}}
        self.assertEqual(gate.source_sha('pull_request', event, SHA), SHA)
        self.assertEqual(gate.source_sha('merge_group', {'merge_group': {'head_sha': SHA}}, SHA), SHA)
        for event in ({}, {'merge_group': {'head_sha': 'c' * 40}}):
            with self.assertRaises(ValueError):
                gate.source_sha('merge_group', event, SHA)
        for bad in ('main', '', '0' * 40, '../ci.yml'):
            with self.assertRaises(ValueError):
                gate.source_sha('pull_request', {}, bad)


class RequiredVerdict(unittest.TestCase):
    def run_gate(self, *, text=GOOD, event_name='pull_request', event=None, state=False,
                 api_error=False, exemption=None):
        event = ({'merge_group': {'head_sha': SHA}} if event_name == 'merge_group' else {}) if event is None else event
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'event.json'
            path.write_text(json.dumps(event))
            env = {'GITHUB_REPOSITORY': 'SylphxAI/example', 'GITHUB_EVENT_NAME': event_name,
                   'GITHUB_EVENT_PATH': str(path), 'GITHUB_SHA': SHA, 'GITHUB_TOKEN': 'not-a-secret'}
            api = Mock()
            policy = types.SimpleNamespace(GitHub=Mock(return_value=api), dispatch_target=Mock())
            output = io.StringIO()
            with patch.dict(os.environ, env, clear=True), patch.object(gate, 'delivered', return_value=state), patch.object(gate.time, 'sleep'), \
                 patch.object(gate, 'exempt', return_value=exemption), \
                 patch.object(gate, 'contribution', return_value=policy), \
                 patch.object(gate, 'read_workflows', side_effect=RuntimeError('sensitive remote text') if api_error else None,
                              return_value=({'ci.yml': text}, True)) as read, \
                 contextlib.redirect_stdout(output):
                result = gate.main()
            return result, output.getvalue(), read, policy

    def test_plain_blocks_required_verdict_on_pr_and_merge_group(self):
        for event in ('pull_request', 'merge_group'):
            result, output, read, _ = self.run_gate(text=PLAIN, event_name=event)
            self.assertEqual(result, 1)
            self.assertIn('pull-request pool', output)
            self.assertEqual(read.call_args.args[1], SHA)
        self.assertEqual(self.run_gate()[0], 0)

    def test_delivery_and_hands_off_skip_without_source_read(self):
        for state in (True,):
            result, _, read, _ = self.run_gate(state=state)
            self.assertEqual(result, 0)
            read.assert_not_called()
        result, _, read, _ = self.run_gate(state=None)
        self.assertEqual(result, 0)
        read.assert_called_once()
        result, _, read, _ = self.run_gate(exemption={'class': 'hands-off', 'owner': 'Kyle', 'review': '2026-12-01'})
        self.assertEqual(result, 0)
        read.assert_not_called()

    def test_dispatch_validates_code_target_and_uses_merge_not_head(self):
        event = {'inputs': {'code-merge': 'd' * 40, 'code-head': 'e' * 40, 'code-pr': '12'}}
        result, _, read, policy = self.run_gate(event_name='workflow_dispatch', event=event)
        self.assertEqual(result, 0)
        policy.dispatch_target.assert_called_once()
        self.assertEqual(read.call_args.args[1], 'd' * 40)
        result, _, read, _ = self.run_gate(event_name='workflow_dispatch')
        self.assertEqual(result, 0)
        read.assert_not_called()

    def test_api_failure_fails_without_remote_text(self):
        result, output, _, _ = self.run_gate(api_error=True)
        self.assertEqual(result, 1)
        self.assertIn('API/source read failure, not a lane violation', output)
        self.assertNotIn('sensitive remote', output)

    def test_only_explicit_expiring_static_policy_or_hands_off_waive(self):
        entry = {'class': 'static-runner-policy', 'repos': ['SylphxAI/example'],
                 'reason': 'reviewed parser limitation', 'owner': 'Kyle', 'review': '2026-12-01'}
        for cls, expected in (('static-runner-policy', True), ('no-build', False),
                              ('suite-is-gate', False), ('hands-off', True)):
            item = {**entry, 'class': cls}
            with patch.object(gate, 'load_policy', return_value={'exemptions': [item], 'classes': {
                    cls: {'waives': ['*'] if cls == 'hands-off' else ['R8']}}}):
                self.assertEqual(bool(gate.exempt('SylphxAI/example', datetime.date(2026, 11, 2))), expected)
                self.assertIsNone(gate.exempt('SylphxAI/example', datetime.date(2027, 1, 1)))

    def test_property_retries_but_unreadable_property_does_not_bypass_source(self):
        for values, expected_calls in (([None, False], 2), ([None, None, None], 3)):
            with patch.object(gate, 'delivered', side_effect=values) as read, \
                 patch.object(gate.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()):
                self.assertFalse(gate.delivery_state())
                self.assertEqual(read.call_count, expected_calls)
        for reason in ('HTTP 500', 'HTTP 403'):
            for source, expected in ((GOOD, 0), (PLAIN, 1)):
                with self.subTest(reason=reason, source=source):
                    result, output, read, _ = self.run_gate(state=None, text=source)
                    self.assertEqual(result, expected)
                    read.assert_called_once()
                    self.assertIn('delivery property API read unavailable', output)

    def test_real_property_contract_http_faults_are_bounded(self):
        import urllib.error
        from perf_gate import delivered as real_delivered
        for status in (500, 403):
            failure = lambda: urllib.error.HTTPError('https://api.github.com/properties', status, 'unavailable', {}, None)
            for outcomes, calls in (([failure(), []], 2), ([failure(), failure(), failure()], 3)):
                fetch = Mock(side_effect=outcomes)
                with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'SylphxAI/example', 'GITHUB_TOKEN': 'test-token'}, clear=True), \
                     patch.object(gate, 'delivered', side_effect=lambda env: real_delivered(env, fetch)), \
                     patch.object(gate.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()):
                    self.assertFalse(gate.delivery_state())
                self.assertEqual(fetch.call_count, calls)

    def test_source_api_bounded_retry_and_rate_limit_no_retry(self):
        api = TreeRead().api(response())
        api.read.side_effect = [RuntimeError('HTTP 500'), response()]
        with patch.object(gate.time, 'sleep'):
            self.assertEqual(gate.read_workflows(api, SHA), ({'ci.yml': GOOD}, True))
        self.assertEqual(api.read.call_count, 2)
        for error, count in (('HTTP 500', 3), ('rate limit exceeded', 1)):
            api = TreeRead().api(response())
            api.read.side_effect = RuntimeError(error)
            with patch.object(gate.time, 'sleep'), self.assertRaises(RuntimeError):
                gate.read_workflows(api, SHA)
            self.assertEqual(api.read.call_count, count)

    def test_local_git_reads_exact_object_not_modified_worktree(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            def git(*args):
                return subprocess.check_output(['git', '-C', tmp, *args], stderr=subprocess.DEVNULL).decode().strip()
            git('init', '-q')
            folder = Path(tmp) / '.github/workflows'
            folder.mkdir(parents=True)
            path = folder / 'ci.yml'
            path.write_text(PLAIN)
            git('add', '.')
            git('-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '-qm', 'Fixture')
            sha = git('rev-parse', 'HEAD')
            path.write_text(GOOD)
            with patch.dict(os.environ, {'GITHUB_WORKSPACE': tmp}):
                self.assertEqual(gate.local_workflows(sha, {'repository': {'private': True}}), ({'ci.yml': PLAIN}, True))
                self.assertIsNone(gate.local_workflows('f' * 40, {}))

    def test_required_actions_include_guard_before_verdict_without_checkout(self):
        import yaml
        for name in ('ci-ok', 'needs-pass'):
            action = yaml.safe_load((ROOT / f'.github/actions/{name}/action.yml').read_text())
            first = action['runs']['steps'][0]
            self.assertEqual(first['name'], 'Require merge-lane routing')
            self.assertIn('../merge-lane/check.sh', first['run'])
        standalone = (ROOT / '.github/actions/merge-lane/action.yml').read_text()
        script = (ROOT / '.github/actions/merge-lane/check.sh').read_text()
        self.assertNotIn('checkout', standalone.split('runs:', 1)[1])
        self.assertIn('PyYAML==6.0.3', script)
        self.assertIn('--only-binary=:all:', script)


if __name__ == '__main__':
    unittest.main()
