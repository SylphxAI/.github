#!/usr/bin/env python3
"""Inspect committed changes for public disclosures and unphased table drops."""

import argparse
from collections import Counter
import json
from pathlib import Path, PurePosixPath
import os
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def whole_token(pattern):
    return re.compile(rb"(?<![\w-])(?:" + pattern + rb")(?![\w-])", re.I)


def hostname(pattern):
    return re.compile(rb"(?<![\w.-])(?:" + pattern + rb")(?![\w-]|\.[\w-])", re.I)


def hygiene_rules():
    orgs = json.loads((ROOT / "policy/merge-queue.json").read_text())["orgs"]
    sibling_names = rb"|".join(re.escape(org.encode("ascii")) for org in orgs if org != "SylphxAI")
    return {
        "internal host or service name": hostname(
            rb"(?:[\w-]+\.)*(?:desk-next|gra-c[0-9]+|fsn1-c[0-9]+)(?:\.[\w-]+)*"
            rb"|(?:[\w-]+\.)*(?:kylet\.se|svc\.cluster\.local|sylphx\.internal)"
        ),
        "unrelated organization name": whole_token(sibling_names + rb"|Ozyrix|Epiow(?:[ -]AI)?"),
        "internal route or operator-only setting": whole_token(
            rb"host[-_]claims|(?:desk|internal)[-_]operator(?:[-_]token)?"
        ),
        "personal operator identifier": whole_token(rb"shtse8|Kyle[ \t]+Tse"),
    }


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args])


def revision(root, sha):
    if not re.fullmatch(r"[a-f0-9]{40}", sha or ""):
        raise ValueError("an exact base and head commit SHA are required")
    return git(root, "rev-parse", "--verify", sha + "^{commit}").decode().strip()


def changes(root, base, head):
    # Use NUL-delimited paths and full blob IDs; no textconv or worktree reads.
    # A rename counts as a removal and an addition at the destination path.
    fields = iter(git(root, "diff", "--raw", "--no-abbrev", "--no-renames",
                      "--diff-filter=AMT", "-z", base, head).split(b"\0")[:-1])
    for meta, path in zip(fields, fields, strict=True):
        old_mode, mode, old_oid, oid, _ = meta.removeprefix(b":").decode("ascii").split()
        yield path, old_mode, mode, old_oid, oid


def is_migration(path):
    path = PurePosixPath(os.fsdecode(path))
    return path.suffix.lower() == ".sql" and any(part.startswith("migrations") for part in path.parts[:-1])


def blob(root, mode, oid):
    if mode == "000000":
        return b""
    if mode not in {"100644", "100755", "120000"}:
        raise ValueError("cannot inspect a changed Git submodule or non-blob entry")
    return git(root, "cat-file", "blob", oid)


def hygiene_findings(before, after, rules):
    for reason, pattern in rules.items():
        known = Counter(m.group().lower() for m in pattern.finditer(before))
        line, start = 1, 0
        for match in pattern.finditer(after):
            line += after.count(b"\n", start, match.start())
            start = match.start()
            value = match.group().lower()
            if known[value]:
                known[value] -= 1
            else:
                yield line, reason


def sqlite_migration_dirs(raw):
    directories = []
    for line in raw.splitlines():
        if not (directory := line.strip()):
            continue
        directory = directory.rstrip("/")
        if (any(part in {"", ".", ".."} for part in directory.split("/"))
                or any(ch in directory for ch in "*?[]\\")):
            raise argparse.ArgumentTypeError("expected one repository-relative literal path per line")
        directories.append(PurePosixPath(directory))
    return tuple(directories)


def migration_history(root, base, head, path, file_dialect):
    """Only identical, committed predecessors in the same numbered stream."""
    directory, name = path.rsplit(b"/", 1)
    target = re.fullmatch(rb"(\d+)[_.-].*", name)
    if not target:
        return []
    target_version = int(target[1])

    def chain(ref):
        prefix = directory + b"/"
        entries = {}
        for entry in git(root, "--literal-pathspecs", "ls-tree", "-rz", ref, "--", os.fsdecode(prefix)).split(b"\0")[:-1]:
            meta, filename = entry.split(b"\t", 1)
            child = filename.removeprefix(prefix)
            if b"/" in child or not child.lower().endswith(b".sql") or filename == path:
                continue
            suffix = child.lower()
            if (suffix.endswith(b".mysql.sql") and file_dialect != "mysql"
                    or suffix.endswith(b".postgresql.sql") and file_dialect != "postgres"):
                continue
            v = re.fullmatch(rb"(\d+)[_.-].*", child)
            if not v:
                raise ValueError("expand proof needs ordered migration history")
            seq = int(v[1])
            # Reject an inserted/backdated version against a later deployed
            # schema; later files from this PR are not part of the proof.
            if seq > target_version and ref == head:
                continue
            mode, _, oid = meta.decode("ascii").split()
            if seq >= target_version or seq in entries or mode not in {"100644", "100755"}:
                raise ValueError("ambiguous expand migration history")
            entries[seq] = (filename, mode, oid)
        return entries

    try:
        old = chain(base)
        if old != chain(head):
            return []
        return [blob(root, mode, oid).decode("utf-8") for _, mode, oid in (old[v] for v in sorted(old))]
    except (ValueError, OSError, subprocess.CalledProcessError):
        return []


def check(root, base, head, public, migrations, dialect, sqlite_dirs=()):
    head = revision(root, head)
    base = revision(root, base)
    prior_tip = base
    base = git(root, "merge-base", base, head).decode().strip()
    rules = hygiene_rules() if public else {}
    findings = []
    for path, old_mode, mode, old_oid, oid in changes(root, base, head):
        migration = migrations and is_migration(path)
        if not public and not migration:
            continue
        label = path
        for reason, pattern in rules.items():
            if pattern.search(path):
                findings.append(f"Changed path: {reason}; choose a public name")
            label = pattern.sub(b"[redacted]", label)
        # Quote filenames so log commands or newlines from the caller never run.
        label = json.dumps(os.fsdecode(label))
        try:
            before, after = blob(root, old_mode, old_oid), blob(root, mode, oid)
            if public:
                findings.extend(f"{label}:{line}: {reason}; use a public example instead"
                                for line, reason in hygiene_findings(before, after, rules))
            if migration:
                if mode not in {"100644", "100755"}:
                    raise ValueError("migration SQL must be stored as a regular file")
                # Delay loading the canonical SQL checker until a migration is found.
                sys.path.insert(0, str(ROOT / ".github/actions/migration-guard"))
                try:
                    from migration_guard import drop_phase_findings
                finally:
                    sys.path.pop(0)
                if path.lower().endswith(b".mysql.sql"):
                    file_dialect = "mysql"
                elif path.lower().endswith(b".postgresql.sql"):
                    file_dialect = "postgres"
                else:
                    parent = PurePosixPath(os.fsdecode(path)).parent
                    file_dialect = "sqlite" if any(parent.is_relative_to(directory)
                                                   for directory in sqlite_dirs) else dialect
                findings.extend(f"{label}:{line}: {reason}; add a statement-scoped drop-phase "
                                "reference to the prior release, or defer the drop"
                                for line, reason in drop_phase_findings(
                                    before.decode("utf-8"), after.decode("utf-8"), file_dialect,
                                    history=lambda: migration_history(root, prior_tip, head, path, file_dialect)))
        except (OSError, ValueError, subprocess.CalledProcessError):
            findings.append(f"{label}: could not inspect changed file; check its Git object and file format")
    return findings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--public", action="store_true")
    parser.add_argument("--no-migrations", action="store_true")
    parser.add_argument("--dialect", choices=("postgres", "mysql"), default="postgres")
    parser.add_argument("--sqlite-migration-dirs", type=sqlite_migration_dirs, default="")
    args = parser.parse_args()
    try:
        findings = check(args.root, args.base, args.head, args.public, not args.no_migrations,
                         args.dialect, args.sqlite_migration_dirs)
    except (OSError, ValueError, subprocess.CalledProcessError):
        print("::error::Shared baseline could not read the comparison; check base and head SHAs and fetch full history.")
        return 1
    if findings:
        print("\n".join(findings))
        print("::error::Shared baseline check failed; see findings above.")
        return 1
    print("Shared baseline check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
