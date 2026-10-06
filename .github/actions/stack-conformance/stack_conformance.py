#!/usr/bin/env python3
"""Stack conformance: a ratchet on departures from the company's default stack.

The default stack (SylphxAI/owner standards/stack.md) puts every backend
service in Rust and every first-party schema under Atlas. A repository lists
the departures it already has in a baseline file (default
`.github/stack-departures.txt`); the baseline may only shrink. This script
finds three kinds of departure in a git tree:

  migrations <sylphx.toml> <engine>   [database.migrations] engine is not atlas
  ts-server <package.json> <package>  a TypeScript server framework is a
                                      runtime dependency of a package
  bun-node-service <Dockerfile>       a service built from sylphx.toml runs on
                                      a Bun or Node image and its package is
                                      not a web app

Subcommands:

  check --base REV --head REV   the pull-request gate: fail on a departure the
                                base's baseline does not list, on a baseline
                                line the base did not have (it may only
                                shrink), and on a baseline line whose
                                departure is gone (delete the line)
  scan [--rev REV]              print the departures of one tree, one per
                                line, in baseline format (seeds a baseline)
  all DIR...                    scan each checkout at HEAD and print how many
                                have no departure at all

It reads git objects only, so base and head need no second checkout.
"""
from __future__ import annotations

import argparse
import json
import posixpath
import re
import subprocess
import sys

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11: the regex reader below is enough.
    tomllib = None

BASELINE = ".github/stack-departures.txt"

# Server frameworks whose presence as a runtime dependency makes a package a
# TypeScript backend.
SERVER_FRAMEWORKS = frozenset({
    "hono", "express", "fastify", "elysia", "koa", "@trpc/server",
    "@nestjs/core", "@hapi/hapi", "h3",
})

# A package that depends on one of these is a web app, which the default stack
# allows on TypeScript and Bun (a Next.js server, a static site).
WEB_FRAMEWORKS = frozenset({
    "next", "astro", "nuxt", "@sveltejs/kit", "@remix-run/node",
    "@remix-run/react", "react-router", "@react-router/node", "vite",
    "@tanstack/react-start", "@tanstack/start", "gatsby", "@angular/core",
    "vike", "waku",
})

SKIP_DIRS = frozenset({"node_modules", "vendor", "target", "dist", "build", ".next"})

JS_RUNTIME_IMAGE = re.compile(r"^(node|nodejs\d*|bun)$")

# The executables that start a JavaScript service. A command that names none
# of them and does not start with a shell script runs something else.
JS_LAUNCHERS = frozenset({
    "bun", "bunx", "node", "npm", "npx", "pnpm", "yarn", "tsx", "sh", "bash",
    "docker-entrypoint.sh",
})


class Tree:
    """Read-only view of one git revision."""

    def __init__(self, repo: str, rev: str):
        self.repo, self.rev = repo, rev
        out = git(repo, "ls-tree", "-r", "-z", "--name-only", rev)
        self._paths = [p for p in out.split("\0") if p]

    def paths(self) -> list[str]:
        return self._paths

    def read(self, path: str) -> str | None:
        r = subprocess.run(
            ["git", "-C", self.repo, "cat-file", "blob", f"{self.rev}:{path}"],
            capture_output=True)
        if r.returncode != 0:
            return None
        return r.stdout.decode("utf-8", "replace")


class DictTree:
    """A tree from a dict of path -> text (tests)."""

    def __init__(self, files: dict[str, str]):
        self.files = files

    def paths(self) -> list[str]:
        return sorted(self.files)

    def read(self, path: str) -> str | None:
        return self.files.get(path)


def git(repo: str, *args: str) -> str:
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed in {repo}: {r.stderr.strip()}")
    return r.stdout


def skipped(path: str) -> bool:
    return any(part in SKIP_DIRS for part in path.split("/")[:-1])


def named(tree, name: str) -> list[str]:
    return [p for p in tree.paths() if posixpath.basename(p) == name and not skipped(p)]


def parse_toml(text: str) -> dict:
    if tomllib is not None:
        try:
            return tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            pass
    return parse_toml_lines(text)


def parse_toml_lines(text: str) -> dict:
    """The two facts we need, read line by line: the migration engine and every
    `dockerfile` value. Used when tomllib is missing or the file does not parse."""
    data: dict = {"_dockerfiles": []}
    section = ""
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        m = re.match(r"^\[\[?\s*([^\]]+?)\s*\]\]?$", line)
        if m:
            section = m.group(1)
            continue
        m = re.match(r'^([A-Za-z0-9_-]+)\s*=\s*"([^"]*)"', line)
        if not m:
            continue
        key, value = m.groups()
        if section == "database.migrations" and key == "engine":
            data.setdefault("database", {}).setdefault("migrations", {})["engine"] = value
        if key == "dockerfile":
            data["_dockerfiles"].append(value)
    return data


def dockerfiles(data) -> list[str]:
    """Every `dockerfile = "..."` value anywhere in a parsed sylphx.toml."""
    found = list(data.get("_dockerfiles", [])) if isinstance(data, dict) else []
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "dockerfile" and isinstance(value, str):
                    found.append(value)
                else:
                    stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)
    return sorted(set(found))


def package(tree, path: str) -> dict:
    text = tree.read(path)
    if text is None:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def deps(pkg: dict, *fields: str) -> set[str]:
    names: set[str] = set()
    for field in fields:
        value = pkg.get(field)
        if isinstance(value, dict):
            names.update(value)
    return names


def final_stage(text: str) -> tuple[str | None, list[str]]:
    """The base image of the last stage (following `FROM <stage>` aliases) and
    the words of its command: ENTRYPOINT then CMD, exec or shell form."""
    stages: dict[str, str] = {}
    image = None
    entrypoint: list[str] = []
    cmd: list[str] = []
    for raw in text.splitlines():
        m = re.match(r"^\s*(FROM|CMD|ENTRYPOINT)\s+(.*)$", raw, re.IGNORECASE)
        if not m:
            continue
        op, rest = m.group(1).upper(), m.group(2).strip()
        if op == "FROM":
            words = [w for w in rest.split() if not w.startswith("--")]
            if not words:
                continue
            image = stages.get(words[0].lower(), words[0])
            if len(words) >= 3 and words[1].lower() == "as":
                stages[words[2].lower()] = image
            entrypoint, cmd = [], []
            continue
        try:
            words = json.loads(rest) if rest.startswith("[") else rest.split()
        except ValueError:
            words = rest.split()
        words = [w for w in words if isinstance(w, str)]
        if op == "ENTRYPOINT":
            entrypoint = words
        else:
            cmd = words
    return image, entrypoint + cmd


def image_name(image: str) -> str:
    image = image.split("@", 1)[0]
    name = image.rsplit("/", 1)[-1]
    return name.split(":", 1)[0].lower()


def nearest_package(tree, start: str) -> str | None:
    paths = set(tree.paths())
    d = start
    while True:
        candidate = posixpath.join(d, "package.json") if d else "package.json"
        if candidate in paths:
            return candidate
        if not d:
            return None
        d = posixpath.dirname(d)


def service_packages(tree, dockerfile: str, command: list[str]) -> list[str]:
    """The packages a service image may run: the nearest package.json above
    the Dockerfile, and the package of each repository path its command names
    (`bun apps/web/server.js` in a monorepo built from the root)."""
    paths = set(tree.paths())
    found = []
    nearest = nearest_package(tree, posixpath.dirname(dockerfile))
    if nearest:
        found.append(nearest)
    for word in command[1:]:
        parts = [p for p in word.strip("/").split("/") if p not in ("", ".")]
        for i in range(len(parts)):
            d = posixpath.dirname("/".join(parts[i:]))
            if d and posixpath.join(d, "package.json") in paths:
                found.append(posixpath.join(d, "package.json"))
                break
    return found


def departures(tree) -> list[str]:
    found: set[str] = set()
    for toml in named(tree, "sylphx.toml"):
        data = parse_toml(tree.read(toml) or "")
        engine = (data.get("database", {}) or {}).get("migrations", {}) or {}
        engine = engine.get("engine") if isinstance(engine, dict) else None
        if isinstance(engine, str) and engine and engine.lower() != "atlas":
            found.add(f"migrations {toml} {engine}")
        base = posixpath.dirname(toml)
        for ref in dockerfiles(data):
            path = posixpath.normpath(ref)
            if tree.read(path) is None and base:
                path = posixpath.normpath(posixpath.join(base, ref))
            text = tree.read(path)
            if text is None:
                continue
            image, command = final_stage(text)
            if not image or not JS_RUNTIME_IMAGE.match(image_name(image)):
                continue
            if command and not command[0].endswith(".sh") and not any(
                    posixpath.basename(w) in JS_LAUNCHERS for w in command):
                continue  # e.g. an atlas migrator image built on a Bun base
            if any(deps(package(tree, pkg_path), "dependencies", "devDependencies") & WEB_FRAMEWORKS
                   for pkg_path in service_packages(tree, path, command)):
                continue
            found.add(f"bun-node-service {path}")
    for pkg_path in named(tree, "package.json"):
        for name in sorted(deps(package(tree, pkg_path), "dependencies") & SERVER_FRAMEWORKS):
            found.add(f"ts-server {pkg_path} {name}")
    return sorted(found)


def parse_baseline(text: str | None) -> set[str] | None:
    if text is None:
        return None
    entries = set()
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            entries.add(" ".join(line.split()))
    return entries


def decide(found: list[str], head_baseline: set[str] | None,
           base_baseline: set[str] | None) -> list[str]:
    """Return the errors; empty means the head conforms.

    A base without a baseline file (first adoption, or no base at all) accepts
    the head's baseline as the seed; after that the baseline may only shrink."""
    head = head_baseline or set()
    errors = []
    if base_baseline is not None:
        for line in sorted(head - base_baseline):
            errors.append(f"baseline grew: '{line}' is not in the base's baseline; a recorded "
                          "departure may only be removed, never added")
        allowed = head & base_baseline
    else:
        allowed = head
    for line in found:
        if line not in allowed:
            errors.append(f"new departure from the default stack: '{line}' (backend services are "
                          "Rust and schema is Atlas, SylphxAI/owner standards/stack.md)")
    for line in sorted(head - set(found)):
        errors.append(f"stale baseline line: '{line}' is no longer found; delete it so the "
                      "baseline keeps shrinking")
    return errors


def has_rev(repo: str, rev: str) -> bool:
    return subprocess.run(["git", "-C", repo, "cat-file", "-e", f"{rev}^{{commit}}"],
                          capture_output=True).returncode == 0


def cmd_check(args) -> int:
    head = Tree(args.repo, args.head)
    found = departures(head)
    head_baseline = parse_baseline(head.read(args.baseline))
    base_baseline = None
    if args.base:
        if not has_rev(args.repo, args.base):
            subprocess.run(["git", "-C", args.repo, "fetch", "--quiet", "--no-tags",
                            "--depth=1", "--filter=blob:none", "origin", args.base], check=False)
        base_baseline = parse_baseline(Tree(args.repo, args.base).read(args.baseline))
    errors = decide(found, head_baseline, base_baseline)
    for e in errors:
        print(f"::error::{e}")
    print(f"{len(found)} departure(s) found, {len(head_baseline or ())} recorded in {args.baseline}")
    if errors:
        print(f"Move the change onto the default stack. A departure is recorded only through a "
              f"company decision, by the owner of {args.baseline}.")
        return 1
    return 0


def cmd_scan(args) -> int:
    print("# Recorded departures from the default stack (SylphxAI/owner standards/stack.md).")
    print("# This list may only shrink; the stack-conformance check enforces it.")
    for line in departures(Tree(args.repo, args.rev)):
        print(line)
    return 0


def cmd_all(args) -> int:
    clean = 0
    for repo in args.dirs:
        found = departures(Tree(repo, args.rev))
        if not found:
            clean += 1
        print(f"{repo}\t{len(found)}\t{'; '.join(found)}")
    total = len(args.dirs)
    pct = f" ({100 * clean / total:.0f}%)" if total else ""
    print(f"Products fully on our own stack (stack conformance): {clean} of {total}{pct}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--repo", default=".")
    c.add_argument("--base", default="")
    c.add_argument("--head", default="HEAD")
    c.add_argument("--baseline", default=BASELINE)
    s = sub.add_parser("scan")
    s.add_argument("--repo", default=".")
    s.add_argument("--rev", default="HEAD")
    a = sub.add_parser("all")
    a.add_argument("--rev", default="HEAD")
    a.add_argument("dirs", nargs="+")
    args = p.parse_args(argv)
    return {"check": cmd_check, "scan": cmd_scan, "all": cmd_all}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
