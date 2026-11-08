#!/usr/bin/env python3
"""Download checksum-pinned public release assets with an authenticated API fallback."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import sys
import time

import curl_retry


_MAX_ARCHIVE_BYTES = 64 * 1024 * 1024


class ReleaseError(Exception):
    """Code-owned reasons only; never retain raw responses or exception text."""

    def __init__(self, reason, status='000', exit_code=0):
        self.reason = reason
        self.status = status if re.fullmatch(r'[0-9]{3}', status) else '000'
        super().__init__(f'release download reason={reason} HTTP {self.status} curl_exit={exit_code}')


def download(destination, *, repository, tag, filename, asset_id, sha256, token, budget=120):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository) or \
            not re.fullmatch(r'[A-Za-z0-9_./+-]+', tag) or '..' in tag or tag.startswith('/') or tag.endswith('/') or \
            not re.fullmatch(r'[A-Za-z0-9_.+-]+', filename) or \
            type(asset_id) is not int or asset_id <= 0 or not re.fullmatch(r'[0-9a-f]{64}', sha256):
        raise ReleaseError('invalid-release-identity')
    destination = Path(destination)
    # An Actions/local archive cache is useful only after re-verifying its bytes.
    if destination.is_file() and hashlib.sha256(destination.read_bytes()).hexdigest() == sha256:
        print('Release archive: checksum-verified cache hit.', flush=True)
        return destination
    deadline = time.monotonic() + budget
    part = destination.with_name(destination.name + '.part')

    def request(url, label, *, primary=False):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ReleaseError('download-timeout')
        headers = () if primary else ('Accept: application/octet-stream', 'Authorization: Bearer ' + token)
        return curl_retry.request(
            url, part, label=label, headers=headers, curl_args=curl_retry.HTTP_REDIRECTS,
            max_bytes=_MAX_ARCHIVE_BYTES,
            policy=curl_retry.Policy(attempts=2 if primary else 5,
                                     budget=min(30, remaining) if primary else remaining,
                                     attempt_time=10 if primary else 30, base_delay=1))

    try:
        result = request(f'https://github.com/{repository}/releases/download/{tag}/{filename}',
                         'release-primary', primary=True)
        if result.returncode or not result.code.startswith('2'):
            transient = result.code not in {'401', '403', '404'} and (
                result.returncode in curl_retry.TRANSIENT if result.returncode else
                result.code == '429' or result.code.startswith('5'))
            if transient:
                if not token:
                    raise ReleaseError('fallback-token-missing', result.code, result.returncode)
                print('Release archive: authenticated API asset fallback.', flush=True)
                # Asset IDs are pinned with the release/hash, not discovered from mutable metadata.
                result = request(f'https://api.github.com/repos/{repository}/releases/assets/{asset_id}',
                                 'release-api-asset')
            if result.returncode or not result.code.startswith('2'):
                reason = ('download-timeout' if result.returncode == 28 else
                          'download-network-error' if result.returncode else 'download-http-error')
                raise ReleaseError(reason, result.code, result.returncode)
        if hashlib.sha256(part.read_bytes()).hexdigest() != sha256:
            raise ReleaseError('release-checksum-mismatch', result.code)
        part.replace(destination)
        return destination
    finally:
        part.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('repository', 'tag', 'filename', 'sha256', 'destination'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--asset-id', type=int, required=True)
    args = vars(parser.parse_args())
    try:
        download(**args, token=os.environ.get('GITHUB_TOKEN', ''))
        return 0
    except ReleaseError as error:
        print(f'::error::stage=release-download class=ReleaseError {error}')
    except OSError:
        print('::error::stage=release-download class=OSError reason=local-io-error')
    return 1


if __name__ == '__main__':
    sys.exit(main())
