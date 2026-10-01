"""Required selectors, pure composition, and offline CLI/action consumer contracts."""
import contextlib
import io
import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest

import yaml

ACTION = Path(__file__).resolve().parents[1] / '.github/actions/metadata-sync'
TOOL = runpy.run_path(str(ACTION / 'sync.py'))


def fixture(root):
    (root / 'README.md').write_bytes(b'<!-- sync:lead -->old<!-- /sync:lead -->\r\n<!-- hero -->old<!-- /hero -->\r\n')
    (root / 'package.json').write_text('{"description":"old","nested":{"tags":["old"]}}\n')
    (root / 'index.md').write_text('---\nhero:\n  tagline: old\n---\n')
    plan = {'version': 1, 'files': [
        {'path': 'README.md', 'edits': [
            {'type': 'region', 'name': 'lead', 'value': 'new'},
            {'type': 'region', 'start': '<!-- hero -->', 'end': '<!-- /hero -->', 'value': 'hero'}]},
        {'path': 'package.json', 'edits': [
            {'type': 'json', 'path': ['description'], 'value': 'new'},
            {'type': 'json', 'path': ['nested', 'tags', 0], 'value': 'new'}]},
        {'path': 'index.md', 'edits': [
            {'type': 'regex', 'pattern': '^  tagline: .+$', 'value': '  tagline: "new"'}]}]}
    (root / 'plan.json').write_text(json.dumps(plan))
    return plan


class MetadataSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='metadata consumer ')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.plan = fixture(self.root)

    def cli(self, *args):
        return subprocess.run([sys.executable, '-S', str(ACTION / 'sync.py'),
                               '--plan', str(self.root / 'plan.json'), '--root', str(self.root), *args],
                              capture_output=True, text=True)

    def snapshot(self):
        return {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}

    def test_check_write_determinism_and_idempotence(self):
        before = self.snapshot()
        first, second = self.cli('--check'), self.cli('--check')
        self.assertEqual(first.returncode, 1, first.stderr)
        self.assertEqual(first.stdout, second.stdout)
        self.assertIn('README.md: out of sync', first.stdout)
        self.assertIn('--- README.md', first.stdout)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.cli('--write').returncode, 0)
        output = self.snapshot()
        self.assertIn(b'new<!-- /sync:lead -->\r\n', output['README.md'])
        self.assertIn(b'hero<!-- /hero -->\r\n', output['README.md'])
        self.assertEqual(self.cli('--check').returncode, 0)
        self.assertEqual(self.cli('--write').returncode, 0)
        self.assertEqual(output, self.snapshot())
        # Two independent runs on the same original input produce identical bytes.
        for name, data in before.items():
            (self.root / name).write_bytes(data)
        self.assertEqual(self.cli('--write').returncode, 0)
        self.assertEqual(output, self.snapshot())

    def test_required_diagnostics_validate_whole_plan_before_writes(self):
        cases = [
            ('missing region', {'type': 'region', 'name': 'absent', 'value': 'x'}, 'missing region'),
            ('duplicate region', {'type': 'region', 'start': 'old', 'end': '<!-- /hero -->', 'value': 'x'}, 'duplicate region'),
            ('reversed region', {'type': 'region', 'start': '<!-- /hero -->', 'end': '<!-- hero -->', 'value': 'x'}, 'reversed region'),
            ('regex zero', {'type': 'regex', 'pattern': '^absent$', 'value': 'x'}, 'zero-match field'),
            ('regex duplicate', {'type': 'regex', 'pattern': 'old', 'value': 'x'}, 'duplicate field'),
            ('json zero', {'type': 'json', 'path': ['absent'], 'value': 'x'}, 'zero-match field'),
            ('unknown type', {'type': 'unknown', 'value': 'x'}, 'unknown edit type'),
        ]
        for name, edit, diagnostic in cases:
            with self.subTest(name=name):
                plan = json.loads(json.dumps(self.plan))
                # An earlier file would change if the implementation wrote eagerly.
                path = 'package.json' if edit['type'] == 'json' else 'README.md'
                plan['files'].append({'path': path, 'edits': [edit]})
                # Duplicate-marker fixture must remain duplicated after previous edits.
                if name in ('duplicate region', 'regex duplicate'):
                    edit['pattern' if edit['type'] == 'regex' else 'start'] = '<!--'
                (self.root / 'plan.json').write_text(json.dumps(plan))
                before = self.snapshot()
                for mode in ('--check', '--write'):
                    result = self.cli(mode)
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn(diagnostic, result.stderr)
                    self.assertIn('edit 1', result.stderr)
                    self.assertEqual(before, self.snapshot())

    def test_each_region_fence_and_output_encoding_fail_closed(self):
        patch = TOOL['patch']
        edit = {'type': 'region', 'name': 'x', 'value': 'new'}
        for source, diagnostic in (
            ('<!-- sync:x -->old', 'missing region'),
            ('old<!-- /sync:x -->', 'missing region'),
            ('<!-- sync:x --><!-- sync:x --><!-- /sync:x -->', 'duplicate region'),
            ('<!-- sync:x --><!-- /sync:x --><!-- /sync:x -->', 'duplicate region'),
            ('<!-- /sync:x --><!-- sync:x -->', 'reversed region'),
        ):
            with self.assertRaisesRegex(TOOL['PlanError'], diagnostic):
                patch(source, edit)
        with self.assertRaisesRegex(TOOL['PlanError'], 'replacement contains'):
            patch('<!-- sync:x --><!-- /sync:x -->', dict(edit, value='<!-- sync:x -->'))
        self.plan['files'].append({'path': 'index.md', 'edits': [{'type': 'file', 'value': '\ud800'}]})
        before = self.snapshot()
        with self.assertRaises(UnicodeEncodeError):
            TOOL['synchronize'](self.plan, self.root, 'write')
        self.assertEqual(before, self.snapshot())

    def test_order_across_repeated_entries_and_literal_regex_values(self):
        sources = {'x': 'first'}
        plan = {'version': 1, 'files': [
            {'path': 'x', 'edits': [{'type': 'regex', 'pattern': 'first', 'value': 'second'}]},
            {'path': 'x', 'edits': [{'type': 'regex', 'pattern': 'second', 'value': r'\1 $value'}]}]}
        expected = {'x': r'\1 $value'}
        self.assertEqual(TOOL['render_plan'](plan, sources), expected)
        self.assertEqual(TOOL['render_plan'](plan, sources), expected)
        self.assertEqual(sources, {'x': 'first'})

    def test_invalid_plan_paths_files_and_fields_are_read_only(self):
        for mutate in (
            lambda p: p.update(version=2),
            lambda p: p.update(files=[]),
            lambda p: p['files'][0].update(path='../escape'),
            lambda p: p['files'][0].update(path='/escape'),
            lambda p: p['files'][0].update(path='missing'),
            lambda p: p['files'][0].update(edits=[]),
            lambda p: p['files'][0]['edits'][0].update(typo=True),
            lambda p: p['files'][1]['edits'][1].update(path=['nested', 'tags', -1]),
            lambda p: p['files'][1]['edits'][1].update(path=['nested', 'missing', 0]),
            lambda p: p['files'][1]['edits'][1].update(path=['nested', 'tags', True]),
            lambda p: p['files'][2]['edits'][0].update(pattern='['),
            lambda p: p['files'][2]['edits'][0].update(pattern='^'),
        ):
            plan = json.loads(json.dumps(self.plan))
            mutate(plan)
            (self.root / 'plan.json').write_text(json.dumps(plan))
            before = self.snapshot()
            result = self.cli('--write')
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertNotIn('Traceback', result.stderr)
            self.assertEqual(before, self.snapshot())

    def test_file_edit_requires_existing_target(self):
        plan = {'version': 1, 'files': [{'path': 'README.md', 'edits': [{'type': 'file', 'value': '<svg/>\n'}]}]}
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(TOOL['synchronize'](plan, self.root, 'write'), 0)
        self.assertEqual((self.root / 'README.md').read_bytes(), b'<svg/>\n')
        (self.root / 'README.md').unlink()
        with self.assertRaises(OSError):
            TOOL['synchronize'](plan, self.root, 'write')

    def test_symlink_escape_and_duplicate_alias_rejected(self):
        with tempfile.TemporaryDirectory() as outside:
            (self.root / 'escape').symlink_to(Path(outside) / 'target')
            plan = {'version': 1, 'files': [{'path': 'escape', 'edits': [{'type': 'file', 'value': 'x'}]}]}
            with self.assertRaisesRegex(TOOL['PlanError'], 'escapes root'):
                TOOL['synchronize'](plan, self.root, 'write')
        (self.root / 'alias').symlink_to(self.root / 'README.md')
        self.plan['files'].append({'path': 'alias', 'edits': [{'type': 'file', 'value': 'x'}]})
        with self.assertRaisesRegex(TOOL['PlanError'], 'duplicate target alias'):
            TOOL['synchronize'](self.plan, self.root, 'write')

    def test_mode_is_explicit(self):
        self.assertEqual(self.cli().returncode, 2)
        self.assertEqual(self.cli('--check', '--write').returncode, 2)

    def test_composite_consumer_modes(self):
        action = yaml.safe_load((ACTION / 'action.yml').read_text())
        step = action['runs']['steps'][0]
        self.assertEqual(action['inputs']['mode']['default'], 'check')
        import os
        env = dict(os.environ, GITHUB_ACTION_PATH=str(ACTION), SYNC_ROOT=str(self.root),
                   SYNC_PLAN=str(self.root / 'plan.json'))
        for mode, expected in (('check', 1), ('invalid', 2), ('write', 0), ('check', 0)):
            result = subprocess.run(['bash', '-c', step['run']], env=dict(env, SYNC_MODE=mode),
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, expected, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
