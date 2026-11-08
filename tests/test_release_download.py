"""Exercise the actual composite setup shell with real curl against an HTTP fixture."""
import hashlib
import http.server
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import threading
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
TOKEN = 'synthetic-release-token'


class ReleaseDownloadTests(unittest.TestCase):
    tool = 'sccache'
    expected_api = '/repos/mozilla/sccache/releases/assets/563130201'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.requests = []
        self.primary = 504
        self.api = 200
        self.payload = self.archive()
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                owner.requests.append((self.path, dict(self.headers)))
                status = owner.api if self.path.startswith('/repos/') else owner.primary
                self.send_response(status)
                self.end_headers()
                self.wfile.write(owner.payload if status == 200 else b'unavailable')

            def log_message(self, *_):
                pass

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        fake = self.root / 'bin'
        fake.mkdir()
        # Keep the real curl request/config parser and record argv independently.
        # Only redirect the origin and suppress primary retry waiting in this fixture.
        (fake / 'curl').write_text('''#!/usr/bin/env python3
import json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ['ARGV_LOG'], 'a') as f: f.write(json.dumps(args) + '\\n')
filtered = []
i = 0
while i < len(args):
    if args[i] in ('--retry', '--retry-max-time'):
        i += 2
        continue
    a = args[i]
    for origin in ('https://github.com', 'https://api.github.com'):
        if a.startswith(origin + '/'):
            a = os.environ['FIXTURE_ORIGIN'] + a[len(origin):]
            break
    filtered.append(a)
    i += 1
sys.exit(subprocess.call([os.environ['REAL_CURL'], *filtered]))
''')
        (fake / 'curl').chmod(0o755)
        self.env = {**os.environ, 'PATH': str(fake) + ':' + os.environ['PATH'],
                    'REAL_CURL': shutil.which('curl'), 'ARGV_LOG': str(self.root / 'argv'),
                    'FIXTURE_ORIGIN': f'http://127.0.0.1:{self.server.server_port}',
                    'GITHUB_TOKEN': TOKEN, 'RUNNER_TEMP': str(self.root),
                    'BUILD_CACHE': 'false', 'ACTIONS_CACHE': 'false',
                    'S3_ACCESS_KEY': '', 'S3_SECRET_KEY': '', 'S3_ENDPOINT': ''}
        for key in ('GITHUB_PATH', 'GITHUB_ENV', 'GITHUB_OUTPUT', 'GITHUB_STEP_SUMMARY'):
            self.env[key] = str(self.root / key)

    def archive(self):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode='w:gz') as archive:
            name = ('sccache-v0.18.0-x86_64-unknown-linux-musl/sccache'
                    if self.tool == 'sccache' else 'gitleaks')
            payload = b'#!/bin/sh\necho fixture-tool\n'
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o755
            archive.addfile(info, io.BytesIO(payload))
        return data.getvalue()

    def setup(self, wrong_sha=False):
        """Run the production setup step, overriding only the fixture's digest."""
        action = ROOT / '.github/actions/rust-sccache'
        step = next(s for s in yaml.safe_load((action / 'action.yml').read_text())['runs']['steps']
                    if s.get('id') == 'sccache')
        env = {**self.env, **{k: str(v) for k, v in step['env'].items()
                            if '${{' not in str(v)}, 'GITHUB_ACTION_PATH': str(action)}
        env['SHA256'] = '0' * 64 if wrong_sha else hashlib.sha256(self.payload).hexdigest()
        result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', step['run']],
                                env=env, capture_output=True, text=True, timeout=15)
        self.assertNotIn(TOKEN, result.stdout + result.stderr)
        self.assertNotIn(TOKEN, (self.root / 'argv').read_text())
        return result

    def installed(self):
        paths = Path(self.env['GITHUB_PATH']).read_text().splitlines()
        return Path(paths[-1]) / self.tool

    def test_primary_504_authenticated_api_200(self):
        result = self.setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.installed().is_file())
        self.assertEqual(len(self.requests), 2)
        path, headers = self.requests[-1]
        self.assertEqual(path, self.expected_api)
        self.assertEqual(headers['Authorization'], 'Bearer ' + TOKEN)
        self.assertEqual(headers['Accept'], 'application/octet-stream')
        calls = [json.loads(line) for line in (self.root / 'argv').read_text().splitlines()]
        self.assertNotIn('--retry', calls[-1])

    def test_primary_success_does_not_call_api(self):
        self.primary = 200
        self.env.pop('GITHUB_TOKEN')
        result = self.setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(self.installed().is_file())

    def test_wrong_sha_fails_before_install(self):
        self.assertNotEqual(self.setup(wrong_sha=True).returncode, 0)
        self.assertFalse(Path(self.env['GITHUB_PATH']).exists())
        self.assertFalse(any(p.is_file() for p in self.root.rglob(self.tool)))
        self.assertEqual(len(self.requests), 2)

    def test_missing_token_refuses_api(self):
        self.env.pop('GITHUB_TOKEN')
        result = self.setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('requires GITHUB_TOKEN', result.stderr)
        self.assertEqual(len(self.requests), 1)

    def test_missing_asset_fails_closed(self):
        self.api = 404
        self.assertNotEqual(self.setup().returncode, 0)
        self.assertFalse(Path(self.env['GITHUB_PATH']).exists())
        self.assertFalse(any(p.is_file() for p in self.root.rglob(self.tool)))
        self.assertEqual(len(self.requests), 2)

    def test_primary_wrong_sha_does_not_fallback(self):
        self.primary = 200
        self.assertNotEqual(self.setup(wrong_sha=True).returncode, 0)
        self.assertEqual(len(self.requests), 1)


class SecretScannerDownloadTests(ReleaseDownloadTests):
    tool = 'gitleaks'
    expected_api = '/repos/gitleaks/gitleaks/releases/assets/378332058'
    machine = 'x86_64'

    def setup(self, wrong_sha=False):
        import importlib.util
        from unittest import mock
        spec = importlib.util.spec_from_file_location(
            'release_secret_scan', ROOT / '.github/actions/secret-scan/secret_scan.py')
        scanner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(scanner)
        arch, _, asset_id = scanner.CHECKSUMS[self.machine]
        digest = '0' * 64 if wrong_sha else hashlib.sha256(self.payload).hexdigest()
        scanner.CHECKSUMS[self.machine] = (arch, digest, asset_id)
        # Never inherit the CI credential: these tests use only the synthetic token.
        env = {k: v for k, v in self.env.items() if k != 'SECRET_SCAN_TOKEN'}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(scanner.platform, 'system', return_value='Linux'), \
             mock.patch.object(scanner.platform, 'machine', return_value=self.machine):
            try:
                scanner.install(self.root, scanner.VERSION)
                result = subprocess.CompletedProcess([], 0, '', '')
            except scanner.ScanError as error:
                message = str(error)
                result = subprocess.CompletedProcess([], 1, '', message)
        self.assertNotIn(TOKEN, result.stdout + result.stderr)
        self.assertNotIn(TOKEN, (self.root / 'argv').read_text())
        return result

    def installed(self):
        return self.root / 'gitleaks'


class SecretScannerArmDownloadTests(SecretScannerDownloadTests):
    machine = 'aarch64'
    expected_api = '/repos/gitleaks/gitleaks/releases/assets/378332057'


if __name__ == '__main__':
    unittest.main()
