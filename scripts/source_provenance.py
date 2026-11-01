"""Trusted source-relocation roots, shared by static guards.

The registry belongs to the pinned shared checker, never the caller checkout.
An initial import must match its independently recorded Git tree including modes.
After landing, the normal comparison base is authority for subsequent deltas.
No fetch, source execution, live operation or caller-controlled exemption occurs.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import subprocess

POLICY = pathlib.Path(__file__).resolve().parents[1] / 'policy/source-relocations.json'


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True, stderr=subprocess.PIPE).strip()


def subtree(repo, revision, prefix):
    result = subprocess.run(['git', '-C', str(repo), 'rev-parse', '--verify', f'{revision}:{prefix}'],
                            capture_output=True, text=True)
    if result.returncode:
        return None
    oid = result.stdout.strip()
    return oid if git(repo, 'cat-file', '-t', oid) == 'tree' else None



def _valid_prefix(prefix: str) -> str:
    path = pathlib.PurePosixPath(prefix)
    if (not prefix or path.is_absolute() or '..' in path.parts or '.' in prefix.split('/')
            or path.as_posix() != prefix or not re.fullmatch(r'[A-Za-z0-9._/-]+', prefix)):
        raise ValueError('invalid source-relocation prefix')
    return prefix


def _generated_omissions(row: dict) -> list[dict]:
    """Only the checker release, never a caller declaration, may omit a tree."""
    omissions = row.get('generated_omissions', [])
    if not isinstance(omissions, list):
        raise ValueError('invalid generated source omissions')
    paths = []
    required = {'path', 'tree', 'dependency_repository', 'dependency_commit',
                'dependency_manifest', 'dependency_manifest_sha256'}
    for omitted in omissions:
        if not isinstance(omitted, dict) or set(omitted) != required or any(
                not isinstance(omitted[key], str) or not omitted[key] for key in required):
            raise ValueError('incomplete generated source omission')
        _valid_prefix(omitted['path'])
        _valid_prefix(omitted['dependency_manifest'])
        if not re.fullmatch(r'[0-9a-f]{40}', omitted['tree']) or not re.fullmatch(
                r'[0-9a-f]{40}', omitted['dependency_commit']) or not re.fullmatch(
                r'[0-9a-f]{64}', omitted['dependency_manifest_sha256']):
            raise ValueError('invalid generated source omission digest')
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', omitted['dependency_repository']):
            raise ValueError('invalid generated dependency repository identity')
        paths.append(omitted['path'])
    for index, path in enumerate(sorted(paths)):
        if any(other == path or other.startswith(path + '/') for other in sorted(paths)[index + 1:]):
            raise ValueError('generated source omissions overlap')
    return omissions


def _restored_origin_tree(repo: pathlib.Path, tree: str, omissions: list[dict]) -> str:
    """Hash the full origin by reinserting only trusted omitted subtree OIDs.

    Git tree serialization is reconstructed in memory: no network, object store
    writes or working-tree changes. Names and executable/symlink/gitlink modes
    remain byte-exact. An omission cannot replace an existing file or tree.
    """
    overlay = {}
    for omitted in omissions:
        node = overlay
        parts = omitted['path'].split('/')
        for part in parts[:-1]:
            node = node.setdefault(part.encode(), {})
        node[parts[-1].encode()] = omitted['tree']

    def restore(current, additions):
        entries = {}
        if current:
            result = subprocess.run(['git', '-C', str(repo), 'ls-tree', '-z', current],
                                    check=True, capture_output=True)
            for raw in result.stdout.split(b'\0'):
                if not raw:
                    continue
                metadata, name = raw.split(b'\t', 1)
                mode, _kind, oid = metadata.split(b' ')
                entries[name] = (format(int(mode, 8), 'o').encode(), oid.decode('ascii'))
        for name, addition in additions.items():
            existing = entries.get(name)
            if isinstance(addition, str):
                if existing:
                    raise ValueError('generated omission is present in maintained tree')
                entries[name] = (b'40000', addition)
            else:
                if existing and existing[0] != b'40000':
                    raise ValueError('generated omission ancestor is not a tree')
                entries[name] = (b'40000', restore(existing[1] if existing else None, addition))
        ordered = sorted(entries, key=lambda name: name + (b'/' if entries[name][0] == b'40000' else b''))
        payload = b''.join(entries[name][0] + b' ' + name + b'\0' + bytes.fromhex(entries[name][1])
                           for name in ordered)
        return hashlib.sha1(b'tree ' + str(len(payload)).encode() + b'\0' + payload).hexdigest()

    return restore(tree, overlay)


def roots(repo, base, head, repository, policy=POLICY):
    data = json.loads(pathlib.Path(policy).read_text())
    if data.get('version') != 1 or not isinstance(data.get('relocations'), list):
        raise ValueError('invalid shared source-relocation policy')
    selected = []
    for row in data['relocations']:
        if not isinstance(row, dict) or not all(isinstance(row.get(k), str) for k in
                ('repository', 'prefix', 'source_repository', 'source_commit', 'source_tree', 'evidence')):
            raise ValueError('incomplete shared source-relocation record')
        prefix = _valid_prefix(row['prefix'])
        omissions = _generated_omissions(row)
        if not all(re.fullmatch(r'[0-9a-f]{40}', row[k]) for k in ('source_commit', 'source_tree')):
            raise ValueError('source-relocation provenance requires immutable Git objects')
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', row['source_repository']):
            raise ValueError('invalid source repository identity')
        if row['evidence'] != 'https://github.com/' + row['source_repository'] + '/tree/' + row['source_commit']:
            raise ValueError('source-relocation evidence must name its exact origin commit')
        if row['repository'].lower() != repository.lower():
            continue
        if any(prefix == other['prefix'] or prefix.startswith(other['prefix'] + '/')
               or other['prefix'].startswith(prefix + '/') for other in selected):
            raise ValueError('source-relocation roots must not overlap')
        current = subtree(repo, head, prefix)
        prior = subtree(repo, base, prefix) if base else None
        if current is None:
            continue  # Deletion is still visible to immutable SQL and other guards.
        proof = 'existing-base'
        if prior is None:
            restored = _restored_origin_tree(repo, current, omissions) if omissions else current
            if restored != row['source_tree']:
                raise ValueError('initial source relocation differs from recorded origin tree: ' + prefix)
            for omitted in omissions:
                payload = subprocess.run(['git', '-C', str(repo), 'show',
                    head + ':' + omitted['dependency_manifest']], check=True, capture_output=True).stdout
                if hashlib.sha256(payload).hexdigest() != omitted['dependency_manifest_sha256']:
                    raise ValueError(prefix + ': generated dependency manifest differs from trusted proof')
            proof = 'exact-origin-projection' if omissions else 'exact-origin-tree'
        selected.append({**row, 'head_tree': current, 'base_tree': prior, 'proof': proof})
    return selected


class TreeView:
    """Repository-rooted or prefix-rooted read-only view of a checked Git tree."""
    def __init__(self, tree, prefix='', excluded=()):
        self.tree, self.prefix, self.excluded = tree, prefix.rstrip('/'), tuple(excluded)
        self.repo, self.rev = tree.repo, tree.rev
        stem = self.prefix + '/' if self.prefix else ''
        self._paths = [p[len(stem):] for p in tree.paths() if p.startswith(stem)
                       and not any(p == x or p.startswith(x + '/') for x in self.excluded)]
        self._path_set = set(self._paths)

    def paths(self):
        return self._paths

    def read(self, path, **kwargs):
        if path not in self._path_set:
            return None
        return self.tree.read((self.prefix + '/' if self.prefix else '') + path, **kwargs)

    def read_model_source(self, path):
        return self.read(path, encoding_errors='surrogateescape')


def outer(tree, records):
    return TreeView(tree, excluded=[r['prefix'] for r in records])


def local_changes(changed, prefix):
    if changed is None:
        return None
    stem = prefix + '/'
    return {p[len(stem):]: lines for p, lines in changed.items() if p.startswith(stem)}


def legacy_errors(head, base, records, baseline, departures, parse_baseline, decide):
    errors = []
    for row in records:
        current = TreeView(head, row['prefix'])
        prior = TreeView(base, row['prefix']) if base and row['base_tree'] else current
        def legacy(tree):
            parsed = parse_baseline(tree.read(baseline))
            return None if parsed is None else {v for v in parsed if not v.startswith(('architecture ', 'model-pin ', 'force-push '))}
        errors += [row['prefix'] + ': ' + error for error in
                   decide(departures(current), legacy(current), legacy(prior))]
    return errors


def architecture_report(head, base, records, baseline, changed, today, architecture, parse_baseline):
    combined = {'findings': [], 'unreadable': [], 'reviewed_paths': [], 'excluded_paths': []}
    errors = []
    for record in records:
        prefix = record['prefix']
        current = TreeView(head, prefix)
        prior = TreeView(base, prefix) if base and record['base_tree'] else current
        report = architecture.evaluate(current, record['source_repository'], local_changes(changed, prefix), today)
        previous = architecture.evaluate(prior, record['source_repository'], None, today)
        old = {architecture.baseline_key(row): row for row in previous['findings']}
        def entries(tree):
            return {v for v in parse_baseline(tree.read(baseline)) or () if v.startswith('architecture ')}
        head_records, base_records = entries(current), entries(prior)
        if architecture.BASELINE_MARKER in (prior.read(baseline) or '').splitlines() and architecture.BASELINE_MARKER not in (current.read(baseline) or '').splitlines():
            errors.append(prefix + ': architecture ratchet marker cannot be removed')
        errors += [prefix + ': ' + error for error in architecture.baseline_errors(
            report['findings'], head_records, base_records, previous['findings'])]
        for row in report['findings']:
            key = architecture.baseline_key(row)
            ancestor = old.get(key)
            row['changed'] = not bool(ancestor) or bool(ancestor['decision'] and not row['decision'])
            if row['rule'] in {'tenant-read-functions', 'deal-randomness', 'project-portability'}:
                row['changed'] = key not in head_records
            row['source_root_baseline_recorded'] = key in head_records
            row['source_provenance'] = {k: record[k] for k in ('prefix', 'source_repository', 'source_commit', 'source_tree', 'proof')}
            row['path'] = prefix + '/' + row['path']
            combined['findings'].append(row)
        for row in [*report['unreadable'], *previous['unreadable']]:
            combined['unreadable'].append({**row, 'path': prefix + '/' + row['path']})
        combined['reviewed_paths'] += [prefix + '/' + p for p in report['reviewed_paths']]
        combined['excluded_paths'] += [{**row, 'path': prefix + '/' + row['path']} for row in report['excluded_paths']]
    return combined, errors
