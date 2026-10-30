#!/usr/bin/env python3
"""Expand/contract SQL guard, extracted from the shared product migration check.

Read Git objects rather than working-tree files. Merged migrations are immutable;
new contracting SQL needs a real line comment `-- contract: <released expand>`.
No database connection or SQL execution is used.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from typing import NamedTuple


class Token(NamedTuple):
    value: str
    kind: str
    line: int


class SqlError(ValueError):
    pass


def lex(sql: str, dialect: str) -> tuple[list[Token], list[str]]:
    """Keep identifiers/literals distinct from keywords; comments cannot hide SQL.

    PostgreSQL supports nested block comments and dollar strings; MySQL supports
    backticks, backslash escapes and executable version comments. Opaque SQL in
    procedural statements is refused separately, rather than assumed harmless.
    """
    tokens, markers = [], []
    i, line = 0, 1
    while i < len(sql):
        ch = sql[i]
        if ch.isspace():
            line += ch == "\n"
            i += 1
        elif (sql.startswith("--", i) and (dialect == "postgres" or i + 2 == len(sql) or sql[i + 2].isspace())) or (dialect == "mysql" and ch == "#"):
            end = sql.find("\n", i)
            end = len(sql) if end < 0 else end
            comment = sql[i:end]
            # A marker is a comment on its own line, never text inside a string.
            prefix = sql[sql.rfind("\n", 0, i) + 1:i]
            match = re.fullmatch(r"--[ \t]*contract:[ \t]*(\S.*?)[ \t]*", comment, re.I)
            if not prefix.strip() and match and re.search(r"\w", match[1]):
                markers.append(match[1])
            i = end
        elif sql.startswith("/*", i):
            start, depth = i, 1
            executable = dialect == "mysql" and sql.startswith(("/*!", "/*M!"), i)
            i += 2
            while i < len(sql) and depth:
                if dialect == "postgres" and sql.startswith("/*", i):
                    depth += 1
                    i += 2
                elif sql.startswith("*/", i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
            if depth:
                raise SqlError(f"line {line}: unterminated block comment")
            if executable:
                body = re.sub(r"^(?:!|M!)\d*\s*", "", sql[start + 2:i - 2])
                inner, _ = lex(body, dialect)
                tokens.extend(Token(t.value, t.kind, line + t.line - 1) for t in inner)
            line += sql[start:i].count("\n")
        elif ch in "'\"`" or (dialect == "postgres" and ch == "$" and re.match(r"\$(?:[A-Za-z_][\w]*)?\$", sql[i:])):
            start, start_line = i, line
            dollar = re.match(r"\$(?:[A-Za-z_][\w]*)?\$", sql[i:]) if ch == "$" else None
            if dollar:
                delim = dollar[0]
                end = sql.find(delim, i + len(delim))
                if end < 0:
                    raise SqlError(f"line {line}: unterminated dollar string")
                i = end + len(delim)
                kind, value = "literal", "<literal>"
            else:
                i += 1
                while i < len(sql):
                    if sql[i] == ch:
                        if i + 1 < len(sql) and sql[i + 1] == ch:
                            i += 2
                            continue
                        i += 1
                        break
                    if sql[i] == "\\" and (dialect == "mysql" or (start > 0 and sql[start - 1:start].upper() == "E")):
                        i += 2
                    else:
                        i += 1
                else:
                    raise SqlError(f"line {line}: unterminated quoted token")
                kind = "literal" if ch == "'" or (dialect == "mysql" and ch == '"') else "identifier"
                value = sql[start + 1:i - 1].replace(ch + ch, ch) if kind == "identifier" else "<literal>"
            tokens.append(Token(value, kind, start_line))
            line += sql[start:i].count("\n")
        else:
            match = re.match(r"[\w$]+", sql[i:])
            value = match[0] if match else ch
            tokens.append(Token(value.upper(), "word" if match else "symbol", line))
            i += len(value)
    return tokens, markers


def kw(token: Token, value: str) -> bool:
    return token.kind == "word" and token.value == value


def starts(tokens: list[Token], *words: str) -> bool:
    return len(tokens) >= len(words) and all(kw(t, w) for t, w in zip(tokens, words))


def has(tokens: list[Token], *words: str) -> bool:
    return any(starts(tokens[i:], *words) for i in range(len(tokens)))


def split(tokens: list[Token], separator: str) -> list[list[Token]]:
    parts, start, depth = [], 0, 0
    for i, token in enumerate(tokens):
        if token.kind == "symbol":
            depth += (token.value == "(") - (token.value == ")")
            if depth < 0:
                raise SqlError(f"line {token.line}: unmatched closing parenthesis")
            if token.value == separator and depth == 0:
                parts.append(tokens[start:i])
                start = i + 1
    if depth:
        raise SqlError("unclosed parenthesis")
    parts.append(tokens[start:])
    return [part for part in parts if part]


def top_level(tokens: list[Token]) -> list[Token]:
    depth, out = 0, []
    for token in tokens:
        if token.kind == "symbol" and token.value == "(":
            depth += 1
        elif token.kind == "symbol" and token.value == ")":
            depth -= 1
        elif depth == 0:
            out.append(token)
    return out


def name(tokens: list[Token], at: int) -> tuple[str, int]:
    parts = []
    while at < len(tokens) and tokens[at].kind in {"word", "identifier"}:
        parts.append(tokens[at].value if tokens[at].kind == "identifier" else tokens[at].value.lower())
        at += 1
        if at >= len(tokens) or tokens[at].value != ".":
            break
        at += 1
    return ".".join(parts), at


def violations(sql: str, dialect: str) -> list[tuple[int, str]]:
    tokens, _ = lex(sql, dialect)
    out: list[tuple[int, str]] = []
    defaults: set[tuple[str, str]] = set()
    created: set[str] = set()
    for stmt in split(tokens, ";"):
        for i, token in enumerate(stmt):
            tail = stmt[i:]
            if any(starts(tail, "DROP", obj) for obj in ("TABLE", "COLUMN", "SCHEMA", "DATABASE")):
                out.append((token.line, "DROP " + tail[1].value))
            if starts(tail, "DROP", "TEMPORARY", "TABLE"):
                out.append((token.line, "DROP TEMPORARY TABLE"))
            if kw(token, "TRUNCATE") and not (i + 1 < len(stmt) and stmt[i + 1].value == "("):
                out.append((token.line, "TRUNCATE"))
            if kw(token, "RENAME"):
                out.append((token.line, "RENAME"))
            # Each mutation needs its own top-level WHERE. A WHERE in a nested
            # SELECT, quoted string, or a second statement does not qualify.
            if kw(token, "DELETE") or (kw(token, "UPDATE") and not (i and kw(stmt[i - 1], "ON"))):
                # Stop at the enclosing CTE/subquery boundary as well as the
                # statement end. A later CTE's WHERE cannot qualify this one.
                suffix, depth = [], 0
                for following in tail:
                    if following.kind == "symbol" and following.value == ")":
                        depth -= 1
                        if depth < 0:
                            break
                    if depth == 0:
                        suffix.append(following)
                    if following.kind == "symbol" and following.value == "(":
                        depth += 1
                lock_or_upsert = kw(token, "UPDATE") and i and any(kw(stmt[i - 1], w) for w in ("FOR", "KEY", "ON", "DO"))
                if not lock_or_upsert and not has(suffix, "WHERE"):
                    out.append((token.line, token.value + " without WHERE"))
        if (starts(stmt, "DO") or starts(stmt, "CALL") or starts(stmt, "EXECUTE")
                or starts(stmt, "PREPARE") or starts(stmt, "DELIMITER")
                or (starts(stmt, "CREATE") and any(has(stmt[:8], kind) for kind in ("FUNCTION", "PROCEDURE", "TRIGGER")))):
            out.append((stmt[0].line, "procedural or dynamic SQL requires a contract reference"))
        if starts(stmt, "CREATE", "TABLE"):
            at = 5 if starts(stmt[2:], "IF", "NOT", "EXISTS") else 2
            table, _ = name(stmt, at)
            created.add(table)
        if not starts(stmt, "ALTER", "TABLE"):
            continue
        at = 4 if starts(stmt[2:], "IF", "EXISTS") else 2
        if at < len(stmt) and kw(stmt[at], "ONLY"):
            at += 1
        table, at = name(stmt, at)
        clauses = []
        for clause in split(stmt[at:], ","):
            pos = 2 if starts(clause, "ADD", "COLUMN") else 1
            if starts(clause, "ADD") and len(clause) > pos and clause[pos].value == "(" and clause[-1].value == ")":
                # MySQL also accepts ADD (a int, b int NOT NULL).
                clauses.extend([[clause[0], *part] for part in split(clause[pos + 1:-1], ",")])
            else:
                clauses.append(clause)
        for clause in clauses:
            flat = top_level(clause)
            if not flat:
                continue
            first = flat[0]
            if kw(first, "DROP") and not any(starts(flat[1:], safe) for safe in (
                    "CONSTRAINT", "DEFAULT", "NOT", "IDENTITY", "EXPRESSION", "INDEX", "KEY", "PRIMARY", "FOREIGN", "CHECK")):
                out.append((first.line, "DROP COLUMN"))
            if kw(first, "MODIFY") or kw(first, "CHANGE"):
                # Without the previous type no widening can be proved. This
                # also covers MySQL CHANGE's implicit column rename.
                out.append((first.line, "column type change or rename"))
            if kw(first, "ALTER"):
                at = 2 if starts(flat[1:], "COLUMN") else 1
                column, at = name(flat, at)
                rest = flat[at:]
                key = table, column
                if starts(rest, "TYPE") or starts(rest, "SET", "DATA", "TYPE"):
                    out.append((first.line, "column type change (narrowing cannot be excluded)"))
                if starts(rest, "SET", "DEFAULT"):
                    defaults.add(key)
                if starts(rest, "DROP", "DEFAULT"):
                    defaults.discard(key)
                if starts(rest, "SET", "NOT", "NULL") and table not in created and key not in defaults:
                    out.append((first.line, "SET NOT NULL without a DEFAULT"))
            if kw(first, "ADD"):
                at = 2 if starts(flat[1:], "COLUMN") else 1
                if starts(flat[at:], "IF", "NOT", "EXISTS"):
                    at += 3
                if at < len(flat) and any(kw(flat[at], value) for value in ("CONSTRAINT", "PRIMARY", "UNIQUE", "FOREIGN", "CHECK", "EXCLUDE", "INDEX", "KEY", "VALUE")):
                    continue
                column, at = name(flat, at)
                rest = flat[at:]
                default = any(has(rest, key) for key in ("DEFAULT", "GENERATED", "IDENTITY", "SERIAL", "BIGSERIAL", "SMALLSERIAL", "AUTO_INCREMENT"))
                if default:
                    defaults.add((table, column))
                if table not in created and has(rest, "NOT", "NULL") and not default:
                    out.append((first.line, "ADD COLUMN NOT NULL without a DEFAULT"))
    return sorted(set(out))


def check_sql(sql: str, dialect: str) -> list[tuple[int, str]]:
    tokens, markers = lex(sql, dialect)
    # Lexical failures remain errors even when a marker is present.
    return [] if markers else violations(sql, dialect)


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True)
    return result.stdout.decode("utf-8")


def directories(raw: str) -> list[str]:
    paths = []
    for line in raw.splitlines():
        value = line.strip().rstrip("/")
        if not value:
            continue
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or any(ch in value for ch in "*?[]\\"):
            raise ValueError(f"migration directory must be a repository-relative literal path: {value}")
        paths.append(str(path))
    if not paths:
        raise ValueError("at least one migration directory is required")
    return paths


def migration_files(root: Path, ref: str, dirs: list[str]) -> dict[str, tuple[str, str]]:
    files = {}
    for entry in git(root, "ls-tree", "-rz", ref).split("\0"):
        if not entry:
            continue
        metadata, path = entry.split("\t", 1)
        mode, kind, oid = metadata.split()
        if path.lower().endswith(".sql") and any(d == "." or path.startswith(d + "/") for d in dirs):
            files[path] = mode, oid
    return files


def version(path: str) -> int | None:
    match = re.match(r"(\d+)_", PurePosixPath(path).name)
    return int(match[1]) if match else None


def check_range(root: Path, base: str, head: str, dirs: list[str], dialect: str) -> tuple[list[str], int]:
    # PRs can be behind main. Only their changes since the common ancestor
    # count; merge groups and pushes already have an ancestral base.
    ancestor = git(root, "merge-base", base, head).strip()
    old = migration_files(root, ancestor, dirs)
    new = migration_files(root, head, dirs)
    errors = []
    for path, blob in old.items():
        if new.get(path) != blob:
            errors.append(f"{path}: merged migration was modified, deleted or renamed; append a new migration instead")
    added = sorted(new.keys() - old.keys())
    tops: dict[str, int] = {}
    for path in old:
        v = version(path)
        parent = str(PurePosixPath(path).parent)
        if v is not None:
            tops[parent] = max(tops.get(parent, -1), v)
    versions = Counter((str(PurePosixPath(p).parent), version(p)) for p in added if version(p) is not None)
    for path in added:
        mode, oid = new[path]
        if mode not in {"100644", "100755"}:
            errors.append(f"{path}: migration must be a regular SQL file")
            continue
        v, parent = version(path), str(PurePosixPath(path).parent)
        if v is not None and (v <= tops.get(parent, -1) or versions[parent, v] > 1):
            errors.append(f"{path}: migration version must be unique and above {tops.get(parent, -1)} already on the base")
        try:
            sql = git(root, "cat-file", "blob", oid)
            errors.extend(f"{path}:{line}: {reason}" for line, reason in check_sql(sql, dialect))
        except (SqlError, UnicodeError) as exc:
            errors.append(f"{path}: cannot inspect SQL: {exc}")
    return errors, len(added)


def event_range(env: dict[str, str]) -> tuple[str, str]:
    event = json.loads(Path(env["GITHUB_EVENT_PATH"]).read_text()) if env.get("GITHUB_EVENT_PATH") else {}
    pr, group = event.get("pull_request", {}), event.get("merge_group", {})
    base = env.get("MIGRATION_BASE") or pr.get("base", {}).get("sha") or group.get("base_sha") or event.get("before", "")
    head = env.get("MIGRATION_HEAD") or pr.get("head", {}).get("sha") or group.get("head_sha") or env.get("GITHUB_SHA", "HEAD")
    if not base or not base.strip("0"):
        raise ValueError("no comparison base; supply base-ref and fetch its history (checkout fetch-depth: 0)")
    return base, head


def main(argv: list[str] | None = None, env: dict[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dialect", choices=("postgres", "mysql"), default=env.get("MIGRATION_DIALECT"))
    parser.add_argument("--base", default=env.get("MIGRATION_BASE", ""))
    parser.add_argument("--head", default=env.get("MIGRATION_HEAD", ""))
    parser.add_argument("--directory", action="append")
    args = parser.parse_args(argv)
    try:
        if args.dialect not in {"postgres", "mysql"}:
            raise ValueError("dialect must be postgres or mysql")
        dirs = directories("\n".join(args.directory) if args.directory else env.get("MIGRATION_DIRECTORIES", ""))
        base, head = event_range({**env, "MIGRATION_BASE": args.base, "MIGRATION_HEAD": args.head})
        root = Path(env.get("GITHUB_WORKSPACE", "."))
        errors, count = check_range(root, base, head, dirs, args.dialect)
        if errors:
            print("\n".join(errors))
            print("::error::Migration safety check failed. Merged files are immutable. Ship an expand step before contracting; a new contract migration must name that released step in `-- contract: <reference>`.")
            return 1
        print(f"Migration safety check passed ({count} new SQL migration file(s) inspected; merged files unchanged).")
        return 0
    except (ValueError, OSError, UnicodeError, subprocess.CalledProcessError) as exc:
        print(f"::error::Migration safety check could not complete: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
