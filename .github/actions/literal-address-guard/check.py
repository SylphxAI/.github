#!/usr/bin/env python3
"""Reject newly added inventory-owned IP literals; never execute caller code.

Policy is generated from the owning repository's current checkout, not a copied
address list. Source digests and revision are checked before any diff is scanned.
Only exact scalar spans in that same repository may be authorized.
"""
import argparse
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import subprocess
import sys

# A token cannot be a substring of a dotted number, hostname or identifier.
# Ports and brackets are delimiters. IPv6 zone identifiers are ignored for
# matching, not for the reported span. IPv4-mapped IPv6 is also checked as IPv4.
V4 = re.compile(r"(?<![\w.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?:/[0-9]{1,2})?(?![\w./])")
V6 = re.compile(r"(?<![\w.:])(?=[0-9a-fA-F:.]*:)[0-9a-fA-F:.]+(?:%[\w.-]+)?(?:/[0-9]{1,3})?(?![\w.:%/])")


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args])


def revision(root, ref):
    return git(root, "rev-parse", "--verify", ref + "^{commit}").decode().strip()


def literals(text):
    """Yield canonical networks and exact source character spans."""
    occupied = []
    for pattern in (V6, V4):
        for match in pattern.finditer(text):
            if any(match.start() < end and match.end() > start for start, end in occupied):
                continue
            raw = match.group()
            try:
                value = ipaddress.ip_network(re.sub(r"%[^/]+", "", raw), strict=False)
            except ValueError:
                continue
            occupied.append(match.span())
            yield value, match.start(), match.end()
            if value.version == 6 and value.prefixlen == 128 and value.network_address.ipv4_mapped:
                yield ipaddress.ip_network(value.network_address.ipv4_mapped), match.start(), match.end()


def load_policy(path, source_root):
    policy = json.loads(Path(path).read_text())
    if policy.get("version") != 1:
        raise ValueError("unsupported address inventory version")
    if policy.get("sourceHead") != revision(source_root, "HEAD"):
        raise ValueError("address inventory is stale: source HEAD changed; regenerate it")
    sources = policy.get("sources", [])
    if not sources or not any(s["revision"] == policy["sourceHead"] for s in sources):
        raise ValueError("address inventory has no current source receipts")
    for source in sources:
        actual = git(source_root, "show", source["revision"] + ":" + source["path"])
        if hashlib.sha256(actual).hexdigest() != source["sha256"]:
            raise ValueError("address inventory source digest mismatch: " + source["path"])
    networks = [ipaddress.ip_network(n["cidr"], strict=True) for n in policy["networks"]]
    if not networks:
        raise ValueError("empty address inventory")
    for permit in policy["permits"]:
        if not permit["reason"] or permit["start"] < 0 or permit["end"] <= permit["start"]:
            raise ValueError("invalid exact scalar authorization")
    return policy, networks


def added_lines(root, base, head):
    """Read changed blobs by path, then parse line positions without path quoting."""
    paths = git(root, "diff", "--name-only", "--no-renames", "--diff-filter=AMT", "-z", base, head).split(b"\0")
    for raw in paths:
        if not raw:
            continue
        path = raw.decode("utf-8")
        mode = git(root, "ls-tree", head, "--", path).split(b" ", 1)[0]
        if mode == b"160000":
            raise ValueError("changed submodule cannot be scanned: " + path)
        content = git(root, "show", head + ":" + path)
        # Binary content has no text literals; this guard does not inspect
        # compiled artifacts or encrypted content.
        if b"\0" in content:
            continue
        content.decode("utf-8")  # Fail closed on unreadable text.
        diff = git(root, "diff", "--no-ext-diff", "--no-textconv", "--text", "--no-renames", "--unified=0", base, head, "--", path).decode("utf-8")
        line = None
        for text in diff.split("\n"):
            text = text.removesuffix("\r")
            if text.startswith("@@"):
                line = int(re.search(r"\+(\d+)", text).group(1))
            elif line is not None and text.startswith("+"):
                yield path, line, text[1:]
                line += 1
            elif line is not None and text.startswith(" "):
                line += 1


def check(root, base, head, policy, networks, source_root):
    # An inventory owner's declarations do not authorize the same path in a
    # consumer repository, even when it happens to have the same filename.
    permits = policy["permits"] if Path(root).resolve() == Path(source_root).resolve() else []
    indexed = {}
    for p in permits:
        indexed.setdefault((p["path"], p["line"]), []).append(p)
    hits = []
    for path, line, text in added_lines(root, base, head):
        for candidate, start, end in literals(text):
            if not any(candidate.version == n.version and candidate.subnet_of(n) for n in networks):
                continue
            if any(p["text"] == text and p["start"] <= start and end <= p["end"]
                   for p in indexed.get((path, line), [])):
                continue
            finding = (path, line, start, text[start:end])
            if finding not in hits:
                hits.append(finding)
    return hits


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--inventory-root", default=".")
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", default="HEAD")
    args = parser.parse_args()
    try:
        if not args.base.strip():
            raise ValueError("base revision is required; refusing to silently skip the guard")
        base, head = revision(args.root, args.base), revision(args.root, args.head)
        policy, networks = load_policy(args.inventory, args.inventory_root)
        if Path(args.root).resolve() == Path(args.inventory_root).resolve() and policy["sourceHead"] != head:
            raise ValueError("address inventory does not describe the checked head")
        hits = check(args.root, base, head, policy, networks, args.inventory_root)
        for path, line, _, value in hits:
            print(f"{path}:{line}: literal inventory-owned address {value}; use the routing/DNS contract")
        print(f"literal-address-guard: {len(networks)} inventory networks, {len(hits)} new literal(s); source {policy['sourceHead']}")
        return bool(hits)
    except (ValueError, KeyError, TypeError, OSError, subprocess.CalledProcessError) as error:
        print(f"literal-address-guard: cannot verify: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
