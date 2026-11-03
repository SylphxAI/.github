"""Local independent-repository source credentials, executed through real Git."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('broker_source_configure', ROOT / '.github/actions/broker-git-credentials/configure.py')
configure = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(configure)
REPOS = ['Example/cloud', 'Example/identity']
POLICIES = {repo: 'orgs/example/trust_policies/source-' + repo.split('/')[1] for repo in REPOS}


class SourceCredentialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = {**os.environ, 'BROKER_CI_NAME': 'quality', 'REPOSITORIES': ','.join(REPOS),
                    'TRUST_POLICIES': json.dumps(POLICIES), 'API_URL': 'https://api.example',
                    'RUNNER_TEMP': str(self.root), 'GITHUB_ENV': str(self.root / 'env'),
                    'GITHUB_OUTPUT': str(self.root / 'out'), 'HOME': str(self.root),
                    'GIT_CONFIG_GLOBAL': str(self.root / 'inherited-global'), 'GIT_CONFIG_NOSYSTEM': '1',
                    'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'advice.detachedHead', 'GIT_CONFIG_VALUE_0': 'false'}
        self.git(['init', '-q', str(self.root / 'repo')])
        self.directory = self.root / 'repo'
        self.git(['config', '--global', 'safe.directory', str(self.directory)])
        self.git(['config', '--global', 'url.https://stale-secret@github.com/.insteadOf', 'https://github.com/'])
        # More-specific stale helpers were a witnessed host-only credential leak
        # class; generic helper resets alone do not clear these scopes.
        self.git(['-C', str(self.directory), 'config', 'credential.https://github.com/Example/unknown.helper', '!echo username=stale; echo password=stale-secret'])
        self.git(['-C', str(self.directory), 'config', 'http.https://github.com/Example/cloud.extraheader', 'Authorization: stale-secret'])

    def git(self, args, **kwargs):
        return subprocess.run(['git', *args], env=self.env, text=True, capture_output=True, **kwargs)

    def setup_credentials(self, fail=None):
        calls = []
        def exchange(api, policy, repository, permissions, read_only, **kwargs):
            calls.append((repository, policy, permissions, read_only))
            if fail == repository:
                raise configure.ExchangeError('refused')
            return {'token': 'fake-' + repository.split('/')[1]}
        with patch.object(configure, 'access', return_value='fake-access') as access, patch.object(configure, 'exchange', side_effect=exchange):
            previous = Path.cwd()
            try:
                os.chdir(self.directory)
                configure.configure(self.env)
            finally:
                os.chdir(previous)
            access.assert_called_once_with('https://api.example', 'quality')
        for line in (self.root / 'env').read_text().splitlines():
            key, value = line.split('=', 1)
            self.env[key] = value
        return calls

    def fill(self, url):
        return self.git(['-C', str(self.directory), 'credential', 'fill'], input='url=' + url + '\n\n')

    def test_independent_exact_grants_and_path_isolation_in_real_git(self):
        calls = self.setup_credentials()
        self.assertEqual(calls, [(repo, POLICIES[repo], {'contents': 'read', 'metadata': 'read'}, True) for repo in REPOS])
        for path, token in [('Example/cloud', 'fake-cloud'), ('Example/identity.git', 'fake-identity'), ('example/CLOUD', 'fake-cloud')]:
            result = self.fill('https://github.com/' + path)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('password=' + token, result.stdout)
            other = 'fake-cloud' if token == 'fake-identity' else 'fake-identity'
            self.assertNotIn(other, result.stdout)
        for url in ['https://github.com', 'https://github.com/Example/unknown', 'https://github.com/Foreign/cloud',
                    'https://github.com/Example/cloud/extra', 'https://evil.example/Example/cloud', 'http://github.com/Example/cloud']:
            result = self.fill(url)
            self.assertNotEqual(result.returncode, 0, url)
            self.assertNotIn('password=', result.stdout)
            self.assertNotIn('stale-secret', result.stdout + result.stderr)

    def test_job_local_private_files_config_preservation_and_cleanup(self):
        self.setup_credentials()
        credentials = Path(self.env['CARGO_GIT_CREDENTIALS_FILE'])
        self.assertEqual(credentials.stat().st_mode & 0o777, 0o600)
        self.assertEqual(credentials.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(credentials.read_text(), 'example/cloud\tfake-cloud\nexample/identity\tfake-identity\n')
        self.assertEqual(self.env['GIT_CONFIG_KEY_0'], 'advice.detachedHead')
        self.assertEqual(self.env['GIT_CONFIG_VALUE_0'], 'false')
        self.assertEqual(self.env['CARGO_NET_GIT_FETCH_WITH_CLI'], 'true')
        self.assertTrue(Path(self.env['CARGO_GIT_CREDENTIAL_HELPER']).is_file())
        self.assertNotIn('fake-cloud', (self.root / 'env').read_text() + (self.root / 'out').read_text())
        global_config = Path(self.env['GIT_CONFIG_GLOBAL']).read_text()
        self.assertIn('safe', global_config)
        self.assertNotIn('stale-secret', global_config)
        effective_headers = self.git(['-C', str(self.directory), 'config', '--get-all', 'http.https://github.com/Example/cloud.extraheader'])
        self.assertTrue(effective_headers.stdout.endswith('\n\n'))  # Last value resets checkout header.
        credentials.unlink()
        result = self.fill('https://github.com/Example/cloud')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('password=', result.stdout)

    def test_existing_non_github_source_cache_authority_survives(self):
        self.env.update(GIT_CONFIG_COUNT='3',
                        GIT_CONFIG_KEY_1='url.https://cache.example/source/.insteadOf',
                        GIT_CONFIG_VALUE_1='https://github.com/Example/cloud',
                        GIT_CONFIG_KEY_2='http.https://cache.example/source/.extraheader',
                        GIT_CONFIG_VALUE_2='Authorization: cache-session')
        self.setup_credentials()
        self.assertEqual(self.env['GIT_CONFIG_KEY_1'], 'url.https://cache.example/source/.insteadOf')
        self.assertEqual(self.env['GIT_CONFIG_VALUE_1'], 'https://github.com/Example/cloud')
        headers = self.git(['-C', str(self.directory), 'config', '--get-all', 'http.https://cache.example/source/.extraheader'])
        self.assertEqual(headers.stdout, 'Authorization: cache-session\n')
        self.assertNotIn('http.https://cache.example', (self.root / 'env').read_text())

    def test_invalid_target_map_never_issues(self):
        for policies, repositories in [({}, ','.join(REPOS)), ({**POLICIES, 'Foreign/extra': 'orgs/example/trust_policies/extra'}, ','.join(REPOS)),
                                       (POLICIES, 'Example/cloud,example/CLOUD'), ({REPOS[0]: None}, REPOS[0])]:
            with self.subTest(policies=policies, repositories=repositories), patch.object(configure, 'access') as access:
                self.env.update(TRUST_POLICIES=json.dumps(policies), REPOSITORIES=repositories)
                with self.assertRaises((configure.ExchangeError, TypeError)):
                    configure.configure(self.env)
                access.assert_not_called()

    def test_setup_failure_removes_already_issued_credentials(self):
        self.env['GITHUB_ENV'] = str(self.root / 'missing' / 'env')
        with self.assertRaises(OSError):
            self.setup_credentials()
        self.assertFalse(list(self.root.glob('broker-git-*')))

    def test_partial_exchange_failure_has_no_fallback_or_credential_file(self):
        with self.assertRaises(configure.ExchangeError):
            self.setup_credentials(fail=REPOS[1])
        self.assertFalse(list(self.root.glob('broker-git-*')))
        self.assertFalse((self.root / 'env').exists())


if __name__ == '__main__':
    unittest.main()
