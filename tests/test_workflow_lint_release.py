"""Exercise the composite's real install/lint shell with offline HTTP faults."""
from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / '.github/actions/workflow-lint'
DIGEST = '8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8'


class WorkflowLintReleaseTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        action = yaml.safe_load((ACTION / 'action.yml').read_text())
        self.step = next(step for step in action['runs']['steps'] if 'VERSION' in step.get('env', {}))
        binary = b'#!/bin/bash\nprintf "%s\\n" "$@" > "$LINT_ARGS"\nexit "${LINT_EXIT:-0}"\n'
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode='w:gz') as bundle:
            member = tarfile.TarInfo('actionlint')
            member.size, member.mode = len(binary), 0o700
            bundle.addfile(member, io.BytesIO(binary))
        self.archive = archive.getvalue()
        self.digest = hashlib.sha256(self.archive).hexdigest()
        self.primary, self.fallback, self.body = 504, 200, self.archive
        self.calls = []
        self.token = 'synthetic-workflow-lint-token'
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                owner.calls.append((self.path, dict(self.headers)))
                status = owner.primary if '/releases/download/' in self.path else owner.fallback
                body = owner.body if status == 200 else b'unavailable'
                self.send_response(status)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        # Run the real helper CLI. Only rewrite the HTTP origin, retaining routes,
        # headers, real curl, checksum verification, extraction and lint argv.
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        launcher = bin_dir / 'python3'
        launcher.write_text('#!' + sys.executable + '\n' +
            'import os, runpy, sys\n' +
            'sys.path.insert(0, ' + repr(str(ACTION.parent / '_shared')) + ')\n' +
            'import curl_retry\n' +
            'original = curl_retry.request\n' +
            'def local(url, destination, **kwargs):\n' +
            '    policy = kwargs["policy"]\n' +
            '    kwargs["policy"] = curl_retry.Policy(policy.attempts, 3, 1, 0)\n' +
            '    return original("http://127.0.0.1:' + str(self.server.server_port) +
            '/" + url.split("/", 3)[3], destination, **kwargs)\n' +
            'curl_retry.request = local\n' +
            'sys.argv = sys.argv[1:]\nrunpy.run_path(sys.argv[0], run_name="__main__")\n')
        launcher.chmod(0o700)
        self.env = dict(os.environ, PATH=str(bin_dir) + ':' + os.environ['PATH'],
                        RUNNER_TEMP=str(self.root), GITHUB_ACTION_PATH=str(ACTION),
                        GITHUB_TOKEN=self.token, VERSION='1.7.12', SHA256=self.digest,
                        BINARY_SHA256=self.step['env']['BINARY_SHA256'],
                        IGNORE='property "job_workflow_ref" is not defined',
                        ARGS='-config-file\npath with spaces.yaml',
                        LINT_ARGS=str(self.root / 'lint-args'))

    def run_step(self):
        shell = self.step['run'].replace('/usr/local/bin/actionlint', str(self.root / 'absent-image-binary'))
        result = subprocess.run(['bash', '--noprofile', '--norc', '-eo', 'pipefail', '-c', shell],
                                env=self.env, text=True, capture_output=True, timeout=15)
        self.assertNotIn(self.token, result.stdout + result.stderr)
        return result

    def check_prebaked(self, tool, mismatch=None):
        shutil.rmtree(self.root / 'pinned-tools', ignore_errors=True)
        self.calls.clear()
        name = 'actionlint' if tool == 'workflow-lint' else 'sccache'
        action_path = ACTION.parent / tool
        step = next(step for step in yaml.safe_load((action_path / 'action.yml').read_text())['runs']['steps']
                    if 'VERSION' in step.get('env', {}))
        version = '1.7.12' if name == 'actionlint' else 'sccache 0.18.0'
        version_arg = '-version' if name == 'actionlint' else '--version'
        def executable(output):
            return ('#!/bin/bash\nif [ "$1" = ' + version_arg + ' ]; then echo "' + output +
                    '"; exit; fi\nprintf "%s\\n" "$@" > "$LINT_ARGS"\n').encode()
        good = executable(version)
        candidate_bytes = executable('0.0.0') if mismatch == 'version' else good
        if mismatch == 'digest':
            candidate_bytes += b'# changed bytes, same displayed version\n'
        candidate = self.root / ('image-' + name)
        candidate.write_bytes(candidate_bytes)
        candidate.chmod(0o700)
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode='w:gz') as bundle:
            member = tarfile.TarInfo(name if name == 'actionlint' else
                                     'sccache-v0.18.0-x86_64-unknown-linux-musl/sccache')
            member.size, member.mode = len(good), 0o700
            bundle.addfile(member, io.BytesIO(good))
        self.body = archive.getvalue()
        env = dict(self.env, VERSION=step['env']['VERSION'], SHA256=hashlib.sha256(self.body).hexdigest(),
                   BINARY_SHA256=hashlib.sha256(good if mismatch == 'digest' else candidate_bytes).hexdigest(),
                   GITHUB_ACTION_PATH=str(action_path), GITHUB_PATH=str(self.root / 'path'),
                   GITHUB_ENV=str(self.root / 'env'), SCCACHE_PROBE=str(self.root / 'sccache-probe'))
        self.assertIn('/usr/local/bin/' + name, step['run'])
        shell = step['run'].replace('/usr/local/bin/' + name, str(candidate))
        # Keep the complete installer; backend startup has its own fixture suite.
        shell = shell.replace('. "$GITHUB_ACTION_PATH/start.sh"',
                              '"$dir/sccache" --version > "$SCCACHE_PROBE"')
        result = subprocess.run(['bash', '--noprofile', '--norc', '-eo', 'pipefail', '-c', shell],
                                env=env, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        installed = list((self.root / 'pinned-tools').glob('*/' + name))
        self.assertEqual(len(installed), 1)
        self.assertEqual(installed[0].read_bytes(), good)
        if mismatch is None:
            self.assertEqual(self.calls, [])
            self.assertIn('version-verified pre-baked binary', result.stdout)
        else:
            self.assertTrue(any('/releases/assets/' in path for path, _ in self.calls))
            # A fetched archive is independently pinned, not accepted just because
            # the runner's candidate did not match.
            self.assertIn('authenticated API asset fallback', result.stdout)

    def test_matching_prebaked_tools_are_reused_with_zero_network(self):
        for tool in ('workflow-lint', 'rust-sccache'):
            with self.subTest(tool=tool):
                self.check_prebaked(tool)

    def test_wrong_digest_or_version_uses_the_verified_asset_instead(self):
        for tool in ('workflow-lint', 'rust-sccache'):
            for mismatch in ('digest', 'version'):
                with self.subTest(tool=tool, mismatch=mismatch):
                    self.check_prebaked(tool, mismatch)

    def test_release_identity_and_job_token_are_unchanged(self):
        self.assertEqual(self.step['env']['VERSION'], '1.7.12')
        self.assertEqual(self.step['env']['SHA256'], DIGEST)
        self.assertEqual(self.step['env']['GITHUB_TOKEN'], '${{ github.token }}')
        self.assertIn('--asset-id 384924896', self.step['run'])

    def test_504_falls_back_with_headers_and_runs_lint_with_exact_arguments(self):
        result = self.run_step()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.root / 'lint-args').read_text().splitlines(),
                         ['-shellcheck=', '-pyflakes=', '-ignore',
                          'property "job_workflow_ref" is not defined', '-config-file', 'path with spaces.yaml'])
        api = [(path, headers) for path, headers in self.calls if '/releases/assets/' in path]
        self.assertEqual(len(api), 1)
        self.assertEqual(api[0][0], '/repos/rhysd/actionlint/releases/assets/384924896')
        self.assertEqual(api[0][1]['Authorization'], 'Bearer ' + self.token)
        self.assertEqual(api[0][1]['Accept'], 'application/octet-stream')
        for path, headers in self.calls:
            if '/releases/download/' in path:
                self.assertNotIn('Authorization', headers)

    def test_tampered_download_is_not_extracted_or_run(self):
        self.body += b'tampered'
        result = self.run_step()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('release-checksum-mismatch', result.stdout + result.stderr)
        self.assertFalse((self.root / 'lint-args').exists())
        self.assertFalse(list(self.root.glob('pinned-tools/*/actionlint')))

    def test_both_routes_unavailable_fail_without_running_lint(self):
        self.fallback = 503
        result = self.run_step()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('HTTP 503', result.stdout + result.stderr)
        self.assertFalse((self.root / 'lint-args').exists())

    def test_verified_cache_avoids_network_and_corrupt_cache_fails(self):
        cache = self.root / ('pinned-tools/actionlint-' + DIGEST) / 'a.tgz'
        cache.parent.mkdir(parents=True)
        cache.write_bytes(self.archive)
        self.assertEqual(self.run_step().returncode, 0)
        self.assertEqual(self.calls, [])
        cache.write_bytes(self.archive + b'tampered')
        (self.root / 'lint-args').unlink()
        self.assertNotEqual(self.run_step().returncode, 0)
        self.assertEqual(self.calls, [])
        self.assertFalse((self.root / 'lint-args').exists())

    def test_lint_findings_remain_failures_after_recovered_download(self):
        self.env['LINT_EXIT'] = '7'
        self.assertEqual(self.run_step().returncode, 7)


if __name__ == '__main__':
    unittest.main()