#!/usr/bin/env python3
"""Offline metadata patching. Call render_plan(plan, sources) without any I/O."""
import argparse
import difflib
import json
from pathlib import Path
import re
import sys


class PlanError(ValueError):
    """A required target or edit is invalid."""


def fields(obj, required, optional=()):
    if not isinstance(obj, dict) or not set(required) <= obj.keys() or obj.keys() - set(required) - set(optional):
        raise PlanError(f'expected fields {", ".join(required)}; optional: {", ".join(optional)}')


def text(value, label):
    if not isinstance(value, str):
        raise PlanError(f'{label} must be a string')
    return value


def patch(source, edit):
    """Apply one required edit; replacements are literal, not regex templates."""
    if not isinstance(edit, dict):
        raise PlanError('edit must be an object')
    kind = edit.get('type')
    if kind == 'region':
        fields(edit, ('type', 'value'), ('name', 'start', 'end'))
        if 'name' in edit:
            if 'start' in edit or 'end' in edit:
                raise PlanError('region uses name OR start/end')
            name = text(edit['name'], 'name')
            if not name:
                raise PlanError('region name must not be empty')
            start, end = f'<!-- sync:{name} -->', f'<!-- /sync:{name} -->'
        else:
            start, end = text(edit.get('start'), 'start'), text(edit.get('end'), 'end')
        if not start or not end or start == end:
            raise PlanError('region markers must be non-empty and distinct')
        counts = (source.count(start), source.count(end))
        if max(counts) > 1:
            raise PlanError('duplicate region marker')
        if min(counts) == 0:
            raise PlanError('missing region marker')
        a, b = source.index(start) + len(start), source.index(end)
        if b < a:
            raise PlanError('reversed region markers')
        value = text(edit['value'], 'value')
        if start in value or end in value:
            raise PlanError('replacement contains region marker')
        return source[:a] + value + source[b:]
    if kind == 'regex':
        fields(edit, ('type', 'pattern', 'value'))
        pattern = text(edit['pattern'], 'pattern')
        if not pattern:
            raise PlanError('pattern must not be empty')
        matches = list(re.finditer(pattern, source, re.MULTILINE))
        if not matches:
            raise PlanError('zero-match field')
        if len(matches) != 1:
            raise PlanError(f'duplicate field: {len(matches)} matches')
        match = matches[0]
        if match.start() == match.end():
            raise PlanError('field selector matched zero characters')
        return source[:match.start()] + text(edit['value'], 'value') + source[match.end():]
    if kind == 'json':
        fields(edit, ('type', 'path', 'value'))
        path = edit['path']
        if not isinstance(path, list) or not path or any(not isinstance(p, str) and type(p) is not int for p in path):
            raise PlanError('JSON path must be a non-empty array of keys or indexes')
        obj = json.loads(source)
        target = obj
        for key in path[:-1]:
            target = member(target, key)
        member(target, path[-1])  # Never silently create a missing field.
        target[path[-1]] = edit['value']
        return json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    if kind == 'file':
        fields(edit, ('type', 'value'))
        return text(edit['value'], 'value')
    raise PlanError(f'unknown edit type: {kind!r}')


def member(obj, key):
    if isinstance(obj, dict) and isinstance(key, str) and key in obj:
        return obj[key]
    if isinstance(obj, list) and type(key) is int and 0 <= key < len(obj):
        return obj[key]
    raise PlanError(f'zero-match field: JSON component {key!r}')


def render_plan(plan, sources):
    """Validate and compose the entire plan, returning outputs; never mutates sources."""
    fields(plan, ('version', 'files'))
    if type(plan['version']) is not int or plan['version'] != 1:
        raise PlanError('unsupported plan version')
    if not isinstance(plan['files'], list) or not plan['files']:
        raise PlanError('files must be a non-empty array')
    outputs = {}
    for entry in plan['files']:
        fields(entry, ('path', 'edits'))
        path = text(entry['path'], 'path')
        if not path or Path(path).is_absolute() or '..' in Path(path).parts:
            raise PlanError(f'invalid relative target path: {path!r}')
        if path not in sources:
            raise PlanError(f'{path}: missing target file')
        if not isinstance(entry['edits'], list) or not entry['edits']:
            raise PlanError(f'{path}: edits must be a non-empty array')
        source = outputs.get(path, sources[path])
        for index, edit in enumerate(entry['edits'], 1):
            try:
                source = patch(source, edit)
            except (ValueError, TypeError, re.error) as exc:
                raise PlanError(f'{path}: edit {index}: {exc}') from exc
        outputs[path] = source
    return outputs


def synchronize(plan, root, mode):
    """Read all targets, render all edits, then check or write changed UTF-8 bytes."""
    if mode not in ('check', 'write'):
        raise PlanError('mode must be check or write')
    root = Path(root).resolve()
    # Validate shape and paths before opening anything; no targets are created.
    fields(plan, ('version', 'files'))
    if not isinstance(plan['files'], list):
        raise PlanError('files must be an array')
    paths, sources = {}, {}
    for entry in plan['files']:
        fields(entry, ('path', 'edits'))
        rel = text(entry['path'], 'path')
        if not rel or Path(rel).is_absolute() or '..' in Path(rel).parts:
            raise PlanError(f'invalid relative target path: {rel!r}')
        target = (root / rel).resolve()
        if not target.is_relative_to(root) or target == root:
            raise PlanError(f'{rel}: target escapes root')
        if target in paths.values() and paths.get(rel) != target:
            raise PlanError(f'{rel}: duplicate target alias')
        paths[rel] = target
        sources[rel] = target.read_bytes().decode('utf-8')
    outputs = render_plan(plan, sources)
    changed = [rel for rel in outputs if outputs[rel] != sources[rel]]
    if mode == 'check':
        for rel in changed:
            print(f'{rel}: out of sync')
            print(''.join(difflib.unified_diff(sources[rel].splitlines(keepends=True),
                                             outputs[rel].splitlines(keepends=True),
                                             fromfile=rel, tofile=rel + ' (expected)')), end='')
        return 1 if changed else 0
    # Encode every output before the first write, too.
    encoded = {rel: outputs[rel].encode('utf-8') for rel in changed}
    for rel in changed:
        paths[rel].write_bytes(encoded[rel])
    print(f'metadata synced: {len(changed)} file(s) changed')
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', required=True, help='JSON plan with already-rendered values')
    parser.add_argument('--root', default='.', help='Caller repository root')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--write', action='store_true')
    args = parser.parse_args()
    try:
        plan = json.loads(Path(args.plan).read_bytes())
        return synchronize(plan, args.root, 'check' if args.check else 'write')
    except (OSError, ValueError, TypeError) as exc:
        print(f'metadata-sync: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
