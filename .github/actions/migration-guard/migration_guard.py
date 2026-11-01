#!/usr/bin/env python3
"""Expand/contract SQL guard, extracted from the shared product migration check.

Read Git objects rather than working-tree files. Merged migrations are immutable;
new contracting SQL needs a real line comment `-- contract: <released expand>`.
No database connection or SQL execution is used.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
from difflib import SequenceMatcher
import hashlib
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
        # Bare format can resolve to a user overload even with the default
        # search path. A routine-local catalog-only path removes that ambiguity.
        catalog_format = kind != "DO" and catalog_only_search_path(flat)
        if any(t.kind in {"word", "identifier"} and t.value.lower() in {
                "search_path", "set_config", "reset", "discard", "do"} for t in inner):
            catalog_format = False
        for i in range(len(inner)):
            function, end = qualified_name(inner, i)
            if function and function[0] != "pg_catalog" and end < len(inner) and inner[end].value == "(":
                # A user function can alter the caller's path. Do not infer
                # its effects from a secured caller's routine header.
                catalog_format = False
        finite_executes = (
            finite_query_executes(inner, dialect, catalog_format, nesting + 1)
            | check_widening_executes(inner, dialect)
        )
        return token_violations(inner, dialect, nesting + 1, catalog_format, finite_executes)
    return opaque


def unquote_postgres_literal(token: Token) -> str | None:
    if token.kind != "literal":
        return None
    raw = token.raw
    dollar = DOLLAR.match(raw)
    if dollar:
        return raw[len(dollar[0]):-len(dollar[0])]
    if raw.startswith("'"):
        return raw[1:-1].replace("''", "'")
    return None


def strip_control_prefix(stmt: list[Token]) -> list[Token]:
    at = 0
    while at < len(stmt) and stmt[at].kind == "word" and stmt[at].value in {"BEGIN", "LOOP", "THEN", "ELSE"}:
        at += 1
    return stmt[at:]


POSITIVE_TIMEOUT = re.compile(
    r"\s*[1-9]\d*\s*(?:us|ms|s|sec|secs|second|seconds|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)?\s*$",
    re.I,
)


def timeout_setting(stmt: list[Token]) -> tuple[str, bool] | None:
    """Return (setting_name, is_positive_bound) for SET/RESET/DISCARD/set_config timeout statements."""
    if starts(stmt, "DISCARD", "ALL") or starts(stmt, "RESET", "ALL"):
        return ("*", False)
    if starts(stmt, "RESET") and len(stmt) == 2 and stmt[1].kind in {"word", "identifier"}:
        key = stmt[1].value.lower()
        if key in {"lock_timeout", "statement_timeout"}:
            return (key, False)
        return None
    if (starts(stmt, "SELECT") or starts(stmt, "PERFORM")) and len(stmt) == 9:
        if (stmt[1].kind in {"word", "identifier"} and stmt[1].value.lower() == "set_config"
                and stmt[2].value == "(" and stmt[4].value == "," and stmt[6].value == "," and stmt[8].value == ")"):
            key = unquote_postgres_literal(stmt[3])
            if key and key.lower() in {"lock_timeout", "statement_timeout"}:
                text = unquote_postgres_literal(stmt[5])
                return (key.lower(), bool(text and POSITIVE_TIMEOUT.match(text)))
        return None
    if not starts(stmt, "SET"):
        return None
    at = 2 if len(stmt) > 1 and stmt[1].kind == "word" and stmt[1].value in {"SESSION", "LOCAL"} else 1
    if at >= len(stmt) or stmt[at].kind not in {"word", "identifier"}:
        return None
    key = stmt[at].value.lower()
    if key not in {"lock_timeout", "statement_timeout"}:
        return None
    tail = stmt[at + 1:]
    if len(tail) != 2 or not (tail[0].value == "=" or kw(tail[0], "TO")):
        return (key, False)
    val = tail[1]
    if val.kind == "literal":
        text = unquote_postgres_literal(val)
        return (key, bool(text and POSITIVE_TIMEOUT.match(text)))
    if val.kind == "word":
        return (key, bool(re.fullmatch(r"[1-9]\d*", val.raw or val.value)))
    return (key, False)


def normalize_index_target(index_name: str, table_name: str = "") -> tuple[str, str] | None:
    if not index_name or index_name.count(".") > 1 or table_name.count(".") > 1:
        return None
    if "." in index_name:
        schema, short = index_name.split(".", 1)
        if "." in table_name and table_name.split(".", 1)[0] != schema:
            return None
        return (schema, short)
    if "." in table_name:
        schema, _ = table_name.split(".", 1)
        return (schema, index_name)
    return ("public", index_name)


def concurrent_index_target(stmt: list[Token]) -> tuple[str, tuple[str, str] | None, bool] | None:
    """Return (kind, (schema, index_name), conditional) for concurrent index DDL."""
    if starts(stmt, "DROP", "INDEX", "CONCURRENTLY"):
        if not starts(stmt[3:], "IF", "EXISTS"):
            return ("DROP", None, False)
        idx, at = name(stmt, 5)
        if not idx or (at < len(stmt) and not (at + 1 == len(stmt) and kw(stmt[at], "RESTRICT"))):
            return ("DROP", None, True)
        return ("DROP", normalize_index_target(idx), True)
    at = 3 if starts(stmt, "CREATE", "UNIQUE", "INDEX", "CONCURRENTLY") else 2 if starts(stmt, "CREATE", "INDEX", "CONCURRENTLY") else None
    if at is None:
        return None
    at += 1
    conditional = starts(stmt[at:], "IF", "NOT", "EXISTS")
    if conditional:
        at += 3
    idx, at = name(stmt, at)
    if not idx or not starts(stmt[at:], "ON"):
        return ("CREATE", None, conditional)
    at += 1
    if at < len(stmt) and kw(stmt[at], "ONLY"):
        at += 1
    tbl, _ = name(stmt, at)
    if not tbl:
        return ("CREATE", None, conditional)
    return ("CREATE", normalize_index_target(idx, tbl), conditional)


def check_widening_executes(inner: list[Token], dialect: str) -> set[int]:
    """Recognize catalog-backed CHECK (col = ANY (ARRAY[%s])) NOT VALID widenings."""
    if dialect != "postgres":
        return set()
    if any(t.kind in {"word", "identifier"} and t.value.lower() in {"search_path", "reset", "discard", "do"} for t in inner):
        return set()
    for i, token in enumerate(inner):
        if token.kind in {"word", "identifier"} and token.value.lower() == "set_config":
            if i + 2 >= len(inner) or inner[i + 1].value != "(" or unquote_postgres_literal(inner[i + 2]) != "lock_timeout":
                return set()
        function, end = qualified_name(inner, i)
        if function and function[0] != "pg_catalog" and end < len(inner) and inner[end].value == "(":
            return set()
    statements = split(inner, ";")
    declared_text: set[str] = set()
    declared_text_array: set[str] = set()
    in_declare = False
    for stmt in statements:
        decl = stmt
        if starts(decl, "DECLARE"):
            in_declare = True
            decl = decl[1:]
        if not in_declare:
            break
        if any(kw(t, "BEGIN") for t in decl):
            break
        if len(decl) == 2 and decl[0].kind == "word" and kw(decl[1], "TEXT"):
            declared_text.add(decl[0].value)
        elif len(decl) == 4 and decl[0].kind == "word" and kw(decl[1], "TEXT") and [t.value for t in decl[2:]] == ["[", "]"]:
            declared_text_array.add(decl[0].value)
    colon_assigned = {
        inner[i].value
        for i in range(len(inner) - 2)
        if inner[i].kind == "word" and [t.value for t in inner[i + 1:i + 3]] == [":", "="]
    }
    for_vars = {
        inner[i + 1].value
        for i in range(len(inner) - 1)
        if kw(inner[i], "FOR") and inner[i + 1].kind == "word"
    }
    into_counts = Counter(
        inner[i + 1].value
        for i in range(len(inner) - 1)
        if kw(inner[i], "INTO") and inner[i + 1].kind == "word"
    )
    single_text = {v for v in declared_text - colon_assigned - for_vars if into_counts[v] == 1}
    single_array = {v for v in declared_text_array - colon_assigned - for_vars if into_counts[v] == 1}
    catalog_defs: dict[str, tuple[str, str]] = {}
    kind_arrays: dict[str, tuple[str, str]] = {}
    kind_lists: dict[str, tuple[str, str]] = {}
    for stmt in statements:
        cs = strip_control_prefix(stmt)
        if (len(cs) == 20 and starts(cs, "SELECT", "PG_GET_CONSTRAINTDEF")
                and [t.value for t in cs[2:6]] == ["(", "OID", ")", "INTO"]
                and cs[6].value in single_text
                and [t.value for t in cs[7:12]] == ["FROM", "PG_CONSTRAINT", "WHERE", "CONNAME", "="]
                and [t.value for t in cs[13:16]] == ["AND", "CONRELID", "="]
                and [t.value for t in cs[17:20]] == [":", ":", "REGCLASS"]):
            conname = unquote_postgres_literal(cs[12])
            conrel = unquote_postgres_literal(cs[16])
            if conname and conrel:
                try:
                    rel_tokens, _ = lex(conrel, dialect)
                    rel_name, rel_end = name(rel_tokens, 0)
                except SqlError:
                    continue
                if rel_name and rel_end == len(rel_tokens):
                    catalog_defs[cs[6].value] = (rel_name, conname.lower())
        elif (len(cs) == 28 and starts(cs, "SELECT", "ARRAY_AGG")
              and [t.value for t in cs[2:16]] == ["(", "DISTINCT", "M", "[", "1", "]", "ORDER", "BY", "M", "[", "1", "]", ")", "INTO"]
              and cs[16].value in single_array
              and [t.value for t in cs[17:20]] == ["FROM", "REGEXP_MATCHES", "("]
              and cs[20].value in catalog_defs
              and cs[21].value == "," and cs[22].kind == "literal"
              and cs[23].value == "," and cs[24].kind == "literal"
              and [t.value for t in cs[25:28]] == [")", "AS", "M"]):
            kind_arrays[cs[16].value] = catalog_defs[cs[20].value]
        elif starts(cs, "SELECT", "STRING_AGG") and len(cs) >= 25:
            if [t.value for t in cs[2:5]] != ["(", "QUOTE_LITERAL", "("] or cs[5].kind != "word":
                continue
            alias = cs[5].value
            if [t.value for t in cs[6:8]] != [")", ","]:
                continue
            sep = unquote_postgres_literal(cs[8])
            if sep is None or sep.strip() != ",":
                continue
            at = 9
            if [t.value for t in cs[at:at + 3]] == ["ORDER", "BY", alias]:
                at += 3
            if [t.value for t in cs[at:at + 2]] != [")", "INTO"] or at + 2 >= len(cs):
                continue
            list_var = cs[at + 2].value
            if list_var not in single_text:
                continue
            tail = cs[at + 3:]
            if (len(tail) < 11 or [t.value for t in tail[:3]] != ["FROM", "UNNEST", "("]
                    or tail[3].value not in kind_arrays
                    or [t.value for t in tail[4:8]] != ["|", "|", "ARRAY", "["]
                    or [t.value for t in tail[-4:]] != ["]", ")", "AS", alias]
                    or len({alias, list_var, tail[3].value}) != 3):
                continue
            items = split(tail[8:-4], ",")
            if items and all(len(p) == 1 and bool(unquote_postgres_literal(p[0])) for p in items):
                kind_lists[list_var] = kind_arrays[tail[3].value]
    accepted: set[int] = set()
    preceding_drop: tuple[str, str] | None = None
    for stmt in statements:
        cs = procedural_statement(stmt)
        if starts(cs, "ALTER", "TABLE"):
            tbl, at = name(cs, 2)
            if tbl and len(cs) == at + 3 and starts(cs[at:], "DROP", "CONSTRAINT"):
                cname, cend = name(cs, at + 2)
                if cname and cend == len(cs):
                    preceding_drop = (tbl, cname)
                    continue
        at = 4 if (starts(cs, "EXECUTE", "PG_CATALOG") and len(cs) > 3 and cs[2].value == "." and kw(cs[3], "FORMAT")) else 2
        if (starts(cs, "EXECUTE", "FORMAT") or at == 4) and len(cs) >= at + 5 and cs[at].value == "(" and cs[-1].value == ")":
            args = split(cs[at + 1:-1], ",")
            if (len(args) == 2 and len(args[1]) == 1 and args[1][0].kind == "word"
                    and args[1][0].value in kind_lists and args[0]):
                expected_table, expected_constraint = kind_lists[args[1][0].value]
                parts: list[str] = []
                valid_literals = True
                for idx, tok in enumerate(args[0]):
                    val = unquote_postgres_literal(tok)
                    if val is None or (len(args[0]) > 1 and not tok.raw.startswith("'")):
                        valid_literals = False
                        break
                    if idx > 0 and tok.line <= args[0][idx - 1].line:
                        valid_literals = False
                        break
                    parts.append(val)
                if valid_literals:
                    template = "".join(parts)
                    if template.count("%s") == 1 and "%" not in template.replace("%s", ""):
                        try:
                            cmd, _ = lex(template.replace("%s", "'__guard_check_element__'"), dialect)
                            cmd_stmts = split(cmd, ";")
                        except SqlError:
                            cmd_stmts = []
                        if len(cmd_stmts) == 1 and starts(cmd_stmts[0], "ALTER", "TABLE"):
                            tbl, pos = name(cmd_stmts[0], 2)
                            clauses = split(cmd_stmts[0][pos:], ",")
                            add_clause = None
                            if len(clauses) == 2 and len(clauses[0]) == 3 and starts(clauses[0], "DROP", "CONSTRAINT"):
                                drop_name, _ = name(clauses[0], 2)
                                if drop_name == expected_constraint:
                                    add_clause = clauses[1]
                            elif len(clauses) == 1 and preceding_drop == (expected_table, expected_constraint):
                                add_clause = clauses[0]
                            if tbl == expected_table and add_clause and len(add_clause) >= 15 and starts(add_clause, "ADD", "CONSTRAINT"):
                                cname, cpos = name(add_clause, 2)
                                rest = add_clause[cpos:]
                                if (cname == expected_constraint and len(rest) == 14
                                        and kw(rest[0], "CHECK") and rest[1].value == "("
                                        and rest[2].kind in {"word", "identifier"}
                                        and [t.value for t in rest[3:8]] == ["=", "ANY", "(", "ARRAY", "["]
                                        and rest[8].kind == "literal"
                                        and [t.value for t in rest[9:14]] == ["]", ")", ")", "NOT", "VALID"]):
                                    accepted.add(id(cs[0]))
        preceding_drop = None
    return accepted


def catalog_only_search_path(header: list[Token]) -> bool:
    settings = [i for i, t in enumerate(header[:-1]) if kw(t, "SET")
                and header[i + 1].value.lower() == "search_path"]
    if len(settings) != 1:
        return False
    tail = header[settings[0] + 2:]
    if not tail or not (tail[0].value == "=" or kw(tail[0], "TO")):
        return False
    at, schemas = 1, []
    while at < len(tail):
        token = tail[at]
        if token.kind not in {"word", "identifier"}:
            return False
        schemas.append(token.value if token.kind == "identifier" else token.value.lower())
        at += 1
        if at >= len(tail) or tail[at].value != ",":
            break
        at += 1
    options = {"AS", "LANGUAGE", "SET", "TRANSFORM", "WINDOW", "IMMUTABLE", "STABLE",
               "VOLATILE", "LEAKPROOF", "NOT", "CALLED", "RETURNS", "STRICT", "SECURITY",
               "PARALLEL", "COST", "ROWS", "SUPPORT"}
    return (schemas in [["pg_catalog"], ["pg_catalog", "pg_temp"]]
            and (at == len(tail) or tail[at].kind == "word" and tail[at].value in options))


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


def constant_acl_execute(stmt: list[Token], dialect: str, catalog_format: bool = False) -> bool:
    """Inspect fixed ACL commands whose only substitution is a quoted role."""
    at = 4 if (starts(stmt, "EXECUTE", "PG_CATALOG") and len(stmt) > 3
               and stmt[2].value == "." and kw(stmt[3], "FORMAT")) else 2
    if (dialect != "postgres" or at == 2 and not (catalog_format and starts(stmt, "EXECUTE", "FORMAT"))
            or len(stmt) < at + 5 or stmt[at].value != "(" or stmt[-1].value != ")"):
        return False
    args = split(stmt[at + 1:-1], ",")
    if len(args) != 2 or len(args[0]) != 1 or args[0][0].kind != "literal":
        return False
    role, end = name(args[1], 0)
    if not role or end != len(args[1]):
        return False
    raw = args[0][0].raw
    dollar = DOLLAR.match(raw)
    if dollar:
        template = raw[len(dollar[0]):-len(dollar[0])]
    elif raw.startswith("'"):
        template = raw[1:-1].replace("''", "'")
    else:
        return False
    if template.count("%I") != 1 or "%" in template.replace("%I", ""):
        return False
    prefix, suffix = template.split("%I")
    if not re.fullmatch(r"\s*;?\s*", suffix):
        return False
    # Parse the prefix separately: a literal identifier can never impersonate
    # the placeholder, and %I cannot choose a schema or function target.
    try:
        command, _ = lex(prefix, dialect)
        quoted, _ = lex(prefix + '""', dialect)
    except SqlError:
        return False
    # Quoting the substituted role must add exactly one complete identifier.
    # An open line comment could otherwise swallow its opening quote and let
    # a newline inside the role change how the resulting command is parsed.
    if (len(quoted) != len(command) + 1 or quoted[:-1] != command
            or quoted[-1].kind != "identifier" or quoted[-1].raw != '""'):
        return False
    command.append(Token("%I", "role_placeholder", stmt[0].line))
    if len(command) < 7:
        return False
    grant, revoke = starts(command, "GRANT"), starts(command, "REVOKE")
    if not (grant or revoke) or not kw(command[-2], "TO" if grant else "FROM"):
        return False
    if starts(command[1:], "USAGE", "ON", "SCHEMA"):
        return len(command) == 7 and command[4].kind in {"word", "identifier"}
    if not starts(command[1:], "EXECUTE", "ON", "FUNCTION"):
        return False
    function, at = qualified_name(command, 4)
    signature = command[at:-2]
    depth = 0
    for i, token in enumerate(signature):
        if token.kind == "symbol":
            depth += (token.value == "(") - (token.value == ")")
        if depth <= 0 and i != len(signature) - 1:
            return False
    return bool(function and len(signature) >= 2 and signature[0].value == "("
                and signature[-1].value == ")" and depth == 0 and all(
                    t.kind in {"word", "identifier"} or t.value in {"(", ")", ",", ".", "[", "]"}
                    for t in signature))


def finite_read_query(sql: str, dialect: str, nesting: int, *, predicate: bool = False) -> bool:
    """One syntactically read-only query, with a small builtin call vocabulary.

    This is not a planner: relation definitions and column/operator types remain
    the database's responsibility, just as they do for static report queries.
    """
    tokens, _ = lex(sql, dialect)
    statements = split(tokens, ";")
    if len(statements) != 1 or not any(starts(tokens, w) for w in ("SELECT", "WITH")):
        return False
    forbidden = {"INSERT", "UPDATE", "DELETE", "MERGE", "INTO", "RETURNING", "CALL",
                 "EXECUTE", "COPY", "CREATE", "ALTER", "DROP", "TRUNCATE", "GRANT",
                 "REVOKE", "SELECTINTO", "LOCK", "FOR"}
    if any(t.kind == "word" and t.value in forbidden for t in tokens):
        return False
    calls = {"isfinite", "to_regclass"} if predicate else {"min", "max", "count", "sum", "avg", "bool_and", "bool_or", "coalesce",
             "nullif", "greatest", "least", "split_part", "to_timestamp", "extract"}
    types = {"text", "date", "timestamp", "timestamptz", "bigint", "integer", "int", "int4",
             "int8", "smallint", "boolean", "bool", "numeric", "decimal", "real", "float8"}
    for i in range(len(tokens) - 2):
        if [t.value for t in tokens[i:i + 2]] == [":", ":"]:
            target, _ = name(tokens, i + 2)
            if target.removeprefix("pg_catalog.") not in types:
                return False
    # Typed literals can invoke a user type's input function without ().
    literal_prefixes = {"SELECT", "WHEN", "THEN", "ELSE", "ZONE", "LIKE", "ILIKE",
                        "ESCAPE", "FROM", "BETWEEN", "AND", "OR", "IS"}
    for i, token in enumerate(tokens):
        if token.kind != "literal" or not i or tokens[i - 1].kind not in {"word", "identifier"}:
            continue
        before = tokens[i - 1]
        if before.kind == "word" and before.value in literal_prefixes:
            continue
        typed = before.value if before.kind == "identifier" else before.value.lower()
        if typed not in types:
            return False
        if i > 1 and tokens[i - 2].value == ".":
            qualified, _ = qualified_name(tokens, i - 3)
            if qualified != ("pg_catalog", typed):
                return False
    cte_columns: set[int] = set()
    if starts(tokens, "WITH"):
        at = 1
        while at < len(tokens):
            if tokens[at].kind not in {"word", "identifier"}:
                return False
            cte = at
            at += 1
            if at < len(tokens) and tokens[at].value == "(":
                columns, at = group(tokens, at)
                if any(len(part) != 1 or part[0].kind not in {"word", "identifier"}
                       for part in split(columns, ",")):
                    return False
                cte_columns.add(cte)
            if not starts(tokens[at:], "AS"):
                return False
            _, at = group(tokens, at + 1)
            if at >= len(tokens) or tokens[at].value != ",":
                break
            at += 1
        if not starts(tokens[at:], "SELECT"):
            return False
    syntax = {"AS", "IN", "EXISTS", "FILTER", "OVER", "ON", "LATERAL", "FROM"}
    for i, token in enumerate(tokens[:-1]):
        if tokens[i + 1].value != "(" or token.kind not in {"word", "identifier"}:
            continue
        qualified_call = i and tokens[i - 1].value == "."
        if i in cte_columns or token.kind == "word" and token.value in syntax and not qualified_call:
            continue
        function = token.value if token.kind == "identifier" else token.value.lower()
        if function not in calls:
            return False
        if i and tokens[i - 1].value == ".":
            qualified, _ = qualified_name(tokens, i - 2)
            if qualified != ("pg_catalog", function):
                return False
    return not token_violations(tokens, dialect, nesting)


def finite_query_executes(tokens: list[Token], dialect: str, catalog_format: bool,
                          nesting: int) -> set[int]:
    """Recognize a complete, bounded, local text-construction program.

    Unsupported syntax invalidates the whole proof. Every IF arm is explored;
    no condition is assumed true, and an uninitialized reaching value fails.
    Only one assignment per local per path is supported (a literal declaration
    default may precede it). Loops, handlers, nested blocks and SQL assignments
    cannot silently mutate a credited query. Bounds fail closed.
    """
    if dialect != "postgres" or not catalog_format:
        return set()
    limit, size_limit = 64, 1_000_000
    at = 0
    initial: dict[str, str | None] = {}

    def fail():
        raise ValueError("not a finite literal query program")

    def expression(parts, values):
        result, pos = "", 0
        while pos < len(parts):
            token = parts[pos]
            if token.kind == "literal":
                raw = token.raw
                dollar = DOLLAR.match(raw)
                value = raw[len(dollar[0]):-len(dollar[0])] if dollar else raw[1:-1].replace("''", "'") if raw.startswith("'") else None
            elif token.kind == "word":
                value = values.get(token.value)
            else:
                value = None
            if value is None:
                fail()
            result += value
            if len(result) > size_limit:
                fail()
            pos += 1
            if pos < len(parts):
                if [t.value for t in parts[pos:pos + 2]] != ["|", "|"]:
                    fail()
                pos += 2
                if pos == len(parts):
                    fail()
        if not parts:
            fail()
        return result

    def through(word):
        nonlocal at
        start, depth = at, 0
        while at < len(tokens):
            token = tokens[at]
            if depth == 0 and (token.value == word if word == ";" else kw(token, word)):
                parts = tokens[start:at]
                at += 1
                return parts
            if token.kind == "symbol":
                depth += (token.value == "(") - (token.value == ")")
            at += 1
        fail()

    def sequence(depth=0):
        nonlocal at
        if depth > 32:
            fail()
        nodes = []
        while at < len(tokens) and not any(kw(tokens[at], w) for w in ("END", "ELSE", "ELSIF")):
            if kw(tokens[at], "IF"):
                branches = []
                while kw(tokens[at], "IF") or kw(tokens[at], "ELSIF"):
                    at += 1
                    condition = through("THEN")
                    if not condition or any(t.kind == "word" and t.value in {
                            "CASE", "LOOP", "EXECUTE", "SELECT", "INTO", "UPDATE", "DELETE", "INSERT"} for t in condition):
                        fail()
                    if not finite_read_query("SELECT " + " ".join(t.raw for t in condition),
                                             dialect, nesting, predicate=True):
                        fail()
                    branches.append(sequence(depth + 1))
                    if at >= len(tokens):
                        fail()
                otherwise = []
                if kw(tokens[at], "ELSE"):
                    at += 1
                    otherwise = sequence(depth + 1)
                if not starts(tokens[at:], "END", "IF") or at + 2 >= len(tokens) or tokens[at + 2].value != ";":
                    fail()
                at += 3
                nodes.append(("if", branches + [otherwise]))
            else:
                nodes.append(("statement", through(";")))
        return nodes

    accepted: set[int] = set()
    generated: set[str] = set()

    def evaluate(nodes, states):
        for kind, node in nodes:
            if kind == "if":
                states = [state for branch in node for state in evaluate(branch, [(dict(v), set(w)) for v, w in states])]
            else:
                part = node
                if not part:
                    fail()
                if len(part) >= 4 and part[0].kind == "word" and [t.value for t in part[1:3]] == [":", "="]:
                    key = part[0].value
                    for values, written in states:
                        if key not in values or key in written:
                            fail()
                        values[key] = expression(part[3:], values)
                        written.add(key)
                elif starts(part, "RETURN", "QUERY", "EXECUTE"):
                    if (len(part) < 4 or part[3].kind != "word" or part[3].value not in initial):
                        fail()
                    if len(part) > 4:
                        if not kw(part[4], "USING") or len(part) < 6:
                            fail()
                        args = split(part[5:], ",")
                        if any(len(arg) != 1 or arg[0].kind != "word" for arg in args):
                            fail()
                    for values, _ in states:
                        query = values[part[3].value]
                        if query is None:
                            fail()
                        generated.add(query)
                        if len(generated) > limit or sum(map(len, generated)) > size_limit:
                            fail()
                        if not finite_read_query(query, dialect, nesting):
                            fail()
                    # An unreachable statement is still checked by the normal
                    # scanner; only credit an EXECUTE with a reaching state.
                    if states:
                        accepted.add(id(part[2]))
                elif len(part) == 1 and kw(part[0], "RETURN"):
                    states = []
                elif starts(part, "RAISE", "EXCEPTION") and len(part) == 3 and part[2].kind == "literal":
                    states = []
                elif len(part) == 1 and kw(part[0], "NULL"):
                    pass
                else:
                    fail()
            if len(states) > limit:
                fail()
        return states

    try:
        if at < len(tokens) and kw(tokens[at], "DECLARE"):
            at += 1
            while at < len(tokens) and not kw(tokens[at], "BEGIN"):
                declaration = through(";")
                if len(declaration) < 2 or declaration[0].kind != "word" or not kw(declaration[1], "TEXT"):
                    fail()
                key = declaration[0].value
                if key in initial:
                    fail()
                initial[key] = None
                if len(declaration) > 2:
                    if [t.value for t in declaration[2:4]] != [":", "="] or len(declaration) != 5 or declaration[4].kind != "literal":
                        fail()
                    initial[key] = expression(declaration[4:], {})
        if at >= len(tokens) or not kw(tokens[at], "BEGIN"):
            fail()
        at += 1
        nodes = sequence()
        if not starts(tokens[at:], "END") or any(t.value != ";" for t in tokens[at + 1:]):
            fail()
        evaluate(nodes, [(initial.copy(), set())])
        return accepted
    except (ValueError, IndexError, RecursionError):
        return set()


def token_violations(tokens: list[Token], dialect: str, nesting: int = 0,
                     catalog_format: bool = False, finite_executes: set[int] | None = None) -> list[tuple[int, str]]:
    if nesting > 64:
        raise SqlError("procedural body nesting is too deep to inspect")
    out: list[tuple[int, str]] = []
    defaults: set[tuple[str, str]] = set()
    created: set[str] = set()
    trigger_functions: set[tuple[str, str]] = set()
    trigger_tables: set[tuple[str, str]] = set()
    timeouts: set[str] = set()
    dropped_concurrent_indexes: set[tuple[str, str]] = set()
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
            timeouts.clear()
            dropped_concurrent_indexes.clear()
        control_stmt = procedural_statement(stmt)
        if starts(control_stmt, "ALTER") and has(control_stmt, "SET", "SCHEMA"):
            # A relocated object can leave its old qualified name available
            # for an existing table or an uninspected function to occupy.
            trigger_tables.clear()
            trigger_functions.clear()
            dropped_concurrent_indexes.clear()
        if dialect == "postgres":
            if (any(t.kind in {"word", "identifier"} and t.value.lower() == "search_path" for t in control_stmt)
                    or has(control_stmt, "RESET", "ALL")):
                dropped_concurrent_indexes.clear()
            timeout = timeout_setting(control_stmt)
            if timeout:
                param, valid = timeout
                if param == "*":
                    timeouts.clear()
                elif valid:
                    timeouts.add(param)
                else:
                    timeouts.discard(param)
            concurrent = concurrent_index_target(control_stmt)
            if concurrent:
                kind, target, conditional = concurrent
                if not {"lock_timeout", "statement_timeout"} <= timeouts:
                    out.append((control_stmt[0].line, f"{kind} INDEX CONCURRENTLY requires preceding non-zero lock_timeout and statement_timeout"))
                if kind == "DROP":
                    if target is not None:
                        dropped_concurrent_indexes.add(target)
                else:
                    if conditional:
                        out.append((
                            control_stmt[0].line,
                            "CREATE INDEX CONCURRENTLY IF NOT EXISTS skips an invalid index shell left by a timed-out build; "
                            "use DROP INDEX CONCURRENTLY IF EXISTS before unconditional CREATE INDEX CONCURRENTLY",
                        ))
                    if target is None or target not in dropped_concurrent_indexes:
                        out.append((
                            control_stmt[0].line,
                            "CREATE INDEX CONCURRENTLY requires a preceding DROP INDEX CONCURRENTLY IF EXISTS for the same index",
                        ))
                    elif target is not None:
                        dropped_concurrent_indexes.discard(target)
            elif has(control_stmt, "CONCURRENTLY"):
                if not {"lock_timeout", "statement_timeout"} <= timeouts:
                    out.append((control_stmt[0].line, "CONCURRENTLY DDL requires preceding non-zero lock_timeout and statement_timeout"))
                if starts(control_stmt, "CREATE") and has(control_stmt, "INDEX"):
                    out.append((
                        control_stmt[0].line,
                        "CREATE INDEX CONCURRENTLY requires a preceding DROP INDEX CONCURRENTLY IF EXISTS for the same index",
                    ))
        out.extend((line, f"ON DELETE {action} changes child data") for line, action in foreign_delete_actions(stmt))
        if starts(control_stmt, "SET") and any(
                (t.kind in {"word", "identifier"} and t.value.upper() in {"SQL_MODE", "STANDARD_CONFORMING_STRINGS"})
                or (t.value.startswith("@@") and re.search(r"\bsql_mode\b", t.value, re.I))
                for t in control_stmt):
            # Changing quote semantics would invalidate subsequent lexical
            # inspection; these statements need an explicit contract review.
            out.append((stmt[0].line, "SQL lexical mode change requires a contract reference"))
        non_dml_on = (starts(control_stmt, "GRANT") or starts(control_stmt, "REVOKE")
                      or routine_kind(control_stmt) == "TRIGGER")
        on_index = next((j for j, t in enumerate(stmt) if kw(t, "ON")), 0) if non_dml_on else 0
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
            if (kw(token, "DELETE") or kw(token, "UPDATE")) and i >= on_index and not (i and kw(stmt[i - 1], "ON")):
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
        if dialect == "postgres" and any(starts(stmt, "ALTER", kind) for kind in ("FUNCTION", "PROCEDURE", "ROUTINE")):
            function_name, _ = name(stmt, 2)
            if function_name.split(".")[-1] == "format":
                out.append((stmt[0].line, "formatter redefinition requires a contract reference"))
            if (any(t.kind in {"word", "identifier"} and t.value.lower() == "search_path" for t in stmt)
                    or has(stmt, "RESET", "ALL")):
                out.append((stmt[0].line, "routine search path change requires a contract reference"))
        routine = routine_kind(stmt)
        if routine:
            if routine == "FUNCTION":
                at = 4 if starts(stmt, "CREATE", "OR", "REPLACE", "FUNCTION") else 2
                function_name, _ = name(stmt, at)
                if function_name.split(".")[-1] == "format":
                    out.append((stmt[0].line, "formatter redefinition requires a contract reference"))
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
                trigger_tables.clear()
                trigger_functions.clear()
                timeouts.clear()
                dropped_concurrent_indexes.clear()
        if (starts(stmt, "CALL") or starts(stmt, "EXECUTE") and id(stmt[0]) not in (finite_executes or set())
                and not constant_acl_execute(stmt, dialect, catalog_format)
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


CONCURRENT_INDEX_MARKER = "-- migration:concurrent-index-v1 "
CONCURRENT_INDEX_RUNTIME = "atlas-qualified-v1"


def concurrent_index_violations(sql: str, tokens: list[Token], dialect: str,
                                runtime: str) -> list[tuple[int, str]]:
    """A declaration generates the entire supported statement, not an exemption.

    Earlier SET/DROP statements do not survive Atlas partial resume. A caller
    adopts this contract only with a runner that bounds every new connection,
    qualifies the exact interrupted CREATE, and validates the resulting index.
    """
    if dialect != "postgres":
        return []
    creates = [t.line for i, t in enumerate(tokens) if starts(tokens[i:], "CREATE", "INDEX", "CONCURRENTLY")
               or starts(tokens[i:], "CREATE", "UNIQUE", "INDEX", "CONCURRENTLY")]
    header, body = [], []
    for line in sql.splitlines():
        if not body and (not line.strip() or line.lstrip().startswith("--")):
            header.append(line.strip())
        else:
            body.append(line)
    declarations = [line[len(CONCURRENT_INDEX_MARKER):] for line in header
                    if line.startswith(CONCURRENT_INDEX_MARKER)]
    if not creates and not declarations:
        return []
    line = creates[0] if creates else 1

    def fail(reason):
        return [(line, "concurrent index: " + reason)]

    if runtime != CONCURRENT_INDEX_RUNTIME:
        return fail("requires explicit adoption of the atlas-qualified-v1 runtime contract")
    if (len(declarations) != 1 or header.count("-- atlas:txmode none") != 1
            or any(line.startswith("-- atlas:") and line != "-- atlas:txmode none" for line in header)):
        return fail("requires one versioned declaration and -- atlas:txmode none")
    try:
        def unique_fields(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate declaration field")
                result[key] = value
            return result

        declaration = json.loads(declarations[0], object_pairs_hook=unique_fields)
        required = {"schema", "table", "index", "columns", "lock_timeout_ms", "statement_timeout_ms"}
        if not isinstance(declaration, dict) or not required <= declaration.keys() or declaration.keys() - required - {"where_null"}:
            raise ValueError("unsupported declaration fields")

        def quote(value):
            if (not isinstance(value, str) or not value or len(value.encode("utf-8")) > 63
                    or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)):
                raise ValueError("identifiers must contain 1–63 UTF-8 bytes and no control characters")
            return '"' + value.replace('"', '""') + '"'

        lock, statement = declaration["lock_timeout_ms"], declaration["statement_timeout_ms"]
        if not (type(lock) is int and type(statement) is int and 0 < lock <= 3000
                and lock < statement <= 240000):
            raise ValueError("integer bounds require 0 < lock_timeout_ms <= 3000 and lock_timeout_ms < statement_timeout_ms <= 240000")
        columns = declaration["columns"]
        if not isinstance(columns, list) or not columns:
            raise ValueError("one or more plain index columns are required")
        expected = (f'CREATE INDEX CONCURRENTLY {quote(declaration["index"])} '
                    f'ON {quote(declaration["schema"])}.{quote(declaration["table"])} '
                    f'USING btree ({", ".join(quote(column) for column in columns)})')
        if declaration.get("where_null") is not None:
            expected += f' WHERE {quote(declaration["where_null"])} IS NULL'
        expected += ";"
        if "\n".join(body).strip() != expected:
            raise ValueError("SQL must be the exact declared single nonunique concurrent CREATE; no IF NOT EXISTS, SET, DROP, expression or extra statement")
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        return fail(str(exc))
    return []


def check_sql(sql: str, dialect: str, concurrent_index_runtime: str = "") -> list[tuple[int, str]]:
    tokens, markers = lex(sql, dialect)
    # Lexical failures remain errors even when a marker is present.
    errors = token_violations(tokens, dialect)
    # A released-expand reference does not prove resumability or effective
    # connection bounds. Operational findings are never marker exemptions.
    operational = concurrent_index_violations(sql, tokens, dialect, concurrent_index_runtime)
    if not operational and dialect == "postgres" and any(
            starts(tokens[i:], "CREATE", "INDEX", "CONCURRENTLY")
            or starts(tokens[i:], "CREATE", "UNIQUE", "INDEX", "CONCURRENTLY")
            for i in range(len(tokens))):
        # A valid declaration proves the entire file is exactly one supported
        # CREATE with runtime-owned cleanup and startup bounds. The legacy
        # SET/DROP heuristic contradicts that contract and must not re-admit
        # unconditional cleanup or connection-local SET statements.
        return []
    return operational + ([] if markers else errors)


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


def recorded_unapplied_withdrawal(root: Path, new_tree: dict[str, tuple[str, str]],
                                  path: str, old_blob: tuple[str, str],
                                  env: dict[str, str] | None = None) -> bool:
    if old_blob[0] not in {"100644", "100755"}:
        return False
    version_prefix = PurePosixPath(path).name.split("_", 1)[0]
    if not re.fullmatch(r"\d{14}", version_prefix):
        return False
    applied = {v for v in re.split(r"[\s,]+", (os.environ if env is None else env).get("ATLAS_APPLIED_VERSIONS", "")) if v}
    if version_prefix in applied:
        return False
    registry = new_tree.get("scripts/migrations/unapplied-withdrawals.json")
    if registry is None or registry[0] not in {"100644", "100755"}:
        return False
    try:
        entries = json.loads(git(root, "cat-file", "blob", registry[1]))
        if not isinstance(entries, list):
            return False
        matches = [e for e in entries if isinstance(e, dict) and e.get("path") == path]
        if len(matches) != 1:
            return False
        entry = matches[0]
        evidence = entry.get("evidence")
        if not isinstance(evidence, dict):
            return False
        pos_ctrl = evidence.get("positive_control_version")
        observed_at = evidence.get("observed_at")
        database = evidence.get("database")
        reason = evidence.get("reason")
        sha256 = entry.get("sha256")
        revision_rows = evidence.get("atlas_revision_rows")
        if (type(revision_rows) is not int
                or revision_rows not in (0, 1)
                or type(evidence.get("file_digest_rows")) is not int
                or evidence["file_digest_rows"] != 0
                or not isinstance(pos_ctrl, str)
                or not re.fullmatch(r"\d{14}", pos_ctrl)
                or pos_ctrl == version_prefix
                or not isinstance(observed_at, str)
                or not isinstance(database, str)
                or not database.strip()
                or not isinstance(reason, str)
                or not reason.strip()
                or not isinstance(sha256, str)
                or not re.fullmatch(r"[0-9a-f]{64}", sha256)):
            return False
        if revision_rows == 1:
            archive = entry.get("archive")
            receipt = evidence.get("receipt")
            receipt_sha256 = evidence.get("receipt_sha256")
            absent_objs = evidence.get("schema_objects_absent")
            if (type(evidence.get("atlas_applied_statements")) is not int
                    or evidence["atlas_applied_statements"] != 0
                    or type(evidence.get("atlas_total_statements")) is not int
                    or evidence["atlas_total_statements"] <= 0
                    or "atlas_partial_hashes" not in evidence
                    or evidence["atlas_partial_hashes"] is not None
                    or not isinstance(evidence.get("atlas_error"), str)
                    or not evidence["atlas_error"].strip()
                    or not isinstance(absent_objs, list)
                    or not absent_objs
                    or not all(isinstance(o, str) and o.strip() for o in absent_objs)
                    or not isinstance(archive, str)
                    or archive == path
                    or PurePosixPath(archive).name != PurePosixPath(path).name
                    or new_tree.get(archive) != old_blob
                    or not isinstance(receipt, str)
                    or not isinstance(receipt_sha256, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", receipt_sha256)):
                return False
            receipt_blob = new_tree.get(receipt)
            if receipt_blob is None or receipt_blob[0] not in {"100644", "100755"}:
                return False
            receipt_raw = subprocess.run(["git", "-C", str(root), "cat-file", "blob", receipt_blob[1]], capture_output=True, check=True).stdout
            if hashlib.sha256(receipt_raw).hexdigest() != receipt_sha256:
                return False
        datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        raw = subprocess.run(["git", "-C", str(root), "cat-file", "blob", old_blob[1]], capture_output=True, check=True).stdout
        return hashlib.sha256(raw).hexdigest() == sha256
    except (ValueError, UnicodeError, json.JSONDecodeError, subprocess.CalledProcessError):
        return False


def check_range(root: Path, base: str, head: str, dirs: list[str], dialect: str,
                schemas: list[str] | None = None, concurrent_index_runtime: str = "") -> tuple[list[str], int]:
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
            if path not in new and recorded_unapplied_withdrawal(root, new_tree, path, blob):
                continue
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
            errors.extend(f"{path}:{line}: {reason}" for line, reason in check_sql(sql, dialect, concurrent_index_runtime))
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
    parser.add_argument("--concurrent-index-runtime", choices=("", CONCURRENT_INDEX_RUNTIME),
                        default=env.get("MIGRATION_CONCURRENT_INDEX_RUNTIME", ""))
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
        errors, count = check_range(root, base, head, dirs, args.dialect, schemas, args.concurrent_index_runtime)
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
