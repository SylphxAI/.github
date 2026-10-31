#!/usr/bin/env python3
"""Reject an unsafe run-store bundle before it is unpacked.

A run-store `get` unpacks a bundle another job of the run produced (or, with
`run-id`, another run). Its tar members are untrusted: a member name may escape
the destination (`..` or an absolute path), a symlink or hardlink may point
outside it and be written through by a later member, and a special file (fifo,
device, socket) has no place in a file hand-over. Only regular files and
directories are allowed, and every name is relative with no `..` component.
Exits 0 when every member is safe, else prints the first offending member on
stderr and exits 1, so the caller unpacks nothing.
"""

from __future__ import annotations

import sys
import tarfile
from pathlib import Path


def unsafe_reason(member: tarfile.TarInfo) -> str | None:
    name = member.name
    if name.startswith("/"):
        return f"absolute path {name!r}"
    if ".." in name.split("/"):
        return f"path traversal {name!r}"
    if member.issym():
        return f"symlink {name!r} -> {member.linkname!r}"
    if member.islnk():
        return f"hardlink {name!r} -> {member.linkname!r}"
    if member.isfile() or member.isdir():
        return None
    return f"special file {name!r}"


def main() -> int:
    if len(sys.argv) not in (2, 3):
        print("usage: validate_archive.py <bundle.tgz> [destination]", file=sys.stderr)
        return 2
    try:
        destination = Path(sys.argv[2]).absolute() if len(sys.argv) == 3 else None
        if destination is not None and any(p.is_symlink() for p in [destination, *destination.parents]):
            print("unsafe destination: existing symlink", file=sys.stderr)
            return 1
        with tarfile.open(sys.argv[1], "r:gz") as archive:
            for member in archive:
                reason = unsafe_reason(member)
                if reason is not None:
                    print(f"unsafe archive member: {reason}", file=sys.stderr)
                    return 1
                if destination is not None:
                    target = destination / member.name
                    # A pre-existing link is as unsafe as one in the archive.
                    for ancestor in [target, *target.parents]:
                        if ancestor.is_symlink():
                            print("unsafe destination: existing symlink", file=sys.stderr)
                            return 1
                        if ancestor == destination:
                            break
    except tarfile.TarError as exc:
        print(f"invalid archive: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
