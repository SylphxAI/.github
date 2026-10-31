#!/usr/bin/env python3
"""Read v1 cache manifests and v2 retained manifests with isolated generations."""
import hashlib
import json
import math
import re
from pathlib import Path
import sys

MAGIC = b'ci-run-store-parts-v1\n'
MAGIC_V2 = b'ci-run-store-parts-v2\n'
PART_BYTES = 64 * 1024 * 1024


def manifest(path, maximum, part_bytes):
    with path.open('rb') as source:
        prefix = source.read(len(MAGIC))
        if prefix not in (MAGIC, MAGIC_V2):
            return None
        tail = source.read(4097)
    if len(tail) > 4096:
        raise ValueError('oversized parts manifest')
    value = json.loads(tail)
    fields = {'parts', 'size', 'sha256'} | ({'generation'} if prefix == MAGIC_V2 else set())
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError('invalid parts manifest fields')
    if prefix == MAGIC_V2 and not re.fullmatch(r'[0-9a-f]{32}', str(value['generation'])):
        raise ValueError('invalid part generation')
    if type(value['parts']) is not int or not 1 <= value['parts'] <= min(10000, math.ceil(maximum / part_bytes)):
        raise ValueError('invalid part count')
    if type(value['size']) is not int or not 1 <= value['size'] <= maximum:
        raise ValueError('invalid bundle size')
    digest = value['sha256']
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        raise ValueError('invalid bundle digest')
    return value


def main():
    mode, source_name, directory_name, max_text, part_text = sys.argv[1:6]
    generation = sys.argv[6] if len(sys.argv) == 7 else ''
    if generation and not re.fullmatch(r'[0-9a-f]{32}', generation):
        raise ValueError('invalid part generation')
    maximum, part_bytes = int(max_text), int(part_text)
    if maximum < 1 or not 1 <= part_bytes <= PART_BYTES:
        raise ValueError('invalid upload size limit')
    source, directory = Path(source_name), Path(directory_name)
    if mode == 'split':
        digest = hashlib.sha256()
        size = 0
        parts = 0
        with source.open('rb') as body:
            while True:
                data = body.read(part_bytes)
                if not data:
                    break
                size += len(data)
                if size > maximum or parts >= 10000:
                    raise ValueError('bundle exceeds upload size limit')
                digest.update(data)
                (directory / ('part-' + str(parts))).write_bytes(data)
                parts += 1
        value = {'parts': parts, 'size': size, 'sha256': digest.hexdigest()}
        if generation:
            value['generation'] = generation
        magic = MAGIC_V2 if generation else MAGIC
        (directory / 'manifest').write_bytes(magic + json.dumps(value, separators=(',', ':')).encode() + b'\n')
        (directory / 'part-suffix').write_text('.parts-' + generation + '.part-' if generation else '.part-')
        print(parts)
    elif mode == 'inspect':
        value = manifest(source, maximum, part_bytes)
        if value:
            generation = value.get('generation', '')
            (directory / 'part-suffix').write_text('.parts-' + generation + '.part-' if generation else '.part-')
        print(0 if value is None else value['parts'])
    elif mode == 'join':
        value = manifest(source, maximum, part_bytes)
        if value is None:
            raise ValueError('missing parts manifest')
        destination = directory / 'bundle-joined.tgz'
        try:
            size, digest = 0, hashlib.sha256()
            with destination.open('wb') as target:
                for index in range(value['parts']):
                    with (directory / ('part-' + str(index))).open('rb') as part:
                        part_size = 0
                        while True:
                            data = part.read(1024 * 1024)
                            if not data:
                                break
                            part_size += len(data)
                            size += len(data)
                            if part_size > part_bytes or size > value['size']:
                                raise ValueError('part exceeds declared size')
                            digest.update(data)
                            target.write(data)
            if size != value['size'] or digest.hexdigest() != value['sha256']:
                raise ValueError('bundle size or digest mismatch')
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
    else:
        raise ValueError('invalid parts operation')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, TypeError, KeyError):
        # The untrusted manifest and response bytes are deliberately not echoed.
        print('run-store: invalid, incomplete or corrupt parts bundle', file=sys.stderr)
        raise SystemExit(1)
