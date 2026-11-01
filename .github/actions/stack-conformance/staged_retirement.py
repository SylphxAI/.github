"""Source-bound staged retirement, not permission to expand a retired engine.

Bindings cover EVERY tracked entry below the detected project directory: paths,
Git modes and object identities, including binary assets, symlinks and gitlinks.
Only the ratchet file itself is excluded to avoid self-reference. No caller
exclusions or product identities are accepted. A root-level project therefore
conservatively binds the repository, not just the detector's two Lua inputs.
"""
from __future__ import annotations

import hashlib
import json
import posixpath
import re

RULE = "legacy/solar2d"
CATEGORY = "solar2d"


def records(lines):
    result = {}
    for line in lines or ():
        if not line.startswith(CATEGORY + " "):
            continue
        parts = line.split()
        if len(parts) == 2:
            continue  # Historical path-only records retain their old ratchet.
        if len(parts) != 3 or not re.fullmatch(r"[a-f0-9]{64}", parts[2]):
            raise ValueError("invalid source-bound retirement record: " + line)
        if parts[1] in result:
            raise ValueError("duplicate retirement project: " + parts[1])
        result[parts[1]] = (parts[2], line)
    return result


def inputs(tree, settings, baseline):
    parent = posixpath.dirname(settings)
    stem = parent + "/" if parent else ""
    return {path[len(stem):]: identity for path, identity in tree.entries().items()
            if path.startswith(stem) and path != baseline}


def digest(entries):
    return hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode()).hexdigest()


def binding(tree, settings, baseline):
    return CATEGORY + " " + settings + " " + digest(inputs(tree, settings, baseline))


def subset(before, after):
    return all(before.get(path) == identity for path, identity in after.items())


def findings(tree, lines, departures, baseline, previous_lines=()):
    """An anchor deletion does not retire the independently bound subtree."""
    found = set(departures(tree))
    for settings in records(lines).keys() | records(previous_lines).keys():
        if inputs(tree, settings, baseline):
            found.add(CATEGORY + " " + settings)
    return sorted(found)


def plan(base, head, original, introduced, departures, parse_baseline, baseline):
    """Generate bindings only from independently observed old, exact inputs."""
    existing = parse_baseline(original) or set()
    bound = records(existing)
    before = set(departures(base))
    after = set(departures(head))
    text = original or ""
    additions = set()
    for settings, (expected, line) in bound.items():
        prior, retained = inputs(base, settings, baseline), inputs(head, settings, baseline)
        if digest(prior) != expected or not prior:
            raise ValueError("base retirement binding does not match source: " + settings)
        if not subset(prior, retained):
            raise ValueError("retired project inputs changed or expanded: " + settings)
        if not retained:
            text = "".join(raw for raw in text.splitlines(keepends=True) if raw.strip() != line)
            continue
        text = text.replace(line, binding(head, settings, baseline))
    if RULE in introduced:
        for departure in sorted(after):
            if not departure.startswith(CATEGORY + " ") or departure in existing:
                continue
            settings = departure[len(CATEGORY) + 1:]
            if settings in bound:
                continue
            if departure not in before or not subset(inputs(base, settings, baseline), inputs(head, settings, baseline)):
                raise ValueError("new or changed retired project cannot be adopted: " + settings)
            additions.add(binding(head, settings, baseline))
    return text, additions


def check(head, base, head_lines, base_lines, departures, decide, *, baseline, allow_seed=False):
    """Keep retirement visible while allowing only exact inputs or deletion."""
    # A trunk check validates the committed binding against its own tree.
    base = base or head
    old = set(head_lines or ()) if base_lines is None and base is head else set(base_lines or ())
    current = set(head_lines or ())
    errors = []
    try:
        before_bound, after_bound = records(old), records(current)
    except ValueError as error:
        return [str(error)]
    found = findings(head, current, departures, baseline, previous_lines=old)
    previous = set(departures(base))
    for settings, (expected, _) in before_bound.items():
        prior, retained = inputs(base, settings, baseline), inputs(head, settings, baseline)
        if digest(prior) != expected or not prior:
            errors.append("base retirement binding does not match source: " + settings)
        if settings not in after_bound and retained:
            errors.append("retained retired project inputs require their source binding: " + settings)
        if not subset(prior, retained):
            errors.append("retired project inputs changed or expanded: " + settings)
    for settings, (expected, line) in after_bound.items():
        plain = CATEGORY + " " + settings
        if plain in current:
            errors.append("retirement project has both bound and path-only records: " + settings)
        valid = True
        if expected != digest(inputs(head, settings, baseline)) or not inputs(head, settings, baseline):
            errors.append("retirement binding does not match source: " + settings)
            valid = False
        if settings not in before_bound and not allow_seed:
            errors.append("retirement binding requires introduced-rule adoption: " + settings)
            valid = False
        if ((settings not in before_bound and plain not in previous)
                or not subset(inputs(base, settings, baseline), inputs(head, settings, baseline))):
            errors.append("retired project inputs changed or expanded: " + settings)
            valid = False
        current.discard(line)
        if valid:
            current.add(plain)
            old.add(plain)
    for _, line in before_bound.values():
        old.discard(line)
    # A new caller-controlled path-only record cannot bypass source binding.
    for line in sorted(current - old):
        if line.startswith(CATEGORY + " "):
            errors.append("new retired project requires a source-bound adoption record: " + line)
    errors += decide(found, current, old if base_lines is not None else None)
    return errors
