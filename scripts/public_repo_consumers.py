#!/usr/bin/env python3
"""public_repo_consumers.py -- list public SylphxAI repositories with no recorded internal consumer.

We publish what we use ourselves. Each public, unarchived repository carries the
organization custom property `sylphx_consumer`: the repository path or desk
configuration that runs it. A public repository without it is a tool nobody in
the company uses, and goes to a keep, merge or archive decision
(docs/public-repo-consumers.md).

Delivered customer projects (`sylphx_delivery` = `delivered`) are skipped: they
are hands-off.

Usage:
  scripts/public_repo_consumers.py [--org SylphxAI] [--json] [--input repos.json]

Reads the organization's repository list with `gh api --paginate` (each repo
object carries `custom_properties`), or a saved list with --input.

Exit: 0 every public repository has a consumer, 1 at least one has none,
2 the list or the properties could not be read (never reported as green).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

PROPERTY = "sylphx_consumer"


class Unreadable(Exception):
    """The repository list or its custom properties could not be read."""


def fetch(org: str) -> list[dict]:
    cmd = ["gh", "api", "--paginate", "--slurp", f"orgs/{org}/repos?type=all&per_page=100"]
    try:
        out = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=120).stdout
        pages = json.loads(out)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise Unreadable(f"cannot list {org} repositories: {exc}") from exc
    return [repo for page in pages for repo in page]


def missing_consumers(repos: list[dict]) -> list[dict]:
    """The public, unarchived, not delivered repositories with no `sylphx_consumer` value, by name.

    A credential that cannot read custom properties gets an empty map for every
    repository; when no public repository shows any custom property at all, the
    read is treated as failed rather than flagging everything."""
    public = [r for r in repos if r.get("visibility") == "public" and not r.get("archived") and not r.get("disabled")]
    if not public:
        raise Unreadable("no public repositories in the list")
    if not any(r.get("custom_properties") for r in public):
        raise Unreadable("no public repository shows custom properties: the credential cannot read them")
    out = []
    for r in public:
        props = r.get("custom_properties") or {}
        if props.get("sylphx_delivery") == "delivered":
            continue
        if not str(props.get(PROPERTY) or "").strip():
            out.append({"name": r["name"], "fork": bool(r.get("fork")), "pushed_at": r.get("pushed_at"),
                        "url": r.get("html_url")})
    return sorted(out, key=lambda r: r["name"].lower())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--org", default="SylphxAI")
    ap.add_argument("--input", help="a saved repository list (JSON array, or an array of pages)")
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    args = ap.parse_args(argv)
    try:
        if args.input:
            with open(args.input, encoding="utf-8") as fh:
                data = json.load(fh)
            repos = [r for page in data for r in page] if data and isinstance(data[0], list) else data
        else:
            repos = fetch(args.org)
        missing = missing_consumers(repos)
    except (Unreadable, OSError, ValueError) as exc:
        print(f"public-repo-consumers: UNREADABLE: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"org": args.org, "property": PROPERTY, "missing": missing}, indent=2))
    else:
        for r in missing:
            print(f"MISSING {args.org}/{r['name']}{' (fork)' if r['fork'] else ''} pushed {r['pushed_at']}")
        print(f"public-repo-consumers: {len(missing)} public {args.org} repositories with no {PROPERTY}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
