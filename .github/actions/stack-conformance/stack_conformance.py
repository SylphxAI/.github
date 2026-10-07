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

and two kinds of agent-runtime part, which only Sylphx Agents owns (SylphxAI/cloud
ADR-01M495TCCBZGSQ68A4P4G428H2, D5):

  agent-runtime-package <manifest> <name>  a Cargo or npm package named like
                                      *vault-proxy*, *egress-guard*,
                                      *credential-crypto* or *agent-harness*
  agent-runtime-table <file.sql> <table>   a SQL file creates a session-log,
                                      tool-call or agent-memory table that no
                                      later SQL file drops

and, in SQL files added since the base, a work-engine table that Work owns
unless the file's header says whose records it holds (SylphxAI/work
docs/adr/0010, "Guard for the class"):

  work-obligation-table <file.sql> <table>  a status, an assignee or role and
                                      a due, deadline, SLA, overdue or
                                      escalation column

A product repository never records an agent-runtime part in its own baseline:
the only allowance is policy/agent-runtime.json in SylphxAI/.github, one entry
per existing instance with an expiry date, after which the entry stops
applying. The platform owner's repository is not checked for them.

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
import datetime
import json
import os
import pathlib
import posixpath
import re
import subprocess
import sys

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11: the regex reader below is enough.
    tomllib = None

BASELINE = ".github/stack-departures.txt"

# The company-level allowance for agent-runtime parts: this repository's
# policy/agent-runtime.json, three levels above this action's directory.
AGENT_RUNTIME_POLICY = pathlib.Path(__file__).resolve().parents[3] / "policy" / "agent-runtime.json"

AGENT_RUNTIME = "agent-runtime-"
AGENT_RUNTIME_PACKAGE = re.compile(r"vault-proxy|egress-guard|credential-crypto|agent-harness")
AGENT_RUNTIME_TABLE = re.compile(r"^(session_entries|session_turns|tool_calls|agent_memory\w*|memory_entries)$")
# CREATE TABLE with optional IF NOT EXISTS and an optional, possibly quoted,
# schema qualifier; DROP TABLE with its comma-separated list.
_IDENT = r'(?:"[^"]+"|`[^`]+`|[A-Za-z_][\w$]*)'
SQL_CREATE = re.compile(
    r"^\s*CREATE\s+(?:(?:GLOBAL|LOCAL)\s+)?(?:(?:TEMP|TEMPORARY|UNLOGGED)\s+)?TABLE\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?((?:" + _IDENT + r"\s*\.\s*)?" + _IDENT + r")",
    re.IGNORECASE)
SQL_DROP = re.compile(r"^\s*DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?(.*?)(?:\s+(?:CASCADE|RESTRICT))?\s*$",
                      re.IGNORECASE | re.DOTALL)

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
    """The facts we need, read line by line: the migration engine, every
    `dockerfile` value and a Cargo `[package]` name. Used when tomllib is missing or the file does not parse."""
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
        if section == "package" and key == "name":
            data["package"] = {"name": value}
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


def package_name(norm: str) -> str:
    return norm.lower().replace("_", "-")


def sql_table(raw: str) -> str:
    last = re.split(r"\s*\.\s*", raw.strip())[-1]
    return last.strip('"`').lower()


def agent_runtime(tree) -> list[str]:
    """Agent-runtime parts in one tree: packages by name, and tables created by
    a SQL file and not dropped by a later one (files in path order, which is
    the order of timestamped migrations)."""
    found: set[str] = set()
    for manifest in named(tree, "Cargo.toml"):
        pkg = parse_toml(tree.read(manifest) or "").get("package")
        name = pkg.get("name") if isinstance(pkg, dict) else None
        if isinstance(name, str) and AGENT_RUNTIME_PACKAGE.search(package_name(name)):
            found.add(f"agent-runtime-package {manifest} {name}")
    for manifest in named(tree, "package.json"):
        name = package(tree, manifest).get("name")
        if isinstance(name, str) and AGENT_RUNTIME_PACKAGE.search(package_name(name)):
            found.add(f"agent-runtime-package {manifest} {name}")
    live: dict[str, str] = {}
    for path in sorted(p for p in tree.paths() if p.lower().endswith(".sql") and not skipped(p)):
        text = re.sub(r"/\*.*?\*/", "", re.sub(r"--[^\n]*", "", tree.read(path) or ""), flags=re.DOTALL)
        for statement in text.split(";"):
            created = SQL_CREATE.match(statement)
            if created:
                table = sql_table(created.group(1))
                if AGENT_RUNTIME_TABLE.match(table):
                    live.setdefault(table, path)
                continue
            dropped = SQL_DROP.match(statement)
            if dropped:
                for raw in dropped.group(1).split(","):
                    live.pop(sql_table(raw), None)
    for table, path in live.items():
        found.add(f"agent-runtime-table {path} {table}")
    return sorted(found)


# A hand-rolled work engine (SylphxAI/work docs/adr/0010, "Guard for the
# class"): a new table with a status, an assignee or role, and a due date,
# deadline, SLA, overdue or escalation column. Its migration's header must say
# whose records the rows are: the Work resource they feed, or the product's
# customers' records (ADR 0010 D1).
WORK_DECISION = "SylphxAI/work docs/adr/0010-group-companies-on-work.md"
WORK_RESOURCE_HEADER = re.compile(r"\bwork[ \t]+resource[ \t]*:[ \t]*[^\s*/]", re.IGNORECASE)
CUSTOMER_RECORDS_HEADER = "customer records (work adr 0010 d1)"
_CONSTRAINT_WORDS = frozenset({"constraint", "primary", "unique", "foreign", "check", "exclude",
                               "like", "index", "key", "period"})


def _status_word(words: list[str]) -> bool:
    return any(w in ("status", "state", "stage") for w in words)


def _assignee_word(words: list[str]) -> bool:
    return any(w in ("role", "assigned", "owner", "executor", "executing", "handler", "responsible")
               or w.startswith("assignee") for w in words)


def _due_word(words: list[str]) -> bool:
    return any(w in ("due", "sla", "overdue") or w.startswith(("deadline", "escalat"))
               for w in words)


def _split_top(body: str) -> list[str]:
    parts, depth, cur = [], 0, []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def table_columns(statement: str) -> tuple[str, list[str]] | None:
    """The table and column names of one CREATE TABLE statement, or None."""
    created = SQL_CREATE.match(statement)
    if not created:
        return None
    rest = statement[created.end():].lstrip()
    if not rest.startswith("("):
        return None  # CREATE TABLE ... AS / PARTITION OF: no column list here
    depth, end = 0, None
    for i, ch in enumerate(rest):
        depth += ch == "("
        depth -= ch == ")"
        if depth == 0:
            end = i
            break
    columns = []
    for element in _split_top(rest[1:end if end is not None else len(rest)]):
        m = re.match(r"\s*(\"[^\"]+\"|`[^`]+`|[A-Za-z_][\w$]*)", element)
        if not m:
            continue
        name = m.group(1)
        if name[0] not in '"`' and name.lower() in _CONSTRAINT_WORDS:
            continue
        columns.append(name.strip('"`').lower())
    return sql_table(created.group(1)), columns


def is_obligation_table(columns: list[str]) -> bool:
    words = [c.split("_") for c in columns]
    return (any(_status_word(w) for w in words) and any(_assignee_word(w) for w in words)
            and any(_due_word(w) for w in words))


def header_names_owner(text: str) -> bool:
    """True when the comment lines before the file's first statement name the
    Work resource the table feeds or say it holds customers' records."""
    header, in_block = [], False
    for raw in text.splitlines():
        line = raw.strip()
        if in_block:
            header.append(line)
            in_block = "*/" not in line
        elif line.startswith("--"):
            header.append(line)
        elif line.startswith("/*"):
            header.append(line)
            in_block = "*/" not in line
        elif line:
            break
    words = [re.sub(r"^(?:--|/\*|\*(?!/))+|\*/$", "", line).strip() for line in header]
    return any(WORK_RESOURCE_HEADER.search(w) for w in words) or CUSTOMER_RECORDS_HEADER in " ".join(
        " ".join(words).lower().split())


def obligation_tables(path: str, text: str) -> list[str]:
    """`work-obligation-table <path> <table>` for each work-engine table that a
    SQL file creates without a header saying whose records its rows are. A
    down migration only restores an earlier schema and is not checked."""
    if path.lower().endswith(".down.sql") or header_names_owner(text):
        return []
    body = re.sub(r"/\*.*?\*/", "", re.sub(r"--[^\n]*", "", text), flags=re.DOTALL)
    found = []
    for statement in body.split(";"):
        parsed = table_columns(statement)
        if parsed and is_obligation_table(parsed[1]):
            found.append(f"work-obligation-table {path} {parsed[0]}")
    return found


def added_sql(repo: str, base: str, head: str) -> list[str]:
    """SQL files added between base and head (renames are not additions)."""
    out = git(repo, "diff", "--name-only", "--diff-filter=A", "-M", "-z", base, head, "--")
    return sorted(p for p in out.split("\0")
                  if p and p.lower().endswith(".sql") and not skipped(p))


def decide_obligations(found: list[str]) -> list[str]:
    return [f"new work-engine table: '{line}' has a status, an assignee or role and a due date, "
            "deadline, SLA or escalation. A company's own work (what its people or agents owe) "
            "runs on Work: create items there and name the resource in the migration's header "
            "('-- Work resource: <resource>'). If the rows are the product's customers' records, "
            "say so in the header ('-- customer records (Work ADR 0010 D1)'). "
            f"({WORK_DECISION}, D1)" for line in found]


def load_agent_runtime_policy(path=AGENT_RUNTIME_POLICY) -> dict:
    try:
        return json.loads(pathlib.Path(path).read_text())
    except FileNotFoundError:
        return {"owner_repos": [], "baseline": []}


def decide_agent_runtime(found: list[str], repository: str, policy: dict,
                         today: datetime.date) -> tuple[list[str], list[str]]:
    """Errors and notices for the agent-runtime parts of one repository.

    The platform owner is not checked. Elsewhere a part passes only while
    policy/agent-runtime.json lists it for this repository and its expiry date
    has not passed."""
    if repository in policy.get("owner_repos", []):
        return [], []
    allowed: dict[str, str] = {}
    for entry in policy.get("baseline", []):
        if entry.get("repo") == repository:
            allowed[" ".join(entry["departure"].split())] = entry["expires"]
    decision = policy.get("decision", "the Sylphx Agents decision")
    errors, notices = [], []
    for line in found:
        expires = allowed.get(line)
        if expires is None:
            errors.append(f"new agent-runtime part: '{line}'. Agent definitions, sessions, memory, "
                          f"the tool gateway, credential injection and the egress guard belong to "
                          f"Sylphx Agents; use its API instead ({decision})")
        elif today > datetime.date.fromisoformat(expires):
            errors.append(f"agent-runtime allowance expired on {expires}: '{line}'. Move it onto "
                          f"Sylphx Agents and delete it, or extend its date in SylphxAI/.github "
                          f"policy/agent-runtime.json with a reason ({decision})")
    for line in sorted(set(allowed) - set(found)):
        notices.append(f"agent-runtime allowance '{line}' is no longer found; delete its entry in "
                       "SylphxAI/.github policy/agent-runtime.json")
    return errors, notices


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
    errors = []
    for line in sorted(l for l in (head_baseline or ()) if l.startswith(AGENT_RUNTIME)):
        errors.append(f"'{line}' is in {args.baseline}; an agent-runtime part is allowed only by "
                      "SylphxAI/.github policy/agent-runtime.json, with an expiry date")
        head_baseline.discard(line)
    base_baseline = None
    if args.base:
        if not has_rev(args.repo, args.base):
            subprocess.run(["git", "-C", args.repo, "fetch", "--quiet", "--no-tags",
                            "--depth=1", "--filter=blob:none", "origin", args.base], check=False)
        base_baseline = parse_baseline(Tree(args.repo, args.base).read(args.baseline))
        if base_baseline is not None:
            base_baseline = {l for l in base_baseline if not l.startswith(AGENT_RUNTIME)}
    errors += decide(found, head_baseline, base_baseline)
    runtime = agent_runtime(head)
    policy = load_agent_runtime_policy(args.agent_runtime_policy)
    today = datetime.date.fromisoformat(args.today) if args.today else datetime.datetime.now(
        datetime.timezone.utc).date()
    runtime_errors, notices = decide_agent_runtime(runtime, args.repository, policy, today)
    errors += runtime_errors
    obligations: list[str] = []
    if args.base:
        for path in added_sql(args.repo, args.base, args.head):
            obligations += obligation_tables(path, head.read(path) or "")
    errors += decide_obligations(obligations)
    for n in notices:
        print(f"::notice::{n}")
    for e in errors:
        print(f"::error::{e}")
    print(f"{len(found)} departure(s) found, {len(head_baseline or ())} recorded in {args.baseline}; "
          f"{len(runtime)} agent-runtime part(s) found in {args.repository or 'this repository'}; "
          f"{len(obligations)} new work-engine table(s) without an owner header")
    if errors:
        print(f"Move the change onto the default stack. A departure is recorded only through a "
              f"company decision, by the owner of {args.baseline}.")
        return 1
    return 0


def cmd_scan(args) -> int:
    print("# Recorded departures from the default stack (SylphxAI/owner standards/stack.md).")
    print("# This list may only shrink; the stack-conformance check enforces it.")
    tree = Tree(args.repo, args.rev)
    for line in departures(tree):
        print(line)
    for line in agent_runtime(tree):
        print(f"# not recordable here (SylphxAI/.github policy/agent-runtime.json): {line}")
    return 0


def cmd_all(args) -> int:
    clean = 0
    for repo in args.dirs:
        tree = Tree(repo, args.rev)
        found = departures(tree) + agent_runtime(tree)
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
    c.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""),
                   help="owner/name of the checked repository (default $GITHUB_REPOSITORY)")
    c.add_argument("--agent-runtime-policy", default=str(AGENT_RUNTIME_POLICY))
    c.add_argument("--today", default="", help="YYYY-MM-DD; default today in UTC (tests)")
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
