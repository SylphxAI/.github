"""Exercise the shared secret gate against real Git history and gitleaks."""
from __future__ import annotations

import base64
import hashlib
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

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

    def scan(self, base=None, head=None, event='pull_request', fetch=False, config=None):
        base = base or self.base
        head = head or self.git('rev-parse', 'HEAD')
        payload = {'pull_request': {'base': {'sha': base}, 'head': {'sha': head}},
                   'merge_group': {'base_sha': base, 'head_sha': head}}
        event_path = self.root / 'event.json'
        event_path.write_text(json.dumps(payload))
        env = dict(os.environ, GITHUB_EVENT_PATH=str(event_path), GITHUB_EVENT_NAME=event)
        args = [sys.executable, str(SCANNER), '--gitleaks', self.binary]
        if config is not None:
            args += ['--config', str(config)]
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

    def test_prose_identifiers_and_placeholders_pass_without_an_allowlist(self):
        sha = hashlib.sha1(b'public commit fixture').hexdigest()
        lines = [
            'etcd heartbeat tuning',
            'a capability row of prose',
            '| etcd heartbeat tuning | Operators may adjust intervals. |',
            '| Secret scanning | a capability row of prose |',
            'The token endpoint reuses the existing job token, without a second checkout.',
            'env.update(SYLPHX_API_KEY=api_key_value, '
            'SYLPHX_NO_KEYCHAIN="1", SYLPHX_NO_BROWSER="1")',
            'ACCESS_KEY_NAME = etcd_heartbeat_tuning',
            'TOKEN_ENDPOINT = internal-credential-exchange',
            'credential_provider = workload_identity',
            'revision: ' + sha,
            'key: docs/architecture/credential-rotation.md',
            'token: <REDACTED>',
            'password: changeme',
            'secret: example',
            'token: your-token-here',
            'secret: ***',
            'export API_TOKEN=$TOKEN',
            'export API_TOKEN=' + chr(36) + '{SECRET}',
            'api_key: ' + chr(36) + '{{ secrets.RELEASE_API_KEY }}',
            'postgres://user:<REDACTED>@db.example.com/db',
            'redis://:your-password-here@cache.example.com/0',
            'https://user:changeme@example.com',
            'https://example.com/users/alice@example.com',
            'https://example.com?q=author:alice@example.com',
            'The new reconcile_pending_deployments step runs on the queue.',
            'Use the create-passkey-session endpoint for the handshake.',
        ]
        self.write('docs/architecture.md', '\n'.join(lines) + '\n')
        self.commit('write operational notes')
        code, output = self.scan()
        self.assertEqual(code, 0, output)
        self.assertNotIn('reviewed findings allowed=', output)

    def test_structured_secrets_and_high_entropy_assignments_refuse_in_markdown(self):
        # Generate fake, deterministic values only inside temporary Git fixtures.
        def sample(label, alphabet, length):
            octets = hashlib.shake_256(label.encode()).digest(length)
            return ''.join(alphabet[value % len(alphabet)] for value in octets)

        letters = 'abcdefghijklmnopqrstuvwxyz'
        alnum = letters + letters.upper() + '0123456789'
        hex_chars = '0123456789abcdef'
        upper = letters.upper() + '234567'
        value = sample('password', alnum, 24)
        def jwt_segment(raw):
            return base64.urlsafe_b64encode(raw).decode().rstrip('=')

        jwt = '.'.join(map(jwt_segment, (b'{"alg":"HS256","typ":"JWT"}',
                                         b'{"sub":"fixture","exp":1111111111}',
                                         hashlib.sha256(b'not-a-jwt-signature').digest())))
        hooks = sample('webhook', alnum, 68)
        cases = {
            **{prefix: prefix + sample(prefix, alnum, 24) for prefix in ('sk_live_', 'sk_test_', 'rk_live_')},
            **{prefix: prefix + sample(prefix, alnum, 36) for prefix in ('ghp_', 'gho_', 'ghu_', 'ghs_', 'ghr_')},
            'fine-grained PAT': 'github_' + 'pat_' + sample('fgpat', alnum, 82),
            **{prefix: prefix + sample(prefix, upper, 16) for prefix in ('AKIA', 'ASIA')},
            '1Password': 'ops_' + jwt_segment(b'{"fixture":"' + sample('ops', alnum, 215).encode() + b'"}'),
            'Slack bot': 'xoxb-' + '13579246802-24681357902-' + sample('slack-bot', alnum, 24),
            'Slack user': 'xoxp-' + '13579246802-24681357902-35792468012-' + sample('slack-user', alnum, 32),
            'Slack app': 'xapp-' + '1-A13BCD42-12345678901-' + sample('slack-app', alnum, 64),
            'Resend': 're_' + sample('resend', alnum, 8) + '_' + sample('secret', alnum, 24),
            'Supabase': 'sbp_' + sample('supabase', hex_chars, 40),
            'Google API': 'AIza' + sample('google', alnum, 35),
            'Google OAuth': 'ya29.' + sample('oauth', alnum, 55),
            'JWT': jwt,
            'Telegram': '1375429131:' + sample('telegram', alnum, 35),
            'Discord webhook': 'https://discord.com/api/webhooks/' + '723456789098765432/' + hooks,
            'Slack webhook': 'https://hooks.slack.com/services/' + 'T' + hooks[:10] + '/B' + hooks[10:20] + '/' + hooks[20:44],
            'CF token': 'CLOUDFLARE_' + 'API_TOKEN' + '=' + sample('cloudflare', alnum, 40),
            'CF global key': 'CLOUDFLARE_' + 'API_KEY' + '=' + sample('cloudflare', hex_chars, 37),
            'Cloudflare origin CA': 'v1.0-' + sample('origin', hex_chars, 24) + '-' + sample('cf', hex_chars, 146),
            'Backblaze B2': 'B2_APPLICATION_KEY=' + sample('b2', alnum, 31),
            'PEM header': '-----BEGIN ' + 'OPENSSH PRIVATE KEY-----',
            'Postgres DSN': 'postgres://reader:' + value + '@db.example.com/warehouse',
            'Redis DSN': 'redis://:' + value + '@cache.example.com/0',
            'HTTPS userinfo': 'https://build:' + value + '@packages.example.com',
            'HTTPS percent encoding': 'https://build:' + value + '%40%21@packages.example.com',
            'JSON secret assignment': '{"clientSecret": "' + value + '"}',
            'YAML token assignment': 'api-token: ' + value,
            'shell password assignment': 'export SERVICE_PASSWORD=' + value,
            'lowercase API token assignment': 'api_key = "' + sample('lowercase', letters, 32) + '"',
            'prose with real token': 'The etcd heartbeat tuning procedure used ' + self.token,
        }
        lines = list(cases.values())
        self.write('docs/architecture.md', '\n'.join(lines) + '\n')
        self.commit('add synthetic credential corpus')
        code, output = self.scan()
        self.assertEqual(code, 1, output)
        for number, (label, line) in enumerate(cases.items(), 1):
            with self.subTest(format=label):
                digest = hashlib.sha256(line.encode()).hexdigest()
                self.assertIn(f'line {number} sha256 {digest}', output)
                self.assertNotIn(line, output)

    def test_explicit_standalone_config_remains_an_override(self):
        config = self.root / 'custom.toml'
        config.write_text('[[rules]]\n' 'id = "fixture"\n' 'regex = "standalone-config-fixture"\n')
        self.add_token()
        self.assertEqual(self.scan(config=config)[0], 0)
        line = 'standalone-config-fixture'
        self.write('custom.txt', line + '\n')
        self.commit('exercise caller-specific rule')
        code, output = self.scan(config=config)
        self.assertEqual(code, 1, output)
        self.assertIn(hashlib.sha256(line.encode()).hexdigest(), output)

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

    def test_content_allowlist_survives_rebase_amend_and_line_shift(self):
        self.write('.desk-secret-allowlist.json', json.dumps({'version': 1, 'findings': [{
            'path': 'settings.txt', 'sha256': hashlib.sha256(self.line.encode()).hexdigest(),
            'reason': 'Generated descriptor line, not a credential.'}]}))
        self.add_token()
        self.assertEqual(self.scan()[0], 0)
        # Amending changes every commit identity; a shifted line keeps its content.
        self.write('settings.txt', 'header\n' + self.line + '\n')
        self.git('add', '-A')
        self.git('commit', '-q', '--amend', '-m', 'amended fixture')
        code, output = self.scan()
        self.assertEqual(code, 0, output)
        self.assertIn('reviewed findings allowed=1', output)

    def test_allowlist_does_not_cover_different_path_or_digest(self):
        leaked = self.add_token()
        for field, value in [('path', 'elsewhere.txt'), ('sha256', '0' * 64)]:
            entry = {'commit': leaked, 'path': 'settings.txt', 'line': 1,
                     'sha256': hashlib.sha256(self.line.encode()).hexdigest(), 'reason': 'Test fixture.'}
            entry[field] = value
            self.write('.desk-secret-allowlist.json', json.dumps({'version': 1, 'findings': [entry]}))
            self.commit('mismatched review ' + field)
            self.assertEqual(self.scan()[0], 1, field)

    def test_allowlist_schema_refuses_missing_reason_or_unknown_field(self):
        self.add_token()
        digest = hashlib.sha256(self.line.encode()).hexdigest()
        for entry in ({'path': 'settings.txt', 'sha256': digest},
                      {'path': 'settings.txt', 'sha256': digest, 'reason': ' '},
                      {'path': 'settings.txt', 'sha256': digest, 'reason': 'x', 'scope': 'all'},
                      {'path': 'settings.txt', 'sha256': digest, 'reason': 'x', 'commit': 'main'}):
            with self.subTest(entry=entry):
                self.write('.desk-secret-allowlist.json', json.dumps({'version': 1, 'findings': [entry]}))
                self.commit('invalid review')
                code, output = self.scan()
                self.assertEqual(code, 1)
                self.assertIn('allowlist', output)

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

    def test_merge_group_single_parent_squash_preserves_reviewed_pr_allowlist(self):
        self.git('checkout', '-qb', 'pr-branch')
        leaked = self.add_token()
        digest = hashlib.sha256(self.line.encode()).hexdigest()
        self.write('.desk-secret-allowlist.json', json.dumps({'version': 1, 'findings': [{
            'commit': leaked, 'path': 'settings.txt', 'line': 1,
            'sha256': digest,
            'reason': 'Reviewed synthetic fixture before squash.'}]}))
        self.commit('record reviewed fixture')
        self.write('settings.txt', 'header 1\nheader 2\nheader 3\n' + self.line + '\n')
        self.commit('shift fixture line later in PR')
        self.assertEqual(self.scan(event='pull_request')[0], 0)

        self.git('checkout', '-q', 'main')
        self.git('merge', '--squash', '-q', 'pr-branch')
        squash_head = self.commit('squash merge group candidate')
        code, output = self.scan(base=self.base, head=squash_head, event='merge_group', fetch=True)
        self.assertEqual(code, 0, output)
        self.assertIn('reviewed findings allowed=1', output)

        # A different, unreviewed secret line at the same path in the squash still fails.
        self.git('reset', '--hard', '-q', self.base)
        self.write('.desk-secret-allowlist.json', json.dumps({'version': 1, 'findings': [{
            'path': 'settings.txt', 'sha256': digest,
            'reason': 'Reviewed synthetic fixture before squash.'}]}))
        self.write('settings.txt', self.line + '\n' + self.line.replace(self.token, self.token[::-1]) + '\n')
        other_squash = self.commit('squash with a different unreviewed line')
        self.assertEqual(self.scan(base=self.base, head=other_squash, event='merge_group')[0], 1)

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

    def test_pr_code_dispatch_runs_the_real_secret_gate_on_its_bound_range(self):
        self.write('change.txt', 'ordinary change\n')
        self.commit('clean change')
        for bad in (False, True):
            if bad:
                self.add_token()
            head = self.git('rev-parse', 'HEAD')
            inputs = {'code-merge': 'c' * 40, 'code-base': self.base, 'code-head': head,
                      'code-pr': '7', 'code-base-ref': 'main'}
            event_path = self.root / 'dispatch.json'
            event_path.write_text(json.dumps({'inputs': inputs}))
            responses = [{'parents': [{'sha': self.base}, {'sha': head}]},
                         {'number': 7, 'state': 'open', 'head': {'sha': head},
                          'base': {'sha': self.base, 'ref': 'main'}}]
            module = load()
            output = io.StringIO()
            with self.subTest(bad=bad), mock.patch.dict(os.environ,
                    GITHUB_EVENT_NAME='workflow_dispatch', GITHUB_EVENT_PATH=str(event_path),
                    GITHUB_SHA=head, GITHUB_REPOSITORY='example/repo', SECRET_SCAN_TOKEN='fixture'), \
                    mock.patch.object(sys, 'argv', ['secret_scan', '--pre-merge-only', '--repo',
                                                  str(self.repo), '--gitleaks', self.binary]), \
                    mock.patch.object(module.urllib.request, 'urlopen', side_effect=[
                        io.BytesIO(json.dumps(response).encode()) for response in responses]), \
                    contextlib.redirect_stdout(output):
                self.assertEqual(module.main(), int(bad))
            self.assertNotIn(self.token, output.getvalue())
            self.assertNotIn('no pre-merge range', output.getvalue())

    def test_partial_fetch_bulk_hydrates_all_parent_deltas_before_real_detection(self):
        self.git('config', 'uploadpack.allowFilter', 'true')
        self.write('.desk-secret-allowlist.json', json.dumps({'version': 1, 'findings': []}))
        self.base = self.commit('unchanged reviewed policy in scan base')
        for index in range(12):
            self.write(f'objects/{index}.txt', f'changed object {index}\n')
        self.write('objects/raw\nname\t.txt', 'path delimiters are not records\n')
        header_name = ':100644 100644 ' + 'a' * 40 + ' ' + 'b' * 40 + ' M'
        self.write(header_name, 'a filename is not a raw diff header\n')
        (self.repo / 'objects/binary.part').write_bytes(b'\0fixture\xff' * 131072)
        self.write('snapshots/archive.part-001', self.line + '\n')
        leaked = self.commit('add bodies and intermediate fixture')
        self.write('snapshots/archive.part-001', 'fixture removed\n')
        head = self.commit('remove intermediate fixture')
        module = load()
        bare = self.root / 'filtered.git'
        bare.mkdir()
        env = module.fetch(bare, str(self.repo), self.base, head, '')
        directory = self.root / 'scan'
        directory.mkdir()
        output = io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(output):
            blobs = module.changed_blobs(bare, self.base, head)
            for path in ('.desk-secret-allowlist.json', 'objects/binary.part'):
                self.assertIn(self.git('rev-parse', head + ':' + path), blobs)
            self.assertGreater(len(module.missing_blobs(bare, blobs, self.base, head)), 10)
            with mock.patch.object(module, 'command', wraps=module.command) as commands:
                self.assertEqual(module.scan(bare, self.base, head, self.binary, directory), 1)
            fetches = [call for call in commands.call_args_list
                       if 'fetch' in call.args[0] and '--stdin' in call.args[0]]
            self.assertEqual(len(fetches), 1, 'all required bodies must use one explicit fetch')
            self.assertEqual(fetches[0].kwargs['timeout'], module.OBJECT_FETCH_TIMEOUT)
            self.assertFalse(module.missing_blobs(bare, blobs, self.base, head))
        self.assertIn(leaked, output.getvalue())
        self.assertIn('phase changed-blob-fetch', output.getvalue())
        self.assertIn('phase detector', output.getvalue())
        self.assertNotIn(self.token, output.getvalue())

    def test_changed_blob_inventory_includes_root_and_both_merge_parents(self):
        module = load()
        root_blob = self.git('rev-parse', self.base + ':settings.txt')
        self.assertIn(root_blob, module.changed_blobs(self.repo, '', self.base))
        self.git('checkout', '-qb', 'other')
        self.write('settings.txt', 'other parent\n')
        other = self.commit('other parent')
        self.git('checkout', '-q', 'main')
        self.write('settings.txt', 'first parent\n')
        first = self.commit('first parent')
        subprocess.run(['git', '-C', str(self.repo), 'merge', '--no-commit', 'other'], capture_output=True)
        self.write('settings.txt', 'resolution\n')
        merged = self.commit('merge resolution')
        actual = module.changed_blobs(self.repo, first, merged)
        for commit in (first, other, merged):
            self.assertIn(self.git('rev-parse', commit + ':settings.txt'), actual)

    def test_detector_timeout_reports_its_phase_without_raw_diagnostics(self):
        self.write('settings.txt', 'ordinary change\n')
        head = self.commit('clean change')
        module = load()
        run = subprocess.run
        def timeout_detector(args, **kwargs):
            if args[0] == self.binary:
                self.assertEqual(kwargs['timeout'], module.DETECTOR_TIMEOUT)
                self.assertEqual(kwargs['env']['GIT_NO_LAZY_FETCH'], '1')
                raise subprocess.TimeoutExpired([self.token], kwargs['timeout'],
                                                output=self.token, stderr=self.token)
            return run(args, **kwargs)
        output = io.StringIO()
        with mock.patch.object(module.subprocess, 'run', side_effect=timeout_detector), \
                contextlib.redirect_stdout(output), \
                self.assertRaisesRegex(module.ScanError, 'detector timed out after 600s') as error:
            module.scan(self.repo, self.base, head, self.binary, self.root)
        self.assertIn('phase detector', output.getvalue())
        self.assertNotIn(self.token, output.getvalue() + str(error.exception))

    def test_bulk_object_timeout_fails_closed_without_diagnostics(self):
        module = load()
        oid = 'a' * 40
        def timeout_fetch(args, **kwargs):
            if 'config' in args:
                return subprocess.CompletedProcess(args, 0, b'remote.origin.promisor true\n', b'')
            self.assertIn('--stdin', args)
            self.assertEqual(kwargs['timeout'], module.OBJECT_FETCH_TIMEOUT)
            self.assertEqual(kwargs['env']['GIT_NO_LAZY_FETCH'], '1')
            raise subprocess.TimeoutExpired([self.token], kwargs['timeout'],
                                            output=self.token, stderr=self.token)
        output = io.StringIO()
        with mock.patch.object(module, 'changed_blobs', return_value={oid}), \
                mock.patch.object(module, 'missing_blobs', return_value={oid}), \
                mock.patch.object(module.subprocess, 'run', side_effect=timeout_fetch), \
                contextlib.redirect_stdout(output), \
                self.assertRaisesRegex(module.ScanError, 'Git command timed out after 600s') as error:
            module.hydrate_changed_blobs(self.repo, self.base, self.base)
        self.assertIn('phase changed-blob-fetch', output.getvalue())
        self.assertNotIn(self.token, output.getvalue() + str(error.exception))

    def test_incomplete_bulk_fetch_and_invalid_inventory_fail_closed(self):
        module = load()
        oid = 'a' * 40
        with mock.patch.object(module, 'git', return_value=b''), \
                mock.patch.object(module, 'command', return_value=b'invalid object\n'), \
                self.assertRaisesRegex(module.ScanError, 'inventory is invalid'):
            module.missing_blobs(self.repo, {oid}, self.base, self.base)
        with mock.patch.object(module, 'git', return_value=b''), \
                mock.patch.object(module, 'command', return_value=(self.base + '\n').encode()), \
                self.assertRaisesRegex(module.ScanError, 'absent from range inventory'):
            module.missing_blobs(self.repo, {oid}, self.base, self.base)
        with mock.patch.object(module, 'changed_blobs', return_value={oid}), \
                mock.patch.object(module, 'missing_blobs', return_value={oid}), \
                mock.patch.object(module, 'command', return_value=b'remote.origin.promisor true\n'), \
                contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaisesRegex(module.ScanError, 'remain missing'):
            module.hydrate_changed_blobs(self.repo, self.base, self.base)


class ManifestTest(unittest.TestCase):
    def test_standalone_scan_binds_the_job_token_for_authenticated_fallback(self):
        import yaml
        manifest = yaml.safe_load((ROOT / '.github/actions/secret-scan/action.yml').read_text())
        scanner = next(step for step in manifest['runs']['steps'] if 'secret_scan.py' in step.get('run', ''))
        self.assertEqual(scanner['env']['GITHUB_TOKEN'], chr(36) + '{{ github.token }}')

    def test_pr_code_dispatch_must_validate_its_target_before_scanning(self):
        module = load()
        policy = mock.Mock()
        policy.PolicyError = ValueError
        spec = mock.Mock()
        def validated(api, event, sha):
            self.assertEqual(sha, 'a' * 40)
            return {}, 'b' * 40, 'a' * 40
        policy.dispatch_target.side_effect = validated
        event = {'inputs': {'code-merge': 'c' * 40, 'code-head': 'a' * 40,
                            'code-base': 'b' * 40, 'code-pr': '7', 'code-base-ref': 'main'}}
        with mock.patch.object(module.importlib.util, 'spec_from_file_location', return_value=spec), \
                mock.patch.object(module.importlib.util, 'module_from_spec', return_value=policy), \
                mock.patch.dict(os.environ, SECRET_SCAN_TOKEN='fixture', GITHUB_REPOSITORY='example/repo'):
            self.assertEqual(module.revisions('workflow_dispatch', event, 'a' * 40),
                             ('b' * 40, 'a' * 40))
            policy.dispatch_target.side_effect = ValueError('changed target')
            with self.assertRaises(module.ScanError):
                module.revisions('workflow_dispatch', event, 'a' * 40)

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
