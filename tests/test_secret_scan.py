"""Exercise the shared secret gate against real Git history and gitleaks."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCANNER = ROOT / '.github/actions/secret-scan/secret_scan.py'


def load():
    spec = importlib.util.spec_from_file_location('secret_scan', SCANNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SecretScanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.binary = shutil.which('gitleaks')
        if not cls.binary:
            download = tempfile.TemporaryDirectory()
            cls.addClassCleanup(download.cleanup)
            cls.binary = load().install(Path(download.name), load().VERSION)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.com')
        # Construct a nonfunctional fixture at runtime, so the tests themselves
        # do not add a token-shaped literal to the repository.
        self.token = 'gh' + 'p_' + 'aB3dE5fG7hI9jK1mN3pQ5rS7tU9vW1xY3zA5'
        self.line = 'token = "' + self.token + '"'
        self.write('settings.txt', 'plain text\n')
        self.base = self.commit('base')

    def git(self, *args):
        proc = subprocess.run(['git', '-C', str(self.repo), *args], capture_output=True, text=True)
        if proc.returncode:
            self.fail('fixture Git operation failed')
        return proc.stdout.strip()

    def write(self, path, text):
        dest = self.repo / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)

    def commit(self, message):
        self.git('add', '-A')
        self.git('commit', '-qm', message)
        return self.git('rev-parse', 'HEAD')

    def scan(self, base=None, head=None, event='pull_request', fetch=False):
        base = base or self.base
        head = head or self.git('rev-parse', 'HEAD')
        payload = {'pull_request': {'base': {'sha': base}, 'head': {'sha': head}},
                   'merge_group': {'base_sha': base, 'head_sha': head}}
        event_path = self.root / 'event.json'
        event_path.write_text(json.dumps(payload))
        env = dict(os.environ, GITHUB_EVENT_PATH=str(event_path), GITHUB_EVENT_NAME=event)
        args = [sys.executable, str(SCANNER), '--gitleaks', self.binary]
        if fetch:
            args += ['--fetch', '--remote', str(self.repo)]
        else:
            args += ['--repo', str(self.repo)]
        proc = subprocess.run(args, cwd=self.root, env=env, capture_output=True, text=True)
        output = proc.stdout + proc.stderr
        self.assertNotIn(self.token, output, 'the scanner must never print a finding value')
        return proc.returncode, output

    def add_token(self, path='settings.txt'):
        self.write(path, self.line + '\n')
        return self.commit('add fixture')

    def test_added_token_fails_with_location_and_digest(self):
        head = self.add_token()
        code, output = self.scan()
        self.assertEqual(code, 1)
        for part in (head, 'settings.txt', 'line 1', hashlib.sha256(self.line.encode()).hexdigest()):
            self.assertIn(part, output)

    def test_old_history_and_unchanged_context_pass(self):
        base = self.add_token()
        self.write('settings.txt', self.line + '\nnew harmless line\n')
        self.commit('touch adjacent line')
        self.assertEqual(self.scan(base=base)[0], 0)

    def test_intermediate_leak_removed_before_head_still_fails(self):
        leaked = self.add_token()
        self.write('settings.txt', 'plain text\n')
        self.commit('remove fixture')
        code, output = self.scan()
        self.assertEqual(code, 1)
        self.assertIn(leaked, output)

    def test_exact_reasoned_desk_allowlist_passes(self):
        leaked = self.add_token()
        self.write('.desk-secret-allowlist.json', json.dumps({'version': 1, 'findings': [{
            'commit': leaked, 'path': 'settings.txt', 'line': 1,
            'sha256': hashlib.sha256(self.line.encode()).hexdigest(),
            'reason': 'Nonfunctional synthetic token used to test the detector.'}]}))
        self.commit('record reviewed fixture')
        code, output = self.scan()
        self.assertEqual(code, 0, output)
        self.assertIn('reviewed findings allowed=1', output)

    def test_allowlist_does_not_cover_different_path_or_line_or_digest(self):
        leaked = self.add_token()
        for field, value in [('path', 'elsewhere.txt'), ('line', 2), ('sha256', '0' * 64)]:
            entry = {'commit': leaked, 'path': 'settings.txt', 'line': 1,
                     'sha256': hashlib.sha256(self.line.encode()).hexdigest(), 'reason': 'Test fixture.'}
            entry[field] = value
            self.write('.desk-secret-allowlist.json', json.dumps({'version': 1, 'findings': [entry]}))
            self.commit('mismatched review ' + field)
            self.assertEqual(self.scan()[0], 1, field)

    def test_malformed_allowlist_fails_without_echoing_contents(self):
        self.write('.desk-secret-allowlist.json', self.token)
        self.commit('invalid review')
        code, output = self.scan()
        self.assertEqual(code, 1)
        self.assertIn('allowlist', output)

    def test_merge_group_range_and_fetch_without_checkout(self):
        head = self.add_token()
        code, output = self.scan(event='merge_group', fetch=True)
        self.assertEqual(code, 1)
        self.assertIn(head, output)
        self.assertIn(self.base + '..' + head, output)

    def test_missing_merge_group_range_fails_closed(self):
        code, _ = self.scan(base='0' * 40, event='merge_group')
        self.assertEqual(code, 1)

    def test_repository_gitleaks_config_ignore_and_inline_comment_cannot_disable_gate(self):
        self.write('.gitleaks.toml', '[allowlist]\nregexes = [".*"]\n')
        self.write('.gitleaksignore', '*\n')
        self.write('settings.txt', self.line + ' # gitleaks:allow\n')
        self.commit('try repository suppression')
        self.assertEqual(self.scan()[0], 1)

    def resolve_merge(self, path='settings.txt', prefix=''):
        self.git('checkout', '-qb', 'branch')
        self.write(path, 'branch\n')
        self.commit('branch change')
        self.git('checkout', '-q', 'main')
        self.write(path, 'main\n')
        base = self.commit('main change')
        subprocess.run(['git', '-C', str(self.repo), 'merge', '--no-commit', 'branch'], capture_output=True)
        self.write(path, prefix + self.line + '\n')
        merge = self.commit('resolve merge')
        code, output = self.scan(base=base, event='merge_group')
        self.assertEqual(code, 1)
        self.assertIn(merge, output)

    def test_finding_in_merge_resolution_is_scanned(self):
        self.resolve_merge()

    def test_quoted_merge_paths_and_patch_shaped_added_line(self):
        self.resolve_merge('folder/測試 "name".txt', '+ ')

    def test_path_containing_finding_is_withheld(self):
        self.add_token(self.token + '.txt')
        code, output = self.scan()
        self.assertEqual(code, 1)
        self.assertIn('path contains finding', output)

    def test_merge_inherited_old_secret_does_not_fail(self):
        self.git('checkout', '-qb', 'branch')
        self.write('branch.txt', 'branch change\n')
        self.commit('branch change')
        self.git('checkout', '-q', 'main')
        base = self.add_token()
        self.git('checkout', '-q', 'branch')
        self.git('merge', '-q', 'main', '-m', 'merge base')
        self.assertEqual(self.scan(base=base)[0], 0)

    def test_unrelated_events_do_not_fetch_or_install_for_aggregate(self):
        event_path = self.root / 'event.json'
        event_path.write_text('{}')
        proc = subprocess.run([sys.executable, str(SCANNER), '--pre-merge-only', '--fetch'],
                              env=dict(os.environ, GITHUB_EVENT_NAME='push', GITHUB_EVENT_PATH=str(event_path)),
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


class ManifestTest(unittest.TestCase):
    def test_needs_pass_invokes_scan_automatically(self):
        import yaml
        for action in ('needs-pass', 'ci-ok'):
            with self.subTest(action=action):
                manifest = yaml.safe_load((ROOT / f'.github/actions/{action}/action.yml').read_text())
                steps = manifest['runs']['steps']
                scanner = next(step for step in steps if 'secret_scan.py' in step.get('run', ''))
                self.assertIn('--fetch', scanner['run'])
                self.assertIn('--pre-merge-only', scanner['run'])
                self.assertTrue(scanner['env']['SECRET_SCAN_TOKEN'].startswith('${{ '))
