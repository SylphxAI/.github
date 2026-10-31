#!/usr/bin/env python3
"""Refuse repository references that a rename or owner transfer would break.

A repository is its immutable ID. A name in a workflow, a submodule URL or an
access declaration goes stale on the next move, so each move used to break
whatever still held the old name.
"""
import argparse
import json
from pathlib import Path
import re
import sys
import textwrap

REFERENCE_GLOBS = (".gitmodules", ".github/workflows/*.y*ml", ".github/actions/**/*.y*ml")
# An image's storage path is a registry namespace, not a GitHub owner.
REGISTRIES = r"(?:registry\.sylphx\.com|ghcr\.io)/"
LITERAL_NAME_TEST = re.compile(
    r"""github\.repository(?:_owner)?\s*[!=]=\s*['"]|['"]\s*[!=]=\s*github\.repository(?:_owner)?\b""")
# The caller naming a retired owner to this check is not a reference to it.
OWN_INPUT = re.compile(r"\s*legacy-owners:")
EMBEDDED_JSON = re.compile(r"^(\s*)[\w.-]+\.json:\s*\|[-+]?\s*$")


def legacy_owner_findings(path, text, owners):
    pattern = re.compile(r"(?<![\w-])(" + "|".join(map(re.escape, owners)) + r")(?![\w-])", re.IGNORECASE)
    storage = re.compile(REGISTRIES + r"$", re.IGNORECASE)
    for number, line in enumerate(text.splitlines(), 1):
        if OWN_INPUT.match(line):
            continue
        for match in pattern.finditer(line):
            if not storage.search(line[:match.start()]):
                yield f"{path}:{number}: retired GitHub owner {match.group(1)}; bind the repository ID"
                break


def name_test_findings(path, text):
    for number, line in enumerate(text.splitlines(), 1):
        if LITERAL_NAME_TEST.search(line):
            yield f"{path}:{number}: repository name test; use github.repository_id"


def json_documents(path, text):
    """A JSON file, or the JSON values embedded in a Kubernetes ConfigMap."""
    if path.suffix == ".json":
        yield json.loads(text)
        return
    lines = text.splitlines()
    for index, line in enumerate(lines):
        opening = EMBEDDED_JSON.match(line)
        if opening is None:
            continue
        block = []
        for body in lines[index + 1:]:
            if body.strip() and len(body) - len(body.lstrip()) <= len(opening.group(1)):
                break
            block.append(body)
        yield json.loads(textwrap.dedent("\n".join(block)))


def objects(value):
    if isinstance(value, dict):
        yield value
        value = list(value.values())
    if isinstance(value, list):
        for item in value:
            yield from objects(item)


def has_id(entry, *keys):
    return any(type(entry.get(key)) is int and entry[key] > 0 for key in keys)


def declaration_findings(path, text):
    for document in json_documents(path, text):
        for entry in objects(document):
            label = entry.get("name", "entry")
            if (isinstance(entry.get("repository"), str) and "workflow" in entry
                    and not has_id(entry, "repository_id", "repositoryId")):
                yield f"{path}: {label} binds a repository name without its repository_id"
            if "sourceRepository" in entry and not has_id(entry, "sourceRepositoryId"):
                yield f"{path}: {label} binds sourceRepository without sourceRepositoryId"


def check(root, owners, declarations):
    findings = []
    for pattern in REFERENCE_GLOBS:
        for file in sorted(root.glob(pattern)):
            relative, text = file.relative_to(root), file.read_text(encoding="utf-8")
            findings += legacy_owner_findings(relative, text, owners)
            findings += name_test_findings(relative, text)
    for pattern in declarations:
        for file in sorted(root.glob(pattern)):
            findings += declaration_findings(file.relative_to(root), file.read_text(encoding="utf-8"))
    return findings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--legacy-owners", nargs="+", required=True)
    parser.add_argument("--declarations", nargs="*", default=[])
    args = parser.parse_args()
    findings = check(args.root, args.legacy_owners, args.declarations)
    for finding in findings:
        print(finding, file=sys.stderr)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
