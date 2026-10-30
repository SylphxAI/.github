#!/usr/bin/env python3
"""Expand/contract SQL guard, extracted from the shared product migration check.

Read Git objects rather than working-tree files. Merged migrations are immutable;
new contracting SQL needs a real line comment `-- contract: <released expand>`.
No database connection or SQL execution is used.
"""
from __future__ import annotations

import argparse
from collections import Counter
from difflib import SequenceMatcher
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
    raw: str = ""


class SqlError(ValueError):
    pass


WORD = re.compile(r"[\w$]+")
VARIABLE = re.compile(r"[\w.$]+")
DOLLAR = re.compile(r"\$(?:[A-Za-z_][\w]*)?\$")


def quoted_end(sql: str, start: int, dialect: str, line: int) -> int:
    quote, i = sql[start], start + 1
    escaped = quote != "`" and (dialect == "mysql" or (quote == "'" and start > 0 and sql[start - 1].upper() == "E"))
    while i < len(sql):
        if sql[i] == quote:
            if i + 1 < len(sql) and sql[i + 1] == quote:
                i += 2
                continue
            return i + 1
        i += 2 if escaped and sql[i] == "\\" else 1
    raise SqlError(f"line {line}: unterminated quoted token")


def lex(sql: str, dialect: str) -> tuple[list[Token], list[str]]:
    """Keep identifiers/literals distinct from keywords; comments cannot hide SQL.

    PostgreSQL supports nested block comments and dollar strings; MySQL supports
    backticks, backslash escapes and executable version comments. Inspectable
    routine bodies are scanned recursively; opaque dynamic SQL is refused.
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
                tokens.extend(Token(t.value, t.kind, line + t.line - 1, t.raw) for t in inner)
            line += sql[start:i].count("\n")
        elif dialect == "mysql" and ch == "@":
            start = i
            i += 2 if sql.startswith("@@", i) else 1
            if i < len(sql) and sql[i] in "'\"`":
                i = quoted_end(sql, i, dialect, line)
            else:
                variable = VARIABLE.match(sql, i)
                if not variable:
                    raise SqlError(f"line {line}: cannot inspect MySQL variable")
                i += len(variable[0])
            tokens.append(Token(sql[start:i], "literal", line, sql[start:i]))
            line += sql[start:i].count("\n")
        elif ch in "'\"`" or (dialect == "postgres" and ch == "$" and DOLLAR.match(sql, i)):
            start, start_line = i, line
            dollar = DOLLAR.match(sql, i) if ch == "$" else None
            if dollar:
                delim = dollar[0]
                end = sql.find(delim, i + len(delim))
                if end < 0:
                    raise SqlError(f"line {line}: unterminated dollar string")
                i = end + len(delim)
                kind, value = "literal", "<literal>"
            else:
                i = quoted_end(sql, start, dialect, line)
                kind = "literal" if ch == "'" or (dialect == "mysql" and ch == '"') else "identifier"
                value = sql[start + 1:i - 1].replace(ch + ch, ch) if kind == "identifier" else "<literal>"
            tokens.append(Token(value, kind, start_line, sql[start:i]))
            line += sql[start:i].count("\n")
        else:
            match = WORD.match(sql, i)
            value = match[0] if match else ch
            tokens.append(Token(value.upper(), "word" if match else "symbol", line, value))
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
    return token_violations(tokens, dialect)


def foreign_delete_actions(tokens: list[Token]) -> list[tuple[int, str]]:
    actions = []
    for i, token in enumerate(tokens):
        tail = tokens[i:]
        if starts(tail, "ON", "DELETE", "CASCADE"):
            actions.append((token.line, "CASCADE"))
        elif starts(tail, "ON", "DELETE", "SET", "NULL"):
            actions.append((token.line, "SET NULL"))
    return actions


def procedural_statement(stmt: list[Token]) -> list[Token]:
    """Expose SQL after a procedural control prefix without interpreting values."""
    if starts(stmt, "CREATE") or starts(stmt, "DO"):
        return stmt
    depth = 0
    commands = {"ALTER", "CREATE", "SET", "DO", "CALL", "EXECUTE", "PREPARE", "DELIMITER"}
    boundaries = {"BEGIN", "ATOMIC", "THEN", "ELSE", "LOOP"}
    for i, token in enumerate(stmt):
        if depth == 0 and token.kind == "word" and token.value in commands:
            after_control = i and stmt[i - 1].kind == "word" and stmt[i - 1].value in boundaries
            query_execution = kw(token, "EXECUTE") and i and any(kw(stmt[i - 1], w) for w in ("QUERY", "IN", "FOR"))
            if after_control or query_execution:
                return stmt[i:]
        if token.kind == "symbol":
            depth += (token.value == "(") - (token.value == ")")
    return stmt


def routine_kind(stmt: list[Token]) -> str | None:
    if starts(stmt, "DO"):
        return "DO"
    if starts(stmt, "CREATE"):
        for token in stmt[1:]:
            if token.kind != "word":
                continue
            if token.value in {"TABLE", "VIEW", "INDEX", "SCHEMA", "DATABASE", "SEQUENCE", "TYPE", "DOMAIN", "EXTENSION", "POLICY"}:
                break
            if token.value in {"FUNCTION", "PROCEDURE", "TRIGGER", "EVENT"}:
                return token.value
    return None


def routine_violations(stmt: list[Token], dialect: str, kind: str, nesting: int) -> list[tuple[int, str]]:
    opaque = [(stmt[0].line, "opaque procedural body requires a contract reference")]
    if kind in {"TRIGGER", "EVENT"} or dialect != "postgres":
        return opaque
    flat = top_level(stmt)
    language = "PLPGSQL" if kind == "DO" else None
    for i, token in enumerate(flat[:-1]):
        if kw(token, "LANGUAGE"):
            value = flat[i + 1]
            language = value.value.upper() if value.kind in {"word", "identifier"} else value.raw[1:-1].upper()
    if language not in {"SQL", "PLPGSQL"}:
        return opaque
    if kind == "DO":
        at = 3 if starts(flat[1:], "LANGUAGE") else 1
        body = flat[at] if at < len(flat) and flat[at].kind == "literal" else None
    else:
        body = next((flat[i + 1] for i, t in enumerate(flat[:-1]) if kw(t, "AS") and flat[i + 1].kind == "literal"), None)
    if body is not None:
        delimiter = DOLLAR.match(body.raw)
        if delimiter:
            source = body.raw[len(delimiter[0]):-len(delimiter[0])]
        elif body.raw.startswith("'"):
            source = body.raw[1:-1].replace("''", "'")
        else:
            return opaque
        inner, _ = lex(source, dialect)
        inner = [Token(t.value, t.kind, body.line + t.line - 1, t.raw) for t in inner]
        return token_violations(inner, dialect, nesting + 1)
    return opaque


def qualified_name(tokens: list[Token], at: int) -> tuple[tuple[str, str] | None, int]:
    """Keep schema/name boundaries distinct from dots in quoted identifiers."""
    if (at + 2 >= len(tokens) or tokens[at].kind not in {"word", "identifier"}
            or tokens[at + 1].value != "." or tokens[at + 2].kind not in {"word", "identifier"}
            or at + 3 < len(tokens) and tokens[at + 3].value == "."):
        return None, at
    parts = tuple(t.value if t.kind == "identifier" else t.value.lower() for t in (tokens[at], tokens[at + 2]))
    return parts, at + 3


def trigger_function_name(stmt: list[Token]) -> tuple[str, str] | None:
    """Identify an inspected zero-argument PostgreSQL trigger function."""
    at = 4 if starts(stmt, "CREATE", "OR", "REPLACE", "FUNCTION") else 2
    if not (starts(stmt, "CREATE", "FUNCTION") or starts(stmt, "CREATE", "OR", "REPLACE", "FUNCTION")):
        return None
    function, at = qualified_name(stmt, at)
    if (at + 3 < len(stmt) and stmt[at].value == "(" and stmt[at + 1].value == ")"
            and starts(stmt[at + 2:], "RETURNS", "TRIGGER")):
        return function
    return None


def additive_trigger(stmt: list[Token], created: set[tuple[str, str]], functions: set[tuple[str, str]]) -> bool:
    """Allow a simple INSERT trigger only on a definitely new local table."""
    if not starts(stmt, "CREATE", "TRIGGER"):
        return False
    _, at = name(stmt, 2)
    if at >= len(stmt) or not any(kw(stmt[at], word) for word in ("BEFORE", "AFTER")):
        return False
    if not starts(stmt[at + 1:], "INSERT", "ON"):
        return False
    table, at = qualified_name(stmt, at + 3)
    if table not in created or not starts(stmt[at:], "FOR", "EACH", "ROW", "EXECUTE", "FUNCTION"):
        return False
    function, at = qualified_name(stmt, at + 5)
    return (function in functions and len(stmt) == at + 2
            and stmt[at].value == "(" and stmt[at + 1].value == ")")


def token_violations(tokens: list[Token], dialect: str, nesting: int = 0) -> list[tuple[int, str]]:
    if nesting > 64:
        raise SqlError("procedural body nesting is too deep to inspect")
    out: list[tuple[int, str]] = []
    defaults: set[tuple[str, str]] = set()
    created: set[str] = set()
    trigger_functions: set[tuple[str, str]] = set()
    trigger_tables: set[tuple[str, str]] = set()
    statements = split(tokens, ";")
    uncertain_flow = nesting and any(t.kind == "word" and t.value in {"IF", "CASE", "LOOP", "EXCEPTION", "HANDLER", "ROLLBACK", "DO"} for t in tokens)
    for stmt in statements:
        if uncertain_flow:
            # A conditional/loop may not run, and an exception handler can
            # continue after failed DDL. Credit facts only within one SQL
            # statement, where its clauses execute together.
            defaults.clear()
            created.clear()
            trigger_tables.clear()
            trigger_functions.clear()
        control_stmt = procedural_statement(stmt)
        out.extend((line, f"ON DELETE {action} changes child data") for line, action in foreign_delete_actions(stmt))
        if starts(control_stmt, "SET") and any(
                (t.kind in {"word", "identifier"} and t.value.upper() in {"SQL_MODE", "STANDARD_CONFORMING_STRINGS"})
                or (t.value.startswith("@@") and re.search(r"\bsql_mode\b", t.value, re.I))
                for t in control_stmt):
            # Changing quote semantics would invalidate subsequent lexical
            # inspection; these statements need an explicit contract review.
            out.append((stmt[0].line, "SQL lexical mode change requires a contract reference"))
        for i, token in enumerate(stmt):
            tail = stmt[i:]
            if any(starts(tail, "DROP", obj) for obj in ("TABLE", "COLUMN", "SCHEMA", "DATABASE")):
                out.append((token.line, "DROP " + tail[1].value))
            if starts(tail, "DROP", "TEMPORARY", "TABLE"):
                out.append((token.line, "DROP TEMPORARY TABLE"))
            if any(starts(tail, "DROP", obj) for obj in ("OWNED", "TYPE", "DOMAIN")):
                out.append((token.line, "DROP " + tail[1].value))
            if kw(token, "TRUNCATE") and not (i + 1 < len(stmt) and stmt[i + 1].value == "("):
                out.append((token.line, "TRUNCATE"))
            if kw(token, "RENAME"):
                out.append((token.line, "RENAME"))
            # Each mutation needs its own top-level WHERE. A WHERE in a nested
            # SELECT, quoted string, or a second statement does not qualify.
            if (kw(token, "DELETE") or kw(token, "UPDATE")) and not (i and kw(stmt[i - 1], "ON")):
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
        stmt = control_stmt
        routine = routine_kind(stmt)
        if routine:
            inspected_trigger = dialect == "postgres" and routine == "TRIGGER" and additive_trigger(stmt, trigger_tables, trigger_functions)
            routine_errors = [] if inspected_trigger else routine_violations(stmt, dialect, routine, nesting)
            out.extend(routine_errors)
            function = trigger_function_name(stmt) if dialect == "postgres" and routine == "FUNCTION" else None
            if function and not routine_errors:
                trigger_functions.add(function)
            if routine == "DO":
                # Executed bodies can remove defaults established outside,
                # but their local facts never authorize enclosing statements.
                defaults.clear()
        if (starts(stmt, "CALL") or starts(stmt, "EXECUTE")
                or starts(stmt, "PREPARE") or starts(stmt, "DELIMITER")):
            out.append((stmt[0].line, "procedural or dynamic SQL requires a contract reference"))
        if starts(stmt, "CREATE", "TABLE"):
            conditional = starts(stmt[2:], "IF", "NOT", "EXISTS")
            at = 5 if conditional else 2
            table, _ = name(stmt, at)
            # IF NOT EXISTS may leave a live table in place, so subsequent
            # required-column additions still need a default.
            if not conditional:
                created.add(table)
                qualified, _ = qualified_name(stmt, at)
                if qualified:
                    trigger_tables.add(qualified)
        table_at = 2 if starts(stmt, "ALTER", "TABLE") else 3 if starts(stmt, "ALTER", "FOREIGN", "TABLE") else None
        if table_at is None:
            continue
        at = table_at + 2 if starts(stmt[table_at:], "IF", "EXISTS") else table_at
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
            safe_drops = ("CONSTRAINT",) if dialect == "postgres" else ("CONSTRAINT", "INDEX", "KEY", "PRIMARY", "FOREIGN", "CHECK")
            if kw(first, "DROP") and not any(starts(flat[1:], safe) for safe in safe_drops):
                out.append((first.line, "DROP COLUMN"))
            if starts(flat, "DISCARD", "TABLESPACE") or starts(flat, "CONVERT", "TO", "CHARACTER", "SET"):
                out.append((first.line, "table data discard or character set conversion"))
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
    errors = token_violations(tokens, dialect)
    return [] if markers else errors


class Column(NamedTuple):
    sql_type: tuple[tuple[str, str], ...]
    required: bool
    default: bool
    line: int


class Table(NamedTuple):
    columns: dict[str, Column]
    options: tuple[tuple[str, str], ...]
    foreign_actions: dict[tuple, int]
    line: int


def signature(tokens: list[Token], *, exact_words: bool = False) -> tuple[tuple[str, str], ...]:
    # Literal contents matter for enum definitions and changed data statements.
    # Preserve statement word spelling too: MySQL table names may be case-sensitive.
    return tuple((t.kind, t.raw if t.raw and (t.kind == "literal" or exact_words and t.kind == "word") else t.value) for t in tokens)


def snapshot_name(tokens: list[Token], at: int, dialect: str) -> tuple[tuple[str, ...], int]:
    parts = []
    while at < len(tokens) and tokens[at].kind in {"word", "identifier"}:
        token = tokens[at]
        parts.append(token.value if token.kind == "identifier" else (token.raw or token.value) if dialect == "mysql" else token.value.lower())
        at += 1
        if at >= len(tokens) or tokens[at].value != ".":
            break
        at += 1
    if not parts or at and tokens[at - 1].value == ".":
        raise SqlError("expected an explicit schema identifier")
    return tuple(parts), at


def table_start(stmt: list[Token]) -> int | None:
    if starts(stmt, "CREATE", "TABLE"):
        return 2
    if starts(stmt, "CREATE") and len(stmt) > 2 and any(kw(stmt[1], w) for w in ("TEMP", "TEMPORARY", "UNLOGGED", "FOREIGN")) and kw(stmt[2], "TABLE"):
        return 3
    return None


def group(tokens: list[Token], at: int) -> tuple[list[Token], int]:
    if at >= len(tokens) or tokens[at].value != "(":
        raise SqlError("schema snapshots require explicit CREATE TABLE column definitions")
    depth = 1
    for end in range(at + 1, len(tokens)):
        if tokens[end].kind == "symbol":
            depth += (tokens[end].value == "(") - (tokens[end].value == ")")
        if depth == 0:
            return tokens[at + 1:end], end + 1
    raise SqlError("unclosed schema definition")


def table_options(tokens: list[Token]) -> tuple[tuple[str, str], ...]:
    stable, at = [], 0
    while at < len(tokens):
        if any(kw(tokens[at], word) for word in ("AUTO_INCREMENT", "COMMENT")):
            at += 1
            if at < len(tokens) and tokens[at].value == "=":
                at += 1
            if at >= len(tokens):
                raise SqlError("missing table option value")
            at += 1
        else:
            stable.append(tokens[at])
            at += 1
    return signature(stable)


def snapshot_foreign_actions(definition: list[Token], dialect: str,
                             column: str | None = None) -> dict[tuple, int]:
    actions = foreign_delete_actions(definition)
    if not actions:
        return {}
    reference = next((i for i, t in enumerate(definition) if kw(t, "REFERENCES")), None)
    if reference is None:
        raise SqlError("foreign-key delete action has no explicit reference")
    target, at = snapshot_name(definition, reference + 1, dialect)
    def columns(parts):
        result = []
        for part in split(parts, ","):
            key, _ = snapshot_name(part, 0, dialect)
            if len(key) != 1:
                raise SqlError("foreign keys require explicit column names")
            result.append(key[0].casefold() if dialect == "mysql" else key[0])
        return tuple(result)
    referenced = ()
    if at < len(definition) and definition[at].value == "(":
        body, _ = group(definition, at)
        referenced = columns(body)
    if column is None:
        opening = next((i for i, t in enumerate(definition[:reference]) if t.value == "("), reference)
        body, _ = group(definition, opening)
        source = columns(body)
    else:
        source = (column,)
    return {(source, target, referenced, action): line for line, action in actions}


def snapshot(sql: str, dialect: str) -> tuple[dict[tuple[str, ...], Table], list[list[Token]], set[str]]:
    tokens, markers = lex(sql, dialect)
    statements = split(tokens, ";")
    tables = {}
    for stmt in statements:
        if starts(stmt, "SET"):
            mysql_mode = any(t.value.upper() == "SQL_MODE" or t.value.startswith("@@") and re.search(r"\bsql_mode\b", t.value, re.I) for t in stmt)
            postgres_mode = any(t.value.upper() == "STANDARD_CONFORMING_STRINGS" for t in stmt)
            if (mysql_mode and any(t.kind == "literal" and any(mode in t.raw.upper() for mode in ("NO_BACKSLASH_ESCAPES", "ANSI_QUOTES")) for t in stmt)
                    or postgres_mode and any(t.kind == "word" and t.value in {"OFF", "FALSE", "0"} or t.kind == "literal" and t.raw.strip("'\"").lower() in {"off", "false", "0"} for t in stmt)):
                raise SqlError("schema snapshots require default SQL quote semantics")
        drop_at = 2 if starts(stmt, "DROP", "TABLE") else 3 if starts(stmt, "DROP", "TEMPORARY", "TABLE") else None
        if drop_at is not None:
            if starts(stmt[drop_at:], "IF", "EXISTS"):
                drop_at += 2
            for part in split(stmt[drop_at:], ","):
                dropped, _ = snapshot_name(part, 0, dialect)
                if dropped in tables:
                    raise SqlError(f"DROP TABLE follows the CREATE definition for {'.'.join(dropped)}")
        if starts(stmt, "ALTER", "TABLE") or starts(stmt, "ALTER", "FOREIGN", "TABLE"):
            raise SqlError("schema snapshots must declare table structure in CREATE TABLE rather than ALTER TABLE")
        at = table_start(stmt)
        if at is None:
            if starts(stmt, "CREATE") and any(kw(t, "TABLE") for t in stmt[:5]):
                raise SqlError("unsupported CREATE TABLE header in schema snapshot")
            continue
        prefix = stmt[1:at - 1]
        if starts(stmt[at:], "IF", "NOT", "EXISTS"):
            at += 3
        table, at = snapshot_name(stmt, at, dialect)
        if table in tables:
            raise SqlError(f"duplicate CREATE TABLE definition for {'.'.join(table)}")
        body, end = group(stmt, at)
        if has(stmt[end:], "SELECT"):
            raise SqlError("schema snapshots require explicit declarations rather than CREATE TABLE AS queries")
        if has(stmt[end:], "INHERITS"):
            raise SqlError("inherited schema columns require explicit definitions")
        columns, primary, foreign_actions = {}, [], {}
        for definition in split(body, ","):
            constraint = definition
            if starts(constraint, "CONSTRAINT"):
                if len(constraint) < 3 or constraint[1].kind not in {"word", "identifier"}:
                    raise SqlError("invalid named table constraint")
                constraint = constraint[2:]
            table_constraint = any(starts(constraint, *words) for words in (
                ("PRIMARY", "KEY"), ("FOREIGN", "KEY"), ("UNIQUE",), ("CHECK",), ("EXCLUDE",)))
            if dialect == "mysql" and any(starts(constraint, word) for word in ("KEY", "INDEX", "FULLTEXT", "SPATIAL")):
                table_constraint = True
            if table_constraint:
                foreign_actions.update(snapshot_foreign_actions(constraint, dialect))
                if starts(constraint, "PRIMARY", "KEY"):
                    opening = next((i for i, t in enumerate(constraint) if t.value == "("), len(constraint))
                    key_columns, _ = group(constraint, opening)
                    for part in split(key_columns, ","):
                        key, _ = snapshot_name(part, 0, dialect)
                        if len(key) != 1:
                            raise SqlError("unsupported qualified primary key column")
                        primary.append(key[0].casefold() if dialect == "mysql" else key[0])
                continue
            if starts(definition, "LIKE") or starts(definition, "CONSTRAINT"):
                raise SqlError("schema columns require explicit names and types")
            column, at = snapshot_name(definition, 0, dialect)
            if len(column) != 1:
                raise SqlError("unsupported qualified column name")
            column_name = column[0].casefold() if dialect == "mysql" else column[0]
            foreign_actions.update(snapshot_foreign_actions(definition, dialect, column_name))
            if column_name in columns:
                raise SqlError(f"duplicate column {column_name}")
            depth, type_end = 0, len(definition)
            for i in range(at, len(definition)):
                token = definition[i]
                if depth == 0 and any(kw(token, word) for word in (
                        "NOT", "NULL", "DEFAULT", "PRIMARY", "UNIQUE", "REFERENCES", "CHECK", "CONSTRAINT", "GENERATED", "AUTO_INCREMENT", "COMMENT")):
                    type_end = i
                    break
                if token.kind == "symbol":
                    depth += (token.value == "(") - (token.value == ")")
            sql_type = definition[at:type_end]
            if not sql_type:
                raise SqlError(f"column {column_name} has no explicit type")
            rest = definition[type_end:]
            flat = top_level(rest)
            # MySQL COLLATE is a column attribute and can follow DEFAULT or
            # COMMENT, outside the leading type tokens.
            collation = []
            for i, token in enumerate(flat):
                if kw(token, "COLLATE"):
                    _, collation_end = snapshot_name(flat, i + 1, dialect)
                    collation.extend(flat[i:collation_end])
            generated = has(flat, "GENERATED") or has(flat, "AUTO_INCREMENT") or any(has(sql_type, t) for t in ("SERIAL", "BIGSERIAL", "SMALLSERIAL"))
            default_at = next((i for i, t in enumerate(rest) if kw(t, "DEFAULT")), None)
            default = generated or default_at is not None and default_at + 1 < len(rest) and not kw(rest[default_at + 1], "NULL")
            required = has(flat, "NOT", "NULL") or has(flat, "PRIMARY", "KEY") or generated and (has(flat, "IDENTITY") or has(flat, "AUTO_INCREMENT") or has(sql_type, "SERIAL") or has(sql_type, "BIGSERIAL") or has(sql_type, "SMALLSERIAL"))
            columns[column_name] = Column(signature(sql_type + collation), required, default, definition[0].line)
        for column in primary:
            if column not in columns:
                raise SqlError(f"primary key references undeclared column {column}")
            columns[column] = columns[column]._replace(required=True)
        tables[table] = Table(columns, signature(prefix) + table_options(stmt[end:]), foreign_actions, stmt[0].line)
    return tables, statements, set(markers)


def check_schema(old_sql: str, new_sql: str, dialect: str) -> list[tuple[int, str]]:
    old, old_statements, old_markers = snapshot(old_sql, dialect)
    if old_sql == new_sql:
        return []
    new, new_statements, new_markers = snapshot(new_sql, dialect)
    # A reference left in a mutable snapshot must not permanently exempt it.
    if new_markers - old_markers:
        return []
    errors = []
    for table, current in new.items():
        previous_actions = old[table].foreign_actions if table in old else {}
        for action, line in current.foreign_actions.items():
            if action not in previous_actions:
                errors.append((line, f"foreign key on {'.'.join(table)} introduces ON DELETE {action[-1]}, which changes child data"))
    for table, previous in old.items():
        label = '.'.join(table)
        if table not in new:
            errors.append((previous.line, f"table {label} was removed or renamed"))
            continue
        current = new[table]
        if previous.options != current.options:
            errors.append((current.line, f"table {label} options changed (storage or collation safety cannot be proved)"))
        for column, before in previous.columns.items():
            after = current.columns.get(column)
            if after is None:
                errors.append((current.line, f"column {label}.{column} was removed or renamed"))
            elif before.sql_type != after.sql_type:
                errors.append((after.line, f"column {label}.{column} type changed (narrowing cannot be excluded)"))
        for column, after in current.columns.items():
            before = previous.columns.get(column)
            if after.required and not after.default and (before is None or not before.required):
                errors.append((after.line, f"column {label}.{column} becomes NOT NULL without a DEFAULT"))
    # Include CREATE statements as anchors, so moving an old DROP past its
    # CREATE cannot make the destructive statement disappear in a multiset.
    changes = SequenceMatcher(a=[signature(s, exact_words=True) for s in old_statements],
                              b=[signature(s, exact_words=True) for s in new_statements], autojunk=False)
    inserted = []
    for tag, _, _, start, end in changes.get_opcodes():
        if tag in {"insert", "replace"}:
            for stmt in new_statements[start:end]:
                if table_start(stmt) is None:
                    inserted.extend([*stmt, Token(";", "symbol", stmt[-1].line)])
    errors.extend(token_violations(inserted, dialect))
    return sorted(set(errors))


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True)
    return result.stdout.decode("utf-8")


def directories(raw: str, *, allow_empty: bool = False) -> list[str]:
    paths = []
    for line in raw.splitlines():
        value = line.strip().rstrip("/")
        if not value:
            continue
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or any(ch in value for ch in "*?[]\\"):
            raise ValueError(f"SQL scope must be a repository-relative literal path: {value}")
        if str(path) not in paths:
            paths.append(str(path))
    if not paths and not allow_empty:
        raise ValueError("at least one migration directory is required")
    return paths


def tree_files(root: Path, ref: str) -> dict[str, tuple[str, str]]:
    files = {}
    for entry in git(root, "ls-tree", "-rz", ref).split("\0"):
        if not entry:
            continue
        metadata, path = entry.split("\t", 1)
        mode, kind, oid = metadata.split()
        files[path] = mode, oid
    return files


def version(path: str) -> int | None:
    match = re.match(r"(\d+)_", PurePosixPath(path).name)
    return int(match[1]) if match else None


def check_range(root: Path, base: str, head: str, dirs: list[str], dialect: str,
                schemas: list[str] | None = None) -> tuple[list[str], int]:
    # PRs can be behind main. Only their changes since the common ancestor
    # count; merge groups and pushes already have an ancestral base.
    ancestor = git(root, "merge-base", base, head).strip()
    old_tree, new_tree = tree_files(root, ancestor), tree_files(root, head)
    def migrations(tree):
        return {p: blob for p, blob in tree.items() if p.lower().endswith(".sql") and any(d == "." or p.startswith(d + "/") for d in dirs)}
    old, new = migrations(old_tree), migrations(new_tree)
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
    for path in schemas or []:
        before, after = old_tree.get(path), new_tree.get(path)
        if after is None:
            errors.append(f"{path}: configured schema file is missing or deleted")
            continue
        if any(blob and blob[0] not in {"100644", "100755"} for blob in (before, after)):
            errors.append(f"{path}: schema must be a regular SQL file")
            continue
        try:
            old_sql = git(root, "cat-file", "blob", before[1]) if before else ""
            new_sql = git(root, "cat-file", "blob", after[1])
            errors.extend(f"{path}:{line}: {reason}" for line, reason in check_schema(old_sql, new_sql, dialect))
        except (SqlError, UnicodeError) as exc:
            errors.append(f"{path}: cannot inspect schema: {exc}")
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
    parser.add_argument("--schema-file", action="append")
    args = parser.parse_args(argv)
    try:
        if args.dialect not in {"postgres", "mysql"}:
            raise ValueError("dialect must be postgres or mysql")
        dirs = directories("\n".join(args.directory) if args.directory else env.get("MIGRATION_DIRECTORIES", ""), allow_empty=True)
        schemas = directories("\n".join(args.schema_file) if args.schema_file else env.get("MIGRATION_SCHEMA_FILES", ""), allow_empty=True)
        if not dirs and not schemas:
            raise ValueError("at least one migration directory or schema file is required")
        base, head = event_range({**env, "MIGRATION_BASE": args.base, "MIGRATION_HEAD": args.head})
        root = Path(env.get("GITHUB_WORKSPACE", "."))
        errors, count = check_range(root, base, head, dirs, args.dialect, schemas)
        if errors:
            print("\n".join(errors))
            print("::error::Migration safety check failed. Merged migrations are immutable. Ship an expand step before contracting; add `-- contract: <reference>` naming that released step in the new migration or changed schema snapshot.")
            return 1
        print(f"Migration safety check passed ({count} new SQL migration file(s), {len(schemas)} schema snapshot(s) inspected; merged migrations unchanged).")
        return 0
    except (ValueError, OSError, UnicodeError, subprocess.CalledProcessError) as exc:
        print(f"::error::Migration safety check could not complete: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
