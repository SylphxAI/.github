#!/usr/bin/env python3
"""Reuse runner tool bytes only after a private copy matches both pinned identities."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


def copy_verified(candidate, destination, *, sha256, version, version_args):
    """Return false without publishing on absence, hash/version mismatch or probe error."""
    if not re.fullmatch(r'[0-9a-f]{64}', sha256) or not version:
        return False
    destination = Path(destination)
    try:
        # Preserve the basename: some tools change behavior when invoked under
        # another name. Probe the exact private bytes that will be published.
        with tempfile.TemporaryDirectory(dir=destination.parent) as directory:
            temporary = Path(directory) / destination.name
            with open(candidate, 'rb') as source, temporary.open('wb') as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
            with temporary.open('rb') as source:
                digest = hashlib.sha256()
                for chunk in iter(lambda: source.read(1024 * 1024), b''):
                    digest.update(chunk)
                actual = digest.hexdigest()
            if actual != sha256:
                return False
            temporary.chmod(0o700)
            result = subprocess.run([str(temporary), *version_args], capture_output=True,
                                    text=True, timeout=5, check=False)
            lines = result.stdout.splitlines()
            if result.returncode or not lines or lines[0] != version:
                return False
            os.replace(temporary, destination)
            return True
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('candidate', 'destination', 'sha256', 'version', 'version-arg'):
        parser.add_argument('--' + name, required=True)
    args = vars(parser.parse_args())
    args['version_args'] = (args.pop('version_arg'),)
    if not copy_verified(**args):
        return 1
    print('Reusing checksum- and version-verified pre-baked binary.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
