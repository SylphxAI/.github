#!/usr/bin/env python3
"""Configure source-only Git authority; nothing secret in URL/config/logs."""
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '_shared'))
from broker import exchange, access, validate, ExchangeError, ReadinessError

PERMISSIONS = {'contents': 'read', 'metadata': 'read'}


def targets(repositories, policies):
    repos = [v.strip() for v in re.split(r'[,\n]', repositories) if v.strip()]
    if not repos or len(set(v.lower() for v in repos)) != len(repos) or not isinstance(policies, dict):
        raise ExchangeError('unique owner/name source repositories required')
    if set(repos) != set(policies):
        raise ExchangeError('trust-policies must match the exact requested repository set')
    for repo in repos:
        validate('https://api.example', policies[repo], repo, PERMISSIONS, True)
    return repos


def configure(env):
    policies = json.loads(env['TRUST_POLICIES'])
    repos = targets(env['REPOSITORIES'], policies)
    for repo in repos:
        validate(env['API_URL'], policies[repo], repo, PERMISSIONS, True)
    if not env.get('BROKER_CI_NAME'):
        raise ExchangeError('ci-name is required')
    count = env.get('GIT_CONFIG_COUNT', '0')
    if not re.fullmatch('[0-9]+', count):
        raise ExchangeError('invalid existing Git configuration count')
    # Validate every target before the first issuance; never retry a failed mint.
    key = access(env['API_URL'], env['BROKER_CI_NAME'])
    grants = {}
    for repo in repos:
        result = exchange(env['API_URL'], policies[repo], repo, PERMISSIONS, True, key=key,
                          expected_issuer=env.get('EXPECTED_ISSUER', ''), expected_connection=env.get('EXPECTED_CONNECTION', ''))
        grants[repo.lower()] = result['token']
    root = Path(tempfile.mkdtemp(prefix='broker-git-', dir=env['RUNNER_TEMP']))
    try:
        root.chmod(0o700)
        credentials = root / 'credentials.tsv'
        credentials.touch(mode=0o600)
        credentials.write_text(''.join(repo + '\t' + token + '\n' for repo, token in grants.items()))
        global_config = root / 'global'
        global_config.touch(mode=0o600)
        safe = subprocess.run(['git', 'config', '--global', '--get-all', 'safe.directory'], text=True, capture_output=True, env=env)
        for directory in safe.stdout.splitlines():
            if directory:
                subprocess.run(['git', 'config', '--file', str(global_config), '--add', 'safe.directory', directory], check=True, env=env)
        # Empty helpers reset inherited command/local lists. Extraheaders from checkout
        # must also be cleared. A fresh global config drops old insteadOf rewrites.
        helper = '!sh ' + shlex.quote(str(Path(__file__).with_name('credential.sh'))) + ' ' + shlex.quote(str(credentials))
        inherited = subprocess.run(['git', 'config', '--name-only', '--get-regexp',
                                    r'^(credential(\..*)?\.helper|http(\..*)?\.extraheader)$'],
                                   capture_output=True, text=True, check=False, env=env)
        # Also reset path-specific helpers/headers: a generic reset does not override
        # a more-specific stale checkout credential. Never copy their values.
        reset = sorted({name for name in inherited.stdout.splitlines()
                        if name.lower().startswith('credential.') or name.lower() == 'http.extraheader'
                        or re.fullmatch(r'http\.https?://github\.com(?::[0-9]+)?(?:/.*)?\.extraheader', name, re.IGNORECASE)}
                       | {'credential.helper', 'credential.https://github.com.helper',
                          'http.https://github.com/.extraheader', 'http.extraheader'})
        config = [(name, '') for name in reset]
        config += [('credential.https://github.com.helper', helper), ('credential.useHttpPath', 'true')]
        with open(env['GITHUB_ENV'], 'a') as out:
            for offset, (name, value) in enumerate(config, int(count)):
                out.write(f'GIT_CONFIG_KEY_{offset}={name}\nGIT_CONFIG_VALUE_{offset}={value}\n')
            out.write(f'GIT_CONFIG_COUNT={int(count) + len(config)}\nGIT_CONFIG_GLOBAL={global_config}\nGIT_CONFIG_NOSYSTEM=1\n')
            out.write(f'CARGO_GIT_CREDENTIALS_FILE={credentials}\nCARGO_GIT_CREDENTIAL_HELPER={Path(__file__).with_name("credential.sh")}\nCARGO_NET_GIT_FETCH_WITH_CLI=true\nGIT_TERMINAL_PROMPT=0\n')
        with open(env['GITHUB_OUTPUT'], 'a') as out:
            out.write(f'credentials-file={credentials}\n')
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise


if __name__ == '__main__':
    try:
        configure(dict(os.environ))
    except (ExchangeError, ReadinessError, ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError):
        print('::error::source credential setup failed; no fallback or retried exchange', file=sys.stderr)
        raise SystemExit(1)
