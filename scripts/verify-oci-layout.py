#!/usr/bin/env python3
"""Verify an OCI image layout is complete before it is pushed.

A GitHub artifact download can end short without failing its step (live
2026-09-27: a 2.1 GiB image transfer stopped after 21 minutes, the step
reported success, and the push step found no index.json). This walks the
layout from index.json through every manifest and index to every blob, and
checks each blob exists with its declared size and sha256 digest. It prints
what is missing or corrupt, never any credential, and exits 1 on the first
problem class it finds.

Usage: verify-oci-layout.py <layout-dir> [--label NAME]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

INDEX_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
MANIFEST_TYPES = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}


def blob_path(root: Path, digest: str) -> Path:
    algorithm, _, value = digest.partition(":")
    if algorithm != "sha256" or len(value) != 64:
        raise ValueError(f"unsupported digest {digest!r}")
    return root / "blobs" / "sha256" / value


def check_blob(root: Path, descriptor: dict, problems: list[str]) -> bytes | None:
    digest = descriptor.get("digest", "")
    size = descriptor.get("size")
    try:
        path = blob_path(root, digest)
    except ValueError as error:
        problems.append(str(error))
        return None
    if not path.is_file():
        problems.append(f"missing blob {digest}")
        return None
    actual_size = path.stat().st_size
    if isinstance(size, int) and actual_size != size:
        problems.append(f"blob {digest} is {actual_size} bytes, expected {size}")
        return None
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(chunk)
    if "sha256:" + hasher.hexdigest() != digest:
        problems.append(f"blob {digest} content does not match its digest")
        return None
    media = descriptor.get("mediaType", "")
    if media in INDEX_TYPES or media in MANIFEST_TYPES:
        return path.read_bytes()
    return None


def walk(root: Path, document: dict, problems: list[str], counts: dict) -> None:
    for child in document.get("manifests") or []:
        body = check_blob(root, child, problems)
        counts["manifests"] += 1
        if body is None:
            continue
        nested = json.loads(body)
        if child.get("mediaType") in INDEX_TYPES:
            walk(root, nested, problems, counts)
        else:
            for layer in [nested.get("config", {})] + (nested.get("layers") or []):
                if layer:
                    check_blob(root, layer, problems)
                    counts["blobs"] += 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("layout")
    parser.add_argument("--label", default="image")
    args = parser.parse_args()
    root = Path(args.layout)
    problems: list[str] = []
    if not root.is_dir():
        problems.append(f"layout directory {root} does not exist")
    else:
        if not (root / "oci-layout").is_file():
            problems.append("oci-layout file missing")
        if not (root / "index.json").is_file():
            problems.append("index.json missing")
    if problems:
        present = sorted(p.name for p in root.iterdir())[:20] if root.is_dir() else []
        print(f"::error title=OCI {args.label} transfer incomplete::{'; '.join(problems)}; present: {present}")
        return 1
    counts = {"manifests": 0, "blobs": 0}
    try:
        walk(root, json.loads((root / "index.json").read_bytes()), problems, counts)
    except (ValueError, json.JSONDecodeError) as error:
        problems.append(f"unreadable manifest: {error}")
    if problems:
        shown = problems[:10]
        more = f" (+{len(problems) - 10} more)" if len(problems) > 10 else ""
        print(f"::error title=OCI {args.label} transfer incomplete::{'; '.join(shown)}{more}")
        return 1
    print(f"oci_layout_ok label={args.label} manifests={counts['manifests']} blobs={counts['blobs']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
