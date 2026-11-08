"""Release faults through real curl, a local HTTP server and the scanner entrypoint."""
from __future__ import annotations

import contextlib
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('release_scan', ROOT / '.github/actions/secret-scan/secret_scan.py')
SCAN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SCAN)
RELEASE = SCAN.release_download


class ReleaseDownloadTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        self.marker = self.root / 'executed'
        self.token = 'synthetic-bearer-do-not-log'
        binary = b'#!/bin/sh\nprev=""; for arg; do if [ "$prev" = --report-path ]; then printf "[]" > "$arg"; fi; prev="$arg"; done\ntouch "$EXECUTION_MARKER"\n'
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode='w:gz') as bundle:
            member = tarfile.TarInfo('gitleaks')
            member.size = len(binary)
            member.mode = 0o700
            bundle.addfile(member, io.BytesIO(binary))
        self.archive = archive.getvalue()
        self.digest = hashlib.sha256(self.archive).hexdigest()
        self.primary, self.fallback, self.body = 504, 200, self.archive
        self.calls = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                owner.calls.append((self.path, dict(self.headers)))
                primary = '/releases/download/' in self.path
                status = owner.primary if primary else owner.fallback
                if not primary and (self.headers.get('Authorization') != 'Bearer ' + owner.token or
                                    self.headers.get('Accept') != 'application/octet-stream'):
                    status = 403
                body = owner.body if status == 200 else owner.token.encode()
                self.send_response(status)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        git = lambda *args: subprocess.check_output(['git', '-C', str(self.repo), *args], stderr=subprocess.DEVNULL).decode().strip()
        git('init', '-q', '-b', 'main')
        git('config', 'user.name', 'Test')
        git('config', 'user.email', 'test@example.com')
        git('commit', '-qm', 'base', '--allow-empty')
        base = git('rev-parse', 'HEAD')
        git('commit', '-qm', 'head', '--allow-empty')
        head = git('rev-parse', 'HEAD')
        self.event = self.root / 'event.json'
        self.event.write_text(json.dumps({'pull_request': {'base': {'sha': base}, 'head': {'sha': head}}}))
        original = RELEASE.curl_retry.request

        def local_request(url, destination, **kwargs):
            # No external network: rewrite only the host, retaining exact routes/headers.
            path = url.split('/', 3)[3]
            policy = kwargs['policy']
            kwargs['policy'] = RELEASE.curl_retry.Policy(policy.attempts, min(policy.budget, 3),
                                                       min(policy.attempt_time, 1), 0)
            return original(f'http://127.0.0.1:{self.server.server_port}/' + path, destination, **kwargs)

        self.request_patch = mock.patch.object(RELEASE.curl_retry, 'request', side_effect=local_request)
        self.request_patch.start()
        self.addCleanup(self.request_patch.stop)

    def scan(self, token_environment="SECRET_SCAN_TOKEN"):
        output = io.StringIO()
        with mock.patch.object(sys, 'argv', ['scan', '--repo', str(self.repo)]), \
                mock.patch.dict(os.environ, GITHUB_EVENT_PATH=str(self.event), GITHUB_EVENT_NAME='pull_request',
                                EXECUTION_MARKER=str(self.marker),
                                **{"SECRET_SCAN_TOKEN": "", "GITHUB_TOKEN": "", token_environment: self.token}), \
                mock.patch.object(SCAN.platform, 'system', return_value='Linux'), \
                mock.patch.object(SCAN.platform, 'machine', return_value='x86_64'), \
                mock.patch.dict(SCAN.CHECKSUMS, {'x86_64': ('x64', self.digest)}), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = SCAN.main()
        self.assertNotIn(self.token, output.getvalue())
        self.assertNotIn('Secret scan could not complete; raw diagnostics withheld', output.getvalue())
        return code, output.getvalue()

    def test_primary_504_api_200_runs_scan_and_records_fallback(self):
        code, output = self.scan()
        self.assertEqual(code, 0, output)
        self.assertTrue(self.marker.is_file())
        self.assertIn('authenticated API asset fallback', output)
        self.assertIn('Secret scan passed', output)
        self.assertTrue(any(path == '/repos/gitleaks/gitleaks/releases/assets/378332058' for path, _ in self.calls))

    def test_fallback_carries_bearer_and_octet_stream_but_primary_does_not(self):
        self.assertEqual(self.scan()[0], 0)
        for path, headers in self.calls:
            if '/releases/assets/' in path:
                self.assertEqual(headers['Authorization'], 'Bearer ' + self.token)
                self.assertEqual(headers['Accept'], 'application/octet-stream')
            else:
                self.assertNotIn('Authorization', headers)

    def test_job_github_token_alone_authenticates_asset_fallback(self):
        code, output = self.scan("GITHUB_TOKEN")
        self.assertEqual(code, 0, output)
        self.assertTrue(self.marker.is_file())
        self.assertTrue(any('/releases/assets/' in path and headers.get('Authorization') == 'Bearer ' + self.token
                            for path, headers in self.calls))

    def test_both_routes_fail_with_stage_http_status_and_class(self):
        self.fallback = 503
        code, output = self.scan()
        self.assertEqual(code, 1)
        for part in ('stage=gitleaks-install', 'class=ScanError', 'reason=download-http-error', 'HTTP 503'):
            self.assertIn(part, output)
        self.assertFalse(self.marker.exists())

    def test_tampered_fallback_fails_checksum_without_executing_binary(self):
        self.body = self.archive + b'tampered'
        code, output = self.scan()
        self.assertEqual(code, 1)
        self.assertIn('reason=release-checksum-mismatch', output)
        self.assertFalse(self.marker.exists())

    def test_permanent_primary_refusal_never_falls_back(self):
        self.primary = 403
        code, output = self.scan()
        self.assertEqual(code, 1)
        self.assertIn('HTTP 403', output)
        self.assertFalse(any('/releases/assets/' in path for path, _ in self.calls))

    def test_primary_success_does_not_need_fallback(self):
        self.primary = 200
        self.assertEqual(self.scan()[0], 0)
        self.assertEqual(len(self.calls), 1)

    def test_cache_is_verified_and_corrupt_cache_is_not_trusted(self):
        path = self.root / 'cached.tgz'
        args = dict(repository='gitleaks/gitleaks', tag='v8.30.1', filename='test.tgz', asset_id=378332058,
                    sha256=self.digest, token=self.token)
        path.write_bytes(self.archive)
        RELEASE.download(path, **args)
        self.assertEqual(self.calls, [])
        path.write_bytes(b'corrupt')
        RELEASE.download(path, **args)
        self.assertEqual(path.read_bytes(), self.archive)
        self.assertGreater(len(self.calls), 0)

    def test_token_never_enters_spawned_arguments(self):
        with mock.patch.object(subprocess, 'Popen', wraps=subprocess.Popen) as processes:
            self.assertEqual(self.scan()[0], 0)
        self.assertGreater(len(processes.call_args_list), 0)
        for call in processes.call_args_list:
            self.assertNotIn(self.token, repr(call.args))

    def test_fallback_uses_remaining_primary_budget(self):
        clock = [0]
        budgets = []
        def refused(url, destination, **kwargs):
            budgets.append(kwargs['policy'].budget)
            clock[0] += 30
            return RELEASE.curl_retry.Result('504', 0)
        with mock.patch.object(RELEASE.time, 'monotonic', side_effect=lambda: clock[0]), \
                mock.patch.object(RELEASE.curl_retry, 'request', side_effect=refused), \
                self.assertRaises(RELEASE.ReleaseError):
            RELEASE.download(self.root / 'bounded', repository='gitleaks/gitleaks', tag='v8.30.1',
                             filename='test.tgz', asset_id=378332058, sha256=self.digest,
                             token=self.token, budget=45)
        self.assertEqual(budgets, [30, 15])

    def test_unknown_exception_is_typed_without_exception_text(self):
        with mock.patch.object(SCAN, 'install', side_effect=RuntimeError(self.token)):
            code, output = self.scan()
        self.assertEqual(code, 1)
        self.assertIn('stage=gitleaks-install', output)
        self.assertIn('reason=unexpected-error', output)


if __name__ == '__main__':
    unittest.main()
