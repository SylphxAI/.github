#!/usr/bin/env python3
"""Find tracked Rust files not reached by any Cargo target (no build/network).

Scans every package manifest, including nested/excluded workspaces. Takes the
union of cfg branches: this checks source wiring, not feature/platform coverage.
Exceptions live in .rust-reachability.json as exact paths with a reason. It is
not a baseline: obsolete code should be removed, not exempted.
"""
from __future__ import annotations

import argparse
import json
import glob
import re
import subprocess
import sys
import time
import tomllib
from pathlib import Path


# Strings are single tokens: examples in documentation/SQL must not create edges.
TOKEN = re.compile(r'''(?P<line>//[^\n]*)|(?P<block>/\*)|(?P<raw>(?:b|c)?r(?P<hashes>\#*)")|(?P<string>(?:b|c)?"(?:\\.|[^"\\])*")|(?P<char>'(?:\\(?:u\{[^}]*\}|x..|.)|[^'\\\n])')|(?P<ident>\b(?:mod|include|path|cfg_attr|concat|env|compile_fail|pass)\b)|(?P<punct>[{}()\[\];#!=,])''', re.S)


def tokens(source: str) -> list[str]:
    result = []
    i = 0
    while i < len(source):
        if result[-1:] == ['mod']:
            name = re.match(r'\s*((?:r#)?[A-Za-z_][A-Za-z_0-9]*)', source[i:])
            if name:
                result.append(name.group(1))
                i += name.end()
                continue
        m = TOKEN.search(source, i)
        if m is None:
            break
        i = m.end()
        kind = m.lastgroup
        if kind in ('space', 'line', 'char'):
            continue
        if kind == 'block':
            depth = 1
            while depth and i < len(source):
                n = re.compile(r'/\*|\*/').search(source, i)
                if not n:
                    i = len(source)
                    break
                depth += 1 if n.group() == '/*' else -1
                i = n.end()
            continue
        if kind == 'raw':
            hashes = m.group('hashes')
            end = source.find('"' + hashes, i)
            if end < 0:
                raise ValueError('unterminated raw string')
            result.append(json.dumps(source[i:end]))
            i = end + 1 + len(hashes)
            continue
        value = m.group()
        if kind != 'ident' or value in {'mod', 'include', 'path', 'cfg_attr', 'concat', 'env', 'compile_fail', 'pass'} or result[-1:] == ['mod']:
            result.append(value)
    return result


def literal(token: str) -> str | None:
    if not token.startswith('"'):
        return None
    try:
        return json.loads(token)
    except ValueError:
        return None


def balanced(ts: list[str], start: int) -> int:
    pairs = {'(': ')', '[': ']', '{': '}'}
    stack = [pairs[ts[start]]]
    i = start + 1
    while i < len(ts) and stack:
        if ts[i] in pairs:
            stack.append(pairs[ts[i]])
        elif ts[i] == stack[-1]:
            stack.pop()
        i += 1
    return i


def include_path(ts: list[str], package: Path) -> str | None:
    """Evaluate literal/concat!/env!(CARGO_MANIFEST_DIR); OUT_DIR is generated."""
    ts = [t for t in ts if t != ',']
    if len(ts) == 1:
        return literal(ts[0])
    if ts[:3] == ['env', '!', '('] and ts[-1:] == [')']:
        return str(package) if ts[3:-1] == ['"CARGO_MANIFEST_DIR"'] else None
    if ts[:3] == ['concat', '!', '('] and ts[-1:] == [')']:
        values = []
        i = 3
        while i < len(ts) - 1:
            if ts[i:i+3] == ['env', '!', '(']:
                end = balanced(ts, i + 2)
                value = include_path(ts[i:end], package)
                i = end
            else:
                value = literal(ts[i])
                i += 1
            if value is None:
                return None
            values.append(value)
        return ''.join(values)
    return None


class Census:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.files = {self.root / p for p in subprocess.check_output(
            ['git', '-C', str(root), 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], text=True).split('\0') if p}
        self.rust = {p for p in self.files if p.suffix == '.rs' and p.is_file()}
        self.reached: set[Path] = set()
        self.visited: set[tuple[Path, Path, Path]] = set()
        self.packages: dict[Path, dict] = {}
        self.targets: set[Path] = set()
        self.errors: list[str] = []
        self.dynamic: set[str] = set()

    def rel(self, path: Path) -> str:
        return str(path.relative_to(self.root))

    def discover(self):
        manifests = {p: tomllib.loads(p.read_text()) for p in self.files if p.name == 'Cargo.toml' and p.is_file()}
        for manifest, data in sorted(manifests.items()):
            if 'package' not in data:
                continue
            package = manifest.parent
            self.packages[package] = data
            settings = data['package']
            edition = settings.get('edition', '2015')
            if isinstance(edition, dict):
                edition = next((manifests[p / 'Cargo.toml'].get('workspace', {}).get('package', {}).get('edition') for p in package.parents if p / 'Cargo.toml' in manifests and 'workspace' in manifests[p / 'Cargo.toml']), '2021')
            auto = str(edition) != '2015' or not any(k in data for k in ('lib', 'bin', 'test', 'bench', 'example'))
            # Explicit targets override automatic targets with the same name.
            lib = data.get('lib', {})
            if 'lib' in data or settings.get('autolib', True):
                self.target(package / lib.get('path', 'src/lib.rs'), package)
            for kind, directory, flag in [('bin', 'src/bin', 'autobins'), ('test', 'tests', 'autotests'), ('bench', 'benches', 'autobenches'), ('example', 'examples', 'autoexamples')]:
                explicit = data.get(kind, [])
                for target in explicit:
                    if 'path' in target:
                        self.target(package / target['path'], package)
                    else:
                        name = target['name']
                        candidates = ([package / 'src/main.rs'] if kind == 'bin' and name == settings['name'] else [])
                        candidates += [package / directory / (name + '.rs'), package / directory / name / 'main.rs']
                        for candidate in candidates:
                            self.target(candidate, package)
                if settings.get(flag, auto):
                    names = {t['name'] for t in explicit}
                    if kind == 'bin' and settings['name'] not in names:
                        self.target(package / 'src/main.rs', package)
                    for pattern in ('*.rs', '*/main.rs'):
                        for candidate in (package / directory).glob(pattern):
                            name = candidate.stem if candidate.parent == package / directory else candidate.parent.name
                            if name not in names:
                                self.target(candidate, package)
            build = settings.get('build', 'build.rs')
            if build is not False:
                self.target(package / ('build.rs' if build is True else build), package)

    def target(self, file: Path, package: Path):
        file = file.resolve()
        if file in self.rust:
            self.targets.add(file)
            self.visit(file, file.parent, package)

    def visit(self, file: Path, directory: Path, package: Path):
        file, directory = file.resolve(), directory.resolve()
        if file not in self.rust:
            return
        key = (file, directory, package)
        if key in self.visited:
            return
        self.visited.add(key)
        self.reached.add(file)
        source = file.read_text()
        if re.search(r'\bmod\b[^;{}]*;|\bmod\b[^;\n{]*/[/*]|\binclude\s*!|\btrybuild\b', source):
            self.scan(tokens(source), file, directory, file.parent, package)

    def scan(self, ts: list[str], file: Path, directory: Path, path_base: Path, package: Path):
        i = 0
        attrs: list[str] = []
        while i < len(ts):
            if ts[i] in ('compile_fail', 'pass') and i + 2 < len(ts) and ts[i+1] == '(' and 'trybuild' in file.read_text():
                pattern = literal(ts[i+2])
                if pattern is not None:
                    for fixture in glob.glob(str(package / pattern)):
                        fixture = Path(fixture)
                        self.visit(fixture, fixture.parent, package)
            if ts[i:i+2] == ['#', '[']:
                end = balanced(ts, i + 1)
                attrs += ts[i+2:end-1]
                i = end
                continue
            if ts[i] == 'mod' and i + 2 < len(ts) and re.fullmatch(r'(?:r#)?[A-Za-z_][A-Za-z_0-9]*', ts[i+1]):
                name = ts[i+1].removeprefix('r#')
                paths = [literal(attrs[j+2]) for j in range(len(attrs)-2) if attrs[j:j+2] == ['path', '=']]
                paths = [p for p in paths if p is not None]
                if ts[i+2] == ';':
                    candidates = [(path_base / p, True) for p in paths]
                    # cfg_attr(path=...) also has the non-attribute branch.
                    if not paths or 'cfg_attr' in attrs:
                        candidates += [(directory / (name + '.rs'), False), (directory / name / 'mod.rs', False)]
                    for candidate, overridden in candidates:
                        candidate = candidate.resolve()
                        child_dir = candidate.parent if overridden or candidate.name == 'mod.rs' else candidate.with_suffix('')
                        self.visit(candidate, child_dir, package)
                    i += 3
                    attrs = []
                    continue
                if ts[i+2] == '{':
                    end = balanced(ts, i+2)
                    dirs = [path_base / p for p in paths]
                    if not paths or 'cfg_attr' in attrs:
                        dirs.append(directory / name)
                    for child in dirs:
                        self.scan(ts[i+3:end-1], file, child, child, package)
                    i = end
                    attrs = []
                    continue
            if ts[i:i+3] == ['include', '!', '(']:
                end = balanced(ts, i+2)
                value = include_path(ts[i+3:end-1], package)
                if value is not None:
                    included = (file.parent / value).resolve()
                    self.visit(included, included.parent, package)
                elif '"OUT_DIR"' not in ts[i+3:end-1]:
                    self.dynamic.add(self.rel(file))
                i = end
                continue
            # Attributes apply to one item, not the next unrelated module.
            if ts[i] in (';', '{', '}'):
                attrs = []
            i += 1

    def result(self):
        owned = {p for p in self.rust if any(parent in self.packages for parent in p.parents)}
        orphans = sorted(owned - self.reached)
        exceptions_file = self.root / '.rust-reachability.json'
        exceptions = json.loads(exceptions_file.read_text()) if exceptions_file.exists() else {}
        if not isinstance(exceptions, dict):
            raise ValueError('.rust-reachability.json must map exact paths to reasons')
        for path, reason in exceptions.items():
            if not isinstance(reason, str) or not reason.strip() or any(c in path for c in '*?['):
                self.errors.append(f'invalid exception: {path!r}; use an exact path and a reason')
            elif self.root / path not in orphans:
                self.errors.append(f'stale exception (missing or reachable): {path}')
        return {
            'files': len(self.rust), 'packages': len(self.packages), 'targets': len(self.targets),
            'reachable': len(self.reached),
            'orphans': [self.rel(p) for p in orphans if self.rel(p) not in exceptions],
            'exceptions': {self.rel(p): exceptions[self.rel(p)] for p in orphans if self.rel(p) in exceptions},
            'outside_crates': sorted(self.rel(p) for p in self.rust - owned - self.reached),
            'dynamic_includes': sorted(self.dynamic), 'errors': self.errors,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', nargs='?', type=Path, default=Path.cwd())
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    start = time.monotonic()
    census = Census(args.root)
    census.discover()
    result = census.result()
    result['seconds'] = round(time.monotonic() - start, 3)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for path in result['orphans']:
            print(f'Unreachable Rust source: {path}', file=sys.stderr)
        for error in result['errors']:
            print(error, file=sys.stderr)
        for path in result['dynamic_includes']:
            print(f'Unresolved include! expression (inspect manually): {path}', file=sys.stderr)
        print(f"Rust reachability: {result['files']} files, {result['packages']} packages, {len(result['orphans'])} unreachable, {len(result['exceptions'])} explicit exceptions ({result['seconds']}s)")
    return bool(result['orphans'] or result['errors'] or result['dynamic_includes'])


if __name__ == '__main__':
    sys.exit(main())
