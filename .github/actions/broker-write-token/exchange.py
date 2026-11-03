#!/usr/bin/env python3
import json
import os
from pathlib import Path
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '_shared'))
from broker import exchange, ExchangeError, ReadinessError


def main():
    env = os.environ
    try:
        if env.get('READ_ONLY', 'false') not in ('true', 'false'):
            raise ExchangeError('read-only must be true or false')
        result = exchange(env['API_URL'], env['BROKER_POLICY'], env['REPO'],
                          json.loads(env.get('PERMISSIONS', '{}')), env.get('READ_ONLY') == 'true',
                          env.get('BROKER_CI_NAME', ''), env.get('APP_SLUG', ''),
                          expected_issuer=env.get('EXPECTED_ISSUER', ''), expected_connection=env.get('EXPECTED_CONNECTION', ''))
        # Legacy token output remains. This action does not persist provider credentials.
        with open(env['GITHUB_OUTPUT'], 'a') as out:
            for key in ('token', 'app-slug', 'issuer', 'connection', 'expire_time', 'token_exchange'):
                out.write(key.replace('_', '-') + '=' + result[key] + '\n')
            out.write('permissions=' + json.dumps(result['permissions'], separators=(',', ':')) + '\nfailure=\n')
    except (ExchangeError, ReadinessError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        # Only our fixed messages are safe. Parsing/network errors carry no reply bodies.
        reason = str(error) if isinstance(error, (ExchangeError, ReadinessError)) else 'invalid credential exchange configuration or response'
        print('::error::' + reason)
        with open(env['GITHUB_OUTPUT'], 'a') as out:
            out.write('token=\nfailure=' + reason + '\n')
        return 0 if env.get('SOFT_FAIL') == 'true' else 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
