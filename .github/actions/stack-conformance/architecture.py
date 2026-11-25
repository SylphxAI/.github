"""C1–C7 checks used by stack-conformance; findings never contain source values.

These are structural regression checks, not a semantic census. Check the whole
changed file for context, then gate findings on added lines. Unsupported source
and configured decisions remain explicit in the JSON report.
"""
from __future__ import annotations

import ast
import datetime
import functools
import fnmatch
import hashlib
import io
import importlib.util
import json
import pathlib
import re
import tokenize
import tomllib
from urllib.parse import quote

CONTRACT = "https://github.com/SylphxAI/skills/blob/main/skills/self-challenge/references/architecture.md"
_scope_spec = importlib.util.spec_from_file_location("source_scope", pathlib.Path(__file__).with_name("source_scope.py"))
source_scope = importlib.util.module_from_spec(_scope_spec)
_scope_spec.loader.exec_module(source_scope)
_tenant_spec = importlib.util.spec_from_file_location("tenant_reads", pathlib.Path(__file__).with_name("tenant_reads.py"))
tenant_reads = importlib.util.module_from_spec(_tenant_spec)
_tenant_spec.loader.exec_module(tenant_reads)
_deal_spec = importlib.util.spec_from_file_location("deal_randomness", pathlib.Path(__file__).with_name("deal_randomness.py"))
deal_randomness = importlib.util.module_from_spec(_deal_spec)
_deal_spec.loader.exec_module(deal_randomness)
_portable_spec = importlib.util.spec_from_file_location("project_portability", pathlib.Path(__file__).with_name("project_portability.py"))
project_portability = importlib.util.module_from_spec(_portable_spec)
_portable_spec.loader.exec_module(project_portability)
BASELINE_MARKER = "# architecture-ratchet-v2"
LEGACY_BASELINE_MARKER = "# architecture-ratchet-v1"
SKIP = {"tests", "test", "fixtures", "__fixtures__", "__tests__", "examples", "docs",
        "node_modules", "vendor", "thirdparty", "third_party", "third-party", "external",
        "target", "dist", "build", ".next"}
SOURCE = {".rs", ".py", ".go", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".sh", ".bash", ".java", ".lua", ".cs"}
CONFIG = {".yml", ".yaml", ".toml"}
PLATFORM = "SylphxAI/sylphx"
CATALOG = pathlib.Path(__file__).resolve().parents[3] / "policy/architecture-repositories.json"
try:
    CATALOG_DATA = json.loads(CATALOG.read_text())
except (OSError, ValueError):
    CATALOG_DATA = None
READ = re.compile(r"\b(?:fetch|get_status|query|poll|probe|list_items|status)\w*\s*\(", re.I)
SLEEP = re.compile(r"\b(?:sleep\s*\(|sleep\s+\d|setInterval\s*\()")
LIMITS = [
    "Project portability recognizes literal/template cluster DNS, single-label HTTP service hosts, Kubernetes service-account mounts and explicit in-cluster client calls. Deployment manifests are allowed by path and format; source under those paths is still checked. Computed destinations, arbitrary aliases, API publicness and key grants require semantic review.",
    "Source rules support Rust/Go/JavaScript/TypeScript/Python/shell, TOML and ordinary YAML blocks; macros, templates and aliases may need semantic review.",
    "C1 recognizes explicit first-party/network-trust success branches; indirect authorization behavior requires semantic review.",
    "C2 recognizes provider constructors/secret environment names; it does not prove that all duplicate capabilities are absent.",
    "C3 checks shared-engine executable imports/constants and bundled company Skills; runtime-supplied tenant configuration is allowed.",
    "C4 recognizes literal environment reads and duplicate unconditional literal defaults in Python; dynamic settings and cross-module authority need review.",
    "C5 recognizes polling loops, transport sleeps, polling interval callbacks and declared request-driven warm floors. Unclassified HPA/KEDA workloads are not assumed request-driven.",
    "C6 recognizes unexplained lint suppressions and routes declared retired in a migration contract; arbitrary dead code needs caller analysis.",
    "C7 recognizes failure-only CI verdicts and merge-only completion branches; it cannot infer every product's acceptance contract.",
    "Deal randomness recognizes named Rust/JS/TS/Java/Lua/C# deal, shuffle and card/tile/dice draw paths, same-file helper calls and explicit RNG bindings/imports. Unknown cross-module seed origins, arbitrary aliases and native client distribution require the separate inventory and live verification.",
    "Source findings on changed lines fail the change gate. Production database declaration gaps require an explicit shrinking baseline even on unrelated changes. Unchanged or unread scope is not certified clean.",
    "Economy cache reads recognize named Java/TypeScript/Rust functions and literal JS/TS route callbacks, cache-named calls/types/receivers and typed aliases. Typed permission grants, typed cache leases, pure cache factories, keys and write targets are distinguished structurally; indirect helpers and arbitrary data flow need semantic review. Java supports this rule only.",
    "A cache-display-only exception requires a reason and an existing named assertion-bearing test. The static check verifies that evidence exists; the test runner and review establish that it proves an authoritative re-read in the granting transaction.",
]


_EXCLUDED_NAME = re.compile(r"(?:[._-](?:test|spec)|_test)\.")
_SOURCE_TOKEN_RS_LEGACY = re.compile(
    r"//[^\n]*|/\*.*?\*/|\br(\#*)\"[\s\S]*?\"\1|\"(?:\\.|[^\"\\])*\"|\x27(?:\\.|[^\x27\\\n])\x27|`(?:\\.|[^`\\])*`",
    re.S,
)
_SOURCE_TOKEN_OTHER = re.compile(
    r"//[^\n]*|/\*.*?\*/|\br(\#*)\"[\s\S]*?\"\1|\"(?:\\.|[^\"\\])*\"|\x27(?:\\.|[^\x27\\])*\x27|`(?:\\.|[^`\\])*`",
    re.S,
)
_RS_CFG_TEST_MOD = re.compile(
    r"#\[cfg\(test\)\]\s*(?:#\[[^\]]*\]\s*)*(?:pub(?:\([^)]*\))?\s+)?mod\s+\w+\s*\{"
)
_RS_TEST_FN = re.compile(
    r"#\[(?:(?:tokio|async_std)::)?test(?:\([^\]]*\))?\]\s*(?:#\[[^\]]*\]\s*)*(?:async\s+)?fn\s+\w+[^;{]*\{"
)
_SH_COMMENT = re.compile(r"(?m)^\s*#.*$")
_PY_ATTEMPT_WHILE = re.compile(r"\battempt\w*\s*<")
_PY_ATTEMPT_INC = re.compile(r"\battempt\w*\s*\+=\s*[1-9]")
_SH_WHILE_LOOP = re.compile(r"\bwhile\s+(?:true|:)\s*;?\s*do\b[\s\S]*?\bdone\b")
_SH_BOUNDED = re.compile(r"\b(?:break|return|exit)\b")
_LOOP_PATTERNS = {
    ".rs": re.compile(r"\bwhile\s*(?:\(\s*true\s*\)|true\b|\(?\s*attempt\w*\s*<\s*\d+\s*\)?)\s*\{|\bloop\s*\{"),
    ".go": re.compile(r"\bwhile\s*(?:\(\s*true\s*\)|true\b|\(?\s*attempt\w*\s*<\s*\d+\s*\)?)\s*\{|\bfor\s*(?:true\s*)?\{"),
    "other": re.compile(r"\bwhile\s*(?:\(\s*true\s*\)|true\b|\(?\s*attempt\w*\s*<\s*\d+\s*\)?)\s*\{|\bfor\s*\(\s*;\s*;\s*\)\s*\{"),
}
_LOOP_BOUNDED = re.compile(r"\b(?:break|return|throw)\b")
_LOOP_ATTEMPT_INC = re.compile(r"\battempt\w*\s*(?:\+\+|\+=\s*[1-9])")
_PROVIDER_PART = re.compile(r"(?:^|/)(?:providers?|adapters?)(?:/|$)")
_BRANCH_RE = re.compile(
    r"\bif\b[^\n{}:]*?(?:first_party|is_internal|is_loopback|is_private|cluster_local|tenant\w*\s*===?\s*[\"'](?:sylphx|cubeage)[^\"']*[\"'])[^\n{}:]*[{:]",
    re.I,
)
_BRANCH_RET_RE = re.compile(r"\s*return\s+(?:true\b|True\b|Ok\s*\()")
_PROVIDER_RE = re.compile(
    r"\b(?:new\s+Stripe|stripe\s*::\s*Client\s*::\s*new|stripe\.Stripe|(?:STRIPE|TWILIO|VONAGE|AUTH0)_(?:SECRET|API|AUTH)[A-Z_]*(?:KEY|TOKEN))\b"
)
_ENV_LINE_RE = re.compile(r"getenv|env::var|process\.env|environ")
_PROVIDER_HOST_RE = re.compile(
    r'https?://(?:api\.stripe\.com|rest\.nexmo\.com|(?:buy|sandbox)\.itunes\.apple\.com|androidpublisher\.googleapis\.com|fcm\.googleapis\.com|(?:api\.|queue\.)?fal\.ai|api\.replicate\.com)(?=[:/"\x27\s]|$)',
    re.I,
)
_ENV_READS_RE = re.compile(
    r'''(?:\b(?:std::)?env::var(?:_os)?\s*\(\s*|\bos\.(?:getenv\s*\(|environ(?:\.get\s*\(|\s*\[))\s*|\bprocess\.env\s*\[\s*)["']([A-Z][A-Z0-9_]+)["']|\bprocess\.env\.([A-Z][A-Z0-9_]+)'''
)
_SET_INTERVAL_RE = re.compile(r"\bsetInterval\s*\(")
_SUPPRESSION_RE = re.compile(r"#\s*\[allow\s*\([^)]*\)\]|eslint-disable[^\n]*|#\s*(?:noqa|type:\s*ignore)[^\n]*")
_SUPPRESSION_REASON_RE = re.compile(r"(?:reason|because)\s*[:=]?\s*\S+(?:\s+\S+){2,}", re.I)
_MERGE_COMPLETE_RE = re.compile(r"\bif\b[^\n{}:]*\bmerged\b[^\n{}:]*[{:][\s\S]{0,180}?\b(?:settle_completed|mark_complete|complete_goal)\s*\(")
_MERGE_EVIDENCE_RE = re.compile(r"\b(?:evidence|receipt|deployed|verified)\b")
_MERGE_NEGATION_RE = re.compile(r"!|\bnot\b|\|\||\bor\b|==\s*(?:false|False|None|null)")


def excluded(path):
    p = pathlib.PurePosixPath(path)
    return (bool({part.casefold() for part in p.parts} & SKIP) or p.name.startswith("test_")
            or bool(_EXCLUDED_NAME.search(p.name)))


def canonical_repository(repository):
    """Normalize only the exact former name of the platform repository."""
    return PLATFORM if repository == "SylphxAI/cloud" else repository


@functools.lru_cache(maxsize=None)
def downstream_pattern(repository):
    data = CATALOG_DATA
    if not isinstance(data, dict):
        raise ValueError("pinned repository catalog is unreadable")
    if data.get("version") != 1 or not isinstance(data.get("shared_layers"), dict):
        raise ValueError("invalid pinned repository catalog")
    layers = data["shared_layers"]
    scope = layers.get(canonical_repository(repository))
    if scope is None:
        return None
    tokens = set(scope) | {org.lower() for org in scope}
    repositories = {row["id"]: row for row in data["repositories"]}
    for product in data["products"]:
        row = repositories[product["repository_id"]]
        org, name = row["full_name"].split("/", 1)
        if org in scope and canonical_repository(row["full_name"]) not in layers and not row["archived"]:
            if name not in {"website", "web", "api", "app", "main", "frontend", "backend"}:
                tokens.add(name)
            brand = product["product"]
            tokens.add(brand)
            tokens.add(re.sub(r"\s+", "-", brand.lower()))
    return "(?:" + "|".join(re.escape(n).replace(r"\-", "[_-]") for n in sorted(tokens, key=lambda n: (-len(n), n))) + ")"


def blank(text):
    if "\n" not in text:
        return " " * len(text)
    return "\n".join(" " * len(part) for part in text.split("\n"))


def _apply_spans(text, spans):
    if not spans:
        return text
    spans.sort()
    parts = []
    last = 0
    append = parts.append
    for start, end in spans:
        if end <= last:
            continue
        if start > last:
            append(text[last:start])
            append(blank(text[start:end]))
        else:
            append(blank(text[last:end]))
        last = end
    if last < len(text):
        append(text[last:])
    return "".join(parts)


def source_views(text, suffix, *, test_bodies=False, legacy_rust=False):
    """Return comment-free text and code with literal contents masked; same offsets."""
    comments = []
    if suffix == ".py":
        lines = text.splitlines(keepends=True)
        offsets = [0]
        for line in lines:
            offsets.append(offsets[-1] + len(line))
        clean_spans, code_spans = [], []
        try:
            tokens = tokenize.generate_tokens(io.StringIO(text).readline)
            for token in tokens:
                if token.type not in (tokenize.COMMENT, tokenize.STRING):
                    continue
                start = offsets[token.start[0] - 1] + token.start[1]
                end = offsets[token.end[0] - 1] + token.end[1]
                code_spans.append((start, end))
                if token.type == tokenize.COMMENT:
                    clean_spans.append((start, end))
                    comments.append((start, end))
            # A module/function docstring is documentation, not executable text.
            for node in ast.walk(ast.parse(text)):
                if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    start = offsets[node.lineno - 1] + len(lines[node.lineno - 1].encode()[:node.col_offset].decode())
                    end = offsets[node.end_lineno - 1] + len(lines[node.end_lineno - 1].encode()[:node.end_col_offset].decode())
                    clean_spans.append((start, end))
        except (tokenize.TokenError, IndentationError, SyntaxError):
            raise ValueError("Python could not be parsed; this file is not reviewed clean") from None
        return _apply_spans(text, clean_spans), _apply_spans(text, code_spans), comments
    else:
        if suffix == ".rs" and not legacy_rust:
            tokens = project_portability.rust_tokens(text)
        else:
            token = _SOURCE_TOKEN_RS_LEGACY if suffix == ".rs" else _SOURCE_TOKEN_OTHER
            tokens = (("comment" if text[m.start()] == "/" else "literal", m.start(), m.end())
                      for m in token.finditer(text))
        clean_parts, code_parts = [], []
        last_clean = last_code = 0
        text_find = text.find
        for kind, start, end in tokens:
            masked = (" " * (end - start)) if text_find("\n", start, end) < 0 else blank(text[start:end])
            code_parts.append(text[last_code:start])
            code_parts.append(masked)
            last_code = end
            if kind == "comment":
                clean_parts.append(text[last_clean:start])
                clean_parts.append(masked)
                last_clean = end
                comments.append((start, end))
        code_str = ("".join(code_parts) + text[last_code:]) if code_parts else text
        comment_free = ("".join(clean_parts) + text[last_clean:]) if clean_parts else text
        if suffix == ".rs" and not test_bodies:
            # Inline test modules are fixtures too; do not hide adjacent production.
            if "#[cfg(test)]" in code_str:
                rs_mod_spans = [(m.start(), brace_end(code_str, m.end())) for m in _RS_CFG_TEST_MOD.finditer(code_str)]
                if rs_mod_spans:
                    comment_free = _apply_spans(comment_free, list(rs_mod_spans))
                    code_str = _apply_spans(code_str, rs_mod_spans)
            if "#[" in code_str and "test" in code_str:
                rs_fn_spans = [(m.start(), brace_end(code_str, m.end())) for m in _RS_TEST_FN.finditer(code_str)]
                if rs_fn_spans:
                    comment_free = _apply_spans(comment_free, list(rs_fn_spans))
                    code_str = _apply_spans(code_str, rs_fn_spans)
        if suffix in {".sh", ".bash"} and "#" in text:
            sh_spans = [(m.start(), m.end()) for m in _SH_COMMENT.finditer(text)]
            if sh_spans:
                comments.extend(sh_spans)
                comment_free = _apply_spans(comment_free, list(sh_spans))
                code_str = _apply_spans(code_str, sh_spans)
        return comment_free, code_str, comments


def brace_end(code, start):
    return matching_end(code, start, "{", "}")


def matching_end(code, start, opening, closing):
    size = len(code)
    if start >= size:
        return start
    depth = 1
    pos = start
    find = code.find
    next_open = find(opening, pos)
    next_close = find(closing, pos)
    while depth:
        if next_close < 0:
            return size
        if 0 <= next_open < next_close:
            depth += 1
            pos = next_open + 1
            next_open = find(opening, pos)
        else:
            depth -= 1
            pos = next_close + 1
            next_close = find(closing, pos)
    return pos


def loops(clean, code, suffix):
    """Known unbounded loop bodies, excluding collection iteration and traits."""
    if suffix == ".py":
        lines = clean.splitlines(keepends=True)
        offsets = [0]
        for line in lines:
            offsets.append(offsets[-1] + len(line))
        tree = ast.parse(clean)
        for node in ast.walk(tree):
            retry = isinstance(node, ast.While) and bool(_PY_ATTEMPT_WHILE.search(lines[node.lineno - 1]))
            if not (isinstance(node, ast.While) and (retry or isinstance(node.test, ast.Constant)
                    and node.test.value in (True, 1))):
                continue
            body = clean[offsets[node.lineno - 1]:offsets[node.end_lineno]]
            bounded = (any(isinstance(child, (ast.Break, ast.Return, ast.Raise)) for child in ast.walk(node))
                       or retry and bool(_PY_ATTEMPT_INC.search(body)))
            yield offsets[node.lineno - 1], offsets[node.end_lineno], bounded
        return
    if suffix in {".sh", ".bash"}:
        for m in _SH_WHILE_LOOP.finditer(code):
            yield m.start(), m.end(), bool(_SH_BOUNDED.search(m.group()))
        return
    pattern = _LOOP_PATTERNS.get(suffix, _LOOP_PATTERNS["other"])
    for m in pattern.finditer(code):
        end = brace_end(code, m.end())
        body = code[m.end():end]
        bounded = bool(_LOOP_BOUNDED.search(body)
                       or "attempt" in m.group() and _LOOP_ATTEMPT_INC.search(body))
        yield m.start(), end, bounded


def finding(path, text, at, cls, rule, reason, end=None, *, source_sha=None):
    line = text.count("\n", 0, at) + 1
    # Debt follows the exact violation span, independently of surrounding edits
    # and line movement. Exception evidence still binds the entire source file.
    start = at if end is not None else text.rfind("\n", 0, at) + 1
    finish = end if end is not None else text.find("\n", at)
    if finish == -1:
        finish = len(text)
    if source_sha is None:
        source_sha = hashlib.sha256(text.encode(errors="surrogateescape")).hexdigest()
    return {"path": path, "line": line, "column": at - text.rfind("\n", 0, at),
            "end_line": text.count("\n", 0, end or at) + 1,
            "class": cls, "rule": rule, "reason": reason,
            "span_sha256": hashlib.sha256(text[start:finish].encode(errors="surrogateescape")).hexdigest(),
            "source_sha256": source_sha,
            "contract": CONTRACT + ("#scale" if cls == "C5" else "#hard-lines")}


def env_declarations(tree):
    names = set()
    for path in tree.paths():
        # Fixture declarations are not configuration authority for production
        # source, and may intentionally be malformed negative test inputs.
        if excluded(path):
            continue
        if pathlib.PurePosixPath(path).suffix in {".yaml", ".yml"} and path.startswith(".github/"):
            text = tree.read(path)
            if text is None:
                raise ValueError("unreadable environment declaration: " + path)
            env_indent = None
            for line in text.splitlines():
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                indent = len(line) - len(line.lstrip())
                if env_indent is not None and indent <= env_indent:
                    env_indent = None
                if re.match(r"\s*(?:-\s*)?env:\s*(?:#.*)?$", line):
                    env_indent = indent + (2 if line.lstrip().startswith("- ") else 0)
                elif env_indent is not None:
                    match = re.match(r"\s*([A-Z][A-Z0-9_]+):", line)
                    if match:
                        names.add(match[1])
            continue
        if pathlib.PurePosixPath(path).name not in {".env.example", ".env.sample", "env.schema.json", "sylphx.toml"}:
            continue
        text = tree.read(path)
        if text is None:
            raise ValueError("unreadable environment declaration: " + path)
        if path.endswith("env.schema.json"):
            try:
                names.update(json.loads(text).get("properties", {}))
            except (ValueError, AttributeError):
                raise ValueError("invalid environment declaration: " + path) from None
        elif path.endswith("sylphx.toml"):
            try:
                data = tomllib.loads(text)
                env = data.get("env", {})
                if not isinstance(env, dict):
                    raise ValueError("environment declaration must be a table")
                names.update(env)
            except (ValueError, TypeError):
                raise ValueError("invalid environment declaration: " + path) from None
            names.update(re.findall(r"(?m)^\s*([A-Z][A-Z0-9_]+)\s*=", text))
        else:
            names.update(re.findall(r"(?m)^\s*([A-Z][A-Z0-9_]+)\s*=", text))
    return names


@functools.lru_cache(maxsize=16)
def _service_owner_re(capability):
    return re.compile(r"^services/" + capability + r"/|crates/sylphx-" + capability + r"/")


def owns_provider(repository, path, capability):
    if canonical_repository(repository) != PLATFORM:
        return False
    if _PROVIDER_PART.search(path):
        return True
    return bool(_service_owner_re(capability).search(path)
                or capability == "money" and path.startswith("kernel/billing/"))


def with_occurrences(rows):
    occurrences = {}
    for row in rows:
        identity = (row["class"], row["rule"], row["span_sha256"])
        occurrences[identity] = occurrences.get(identity, 0) + 1
        row["occurrence"] = occurrences[identity]
    return rows


def identifier_words(name):
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).lower()


def economy_name(name):
    words = identifier_words(name)
    return bool(re.search(r"(?:^|[^a-z])(?:purchase|claim|grant|buy|redeem)(?:s|ed|ing)?(?:$|[^a-z])", words)
                or re.search(r"recharge.*call_?back", words))


def non_economy_cache_context(name, signature, body):
    """Require structural domain evidence; an economic operation overrides it."""
    words = identifier_words(signature + body)
    economic = (r"(?:purchase|buy|redeem|recharge|reward|item|inventory|price|stock|coin|currency|balance|"
                r"gift|goods|offer|wallet|payment|entitlement)")
    if (re.search(r"(?:^|[^a-z])" + economic + r"(?:s|ed|ing)?(?:$|[^a-z])", words)
            or re.search(r"\bgrant\s*\(", body)):
        # A pure factory accessor can have Buy in its cache type/name, but it
        # does not consume cached policy. Its purchase caller remains checked.
        return bool(re.match(r"get\w*Cache$", name)
                    and re.fullmatch(r"\s*return\s+\w+(?:\.\w+)*\.get\w*Cache\s*"
                                     r"\(\s*(?:\w+\.class\s*)?\)\s*;\s*\}", body))
    name_words = identifier_words(name)
    authorization = (re.search(r"(?:^|_)permissions?(?:_|$)", name_words)
                     and re.search(r"\b\w*Permission\w*\b", signature))
    lease = (re.search(r"(?:^|_)claim(?:_|$)", name_words)
             and re.search(r"\b\w+\s*:\s*(?:[\w:]+::)?CacheKey\b", signature)
             and re.search(r"\blease_\w*\s*:", signature))
    return bool(authorization or lease)


@functools.lru_cache(maxsize=1)
def _load_cache_economy_profiles():
    policy = json.loads(pathlib.Path(__file__).with_name("cache-economy-profiles.json").read_text())
    if policy.get("version") != 1 or not isinstance(policy.get("repositories"), dict):
        raise ValueError("invalid cache economy domain profiles")
    return policy["repositories"]


@functools.lru_cache(maxsize=64)
def _cache_economy_scope(repository):
    repositories = _load_cache_economy_profiles()
    profile = repositories.get(repository)
    if profile is None:
        profile = next((row for row in repositories.values()
                        if repository in row.get("aliases", [])), None)
    if profile is None:
        return None
    if not isinstance(profile.get("reason"), str) or not profile["reason"].strip():
        raise ValueError("cache economy domain profile requires a reason")
    if profile.get("applicability") == "not-applicable":
        return False
    if profile.get("applicability") != "paths" or not isinstance(profile.get("include"), list) or not profile["include"]:
        raise ValueError("invalid cache economy domain profile scope")
    if any(not isinstance(pattern, str) or not pattern or pattern.startswith("/") or ".." in pathlib.PurePosixPath(pattern).parts for pattern in profile["include"]):
        raise ValueError("invalid cache economy domain profile paths")
    return tuple(profile["include"])


def cache_economy_applicability(repository, path):
    """Reviewed domain profiles constrain this rule, never unrelated admission."""
    scope = _cache_economy_scope(repository)
    if scope is None:
        return True
    if scope is False:
        return False
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in scope)


def cache_key_name(name):
    return bool(re.search(r"(?:^|_)cache_key$", identifier_words(name)))


def named_bodies(clean, code, suffix, name_filter=None):
    """Ordinary declarations only; balanced bodies never bleed into a neighbour."""
    ident = r"[A-Za-z_$][\w$]*"
    if suffix == ".rs":
        pattern = r"\bfn\s+(?P<name>" + ident + r")(?:\s*<[^{};]*?>)?\s*\("
    elif suffix == ".java":
        pattern = (r"(?m)^[^\S\n]*(?:(?:public|private|protected|static|final|synchronized|default)\s+)*"
                   r"[\w$.<>?,\[\]]+\s+(?P<name>" + ident + r")\s*\(")
    else:
        pattern = (r"\bfunction\s+(?P<name>" + ident + r")(?:\s*<[^{};]*?>)?\s*\("
                   r"|\b(?P<arrow>" + ident + r")\s*(?::[^=;{}\n]+)?[=:]\s*(?:async\s*)?\("
                   r"|(?m:^[^\S\n]*(?:(?:export|public|private|protected|static|async)\s+)*"
                   r"(?P<method>" + ident + r")(?:\s*<[^{};]*?>)?\s*\()")
    for match in re.finditer(pattern, code):
        name = next((v for v in match.groupdict().values() if v), "")
        if name in {"if", "for", "while", "switch", "catch", "with"} or name_filter and not name_filter(name):
            continue
        params_end = matching_end(code, match.end(), "(", ")")
        # A declaration ends at a semicolon; a call is not a function body.
        tail = re.compile(r"[^;{}=]*?(?:=>\s*)?\{").match(code, params_end)
        if tail is None:
            continue
        start = tail.end()
        end = brace_end(code, start)
        yield name, match.start(), start, end
    if suffix in {".ts", ".tsx", ".js", ".mjs", ".cjs"}:
        route = r"\.(?:post|put|patch)\s*\(\s*(['\"])(?P<name>[^'\"]+)\1\s*,\s*(?:async\s*)?\([^)]*\)\s*=>\s*\{"
        for match in re.finditer(route, clean):
            if code[match.start()] == "." and (name_filter is None or name_filter(match["name"])):
                yield match["name"], match.start(), match.end(), brace_end(code, match.end())


def economy_cache_reads(clean, code, suffix):
    if suffix not in {".java", ".rs", ".ts", ".tsx", ".js", ".mjs", ".cjs"}:
        return
    if not any(token in code.lower() for token in ("cache", "redis", "valkey")):
        return
    cache_type = r"\w*(?:Cache|cache)\w*"
    # A field/parameter's cache type also identifies a receiver called `offers`.
    aliases = set(re.findall(r"\b" + cache_type + r"(?:\s*<[^;={}()]*>)?\s+(\w+)\b", code))
    aliases.update(re.findall(r"\b(\w+)\s*:\s*&?(?:mut\s+)?(?:[\w:]+::)?" + cache_type + r"\b", code))
    for name, header, start, end in named_bodies(clean, code, suffix, economy_name):
        body = code[start:end]
        if non_economy_cache_context(name, code[header:start], body):
            continue
        candidates = []
        # Java TemplateCache.getCache(), cached_price(), Cache::get(), etc.
        calls = r"\b([A-Za-z_$][\w$]*)\s*(?:(?:::|\.)\s*([A-Za-z_$][\w$]*))?\s*\("
        for match in re.finditer(calls, body):
            receiver, method = match.groups()
            token = method or receiver
            if cache_key_name(token):
                continue
            if re.match(r"(?:invalidate|evict|clear|remove|delete|put|set|insert|refresh|save|write)", token, re.I):
                continue
            if (re.search("cache", token, re.I)
                    or method and (re.search("cache|redis|valkey", receiver, re.I) or receiver in aliases)):
                candidates.append((start + match.start(), start + match.end()))
        # Plain value/field reads (self.cached_price) count; a property name in
        # fetch({cache: 'no-store'}) or a CacheKey type is not a cache read.
        for match in re.finditer(r"\b\w*cache\w*\b", body, re.I):
            tail = body[match.end():]
            if match.group()[0].isupper() or cache_key_name(match.group()):
                continue
            # Assignment targets are writes; equality and compound assignment
            # still consume the old value. Bracket targets may contain reads.
            if re.match(r"\s*(?:=(?!=|>)|:)", tail):
                continue
            if re.match(r"(?:\s*\.\s*\w+|\s*\[[^\[\]]*\])+\s*=(?!=|>)", tail):
                continue
            if re.match(r"\s*\.(?:invalidate|evict|clear|remove|delete|put|set|insert|refresh|save|write)\s*\(", tail):
                continue
            if not re.match(r"(?:invalidate|evict|clear|remove|delete|put|set|insert|refresh|save|write)", match.group(), re.I):
                candidates.append((start + match.start(), start + match.end()))
        # Collapse the receiver/token already covered by a call, while keeping
        # two reads on one line as two ratcheted occurrences.
        seen = []
        for at, finish in sorted(candidates):
            if not any(left <= at < right for left, right in seen):
                seen.append((at, finish))
                yield at, finish


def cache_display_proof(tree, path, text, line):
    """A reason and test reference are evidence, never a proof of program semantics."""
    lines = text.splitlines(keepends=True)
    line_start = sum(map(len, lines[:line - 1]))
    line_end = line_start + len(lines[line - 1])
    _, _, comments = source_views(text, pathlib.PurePosixPath(path).suffix)
    for start, end in comments:
        marker = re.search(r"cache-display-only:\s*(.+?);\s*test=([^\s#;]+)#([\w-]+)\s*(?:\*/)?$",
                           text[max(start, line_start):min(end, line_end)] if start < line_end and end > line_start else "")
        if not marker or not marker[1].strip():
            continue
        reason, test_path, name = marker.groups()
        p = pathlib.PurePosixPath(test_path)
        if (p.is_absolute() or ".." in p.parts or p.suffix not in SOURCE
                or not (excluded(test_path) or p.suffix == ".rs")):
            continue
        test = tree.read(test_path)
        if test is None:
            continue
        clean, code, _ = source_views(test, p.suffix, test_bodies=True)
        bodies = ([(n, a, b, e) for n, a, b, e in named_bodies(clean, code, p.suffix) if n == name]
                  if p.suffix in {".java", ".rs"} else [])
        if p.suffix in {".ts", ".tsx", ".js", ".mjs", ".cjs"}:
            pattern = r"\b(?:test|it)\s*\(\s*(['\"])" + re.escape(name) + r"\1\s*,\s*(?:async\s*)?\([^)]*\)\s*=>\s*\{"
            bodies += [(name, m.start(), m.end(), brace_end(code, m.end())) for m in re.finditer(pattern, clean)
                       if code[m.start():m.start()+2].strip()]
        for _, header, start, end in bodies:
            prefix = clean[max(0, header - 150):header]
            is_test = (p.suffix in {".ts", ".tsx", ".js", ".mjs", ".cjs"}
                       or p.suffix == ".java" and bool(re.search(r"@Test\b[^{};]*$", prefix))
                       or p.suffix == ".rs" and bool(re.search(r"#\[(?:(?:tokio|async_std)::)?test\]\s*(?:#\[[^\]]*\]\s*)*(?:pub\s+)?(?:async\s+)?$", prefix)))
            if is_test and re.search(r"\b(?:assert\w*|expect)\s*(?:!\s*)?\(", code[start:end]):
                return {"reason": reason.strip(), "test": test_path + "#" + name}
    return None


def _downstream_trie(downstream):
    if not (downstream.startswith("(?:") and downstream.endswith(")")):
        return None
    body = downstream[3:-1]
    words = []
    for branch in body.split("|"):
        sentinel = branch.replace("[_-]", "\x00")
        # Bail out cleanly if branch contains unescaped regex constructs.
        if re.search(r"(?<!\\)[.+*?^$()[\]{}]", sentinel):
            return None
        words.append(re.sub(r"\\(.)", r"\1", sentinel))
    tree = {}
    for w in words:
        cur = tree
        for ch in w:
            cur = cur.setdefault(ch, {})
        cur[""] = True

    def render(node):
        if len(node) == 1 and "" in node:
            return ""
        opts = []
        end = "" in node
        for ch, sub in sorted(node.items()):
            if ch == "":
                continue
            r = render(sub)
            esc = r"[_-]" if ch == "\x00" else re.escape(ch)
            opts.append(esc + r)
        body_re = opts[0] if len(opts) == 1 else "(?:" + "|".join(opts) + ")"
        if end:
            if len(opts) == 1 and len(opts[0]) == 1:
                body_re += "?"
            else:
                body_re = "(?:" + body_re + ")?"
        return body_re

    return render(tree)


@functools.lru_cache(maxsize=64)
def _compile_downstream(downstream):
    trie = _downstream_trie(downstream)
    ds_bound = re.compile(r"[\s\"'@`]" + trie + r"(?=\W|$)") if trie else None
    ds_org = re.compile(r"[\"'](?:org_|tenant_)[A-Za-z0-9]+[\"']")
    return (
        re.compile(r"\b(?:use|import|from)\s+" + downstream + r"(?=\W|$)"),
        re.compile(r'\b(?:from\s+|require\s*\(\s*)["\x27]@?' + downstream + r'(?=[/"\x27])[^"\x27]*["\x27]'),
        re.compile(r"\b(?:const|static|let)\b[^\n;=]*=\s*[\"'](?:" + downstream + r"|org_[A-Za-z0-9]+|tenant_[A-Za-z0-9]+)[\"']"),
        ds_bound,
        ds_org,
    )


@functools.lru_cache(maxsize=128)
def _retired_route_re(route):
    return re.compile(r"\b(?:route|path|add_route)\s*\(\s*[\"']" + re.escape(route) + r"[\"']")


_TOML_MASK_RE = re.compile(r'"""[\s\S]*?"""|\x27\x27\x27[\s\S]*?\x27\x27\x27|"(?:\\.|[^"\\])*"|\x27[^\x27]*\x27|#[^\n]*')
_TOML_IDLE_FLOOR_RE = re.compile(r'(?m)^[^\S\n]*min_instances\s*=\s*([1-9]\d*)\b')


def scan_file(path, text, repository, declared, retired_routes, *, tree=None, _views=None, _env_tracker=None):
    suffix = pathlib.PurePosixPath(path).suffix
    if (excluded(path) and deal_randomness.excluded(path)) or not (suffix in SOURCE | CONFIG or project_portability.supported(path)):
        return []
    out = []
    _source_sha = None

    def _get_source_sha():
        nonlocal _source_sha
        if _source_sha is None:
            _source_sha = hashlib.sha256(text.encode(errors="surrogateescape")).hexdigest()
        return _source_sha

    add = lambda at, cls, rule, why, end=None: out.append(finding(path, text, at, cls, rule, why, end, source_sha=_get_source_sha()))
    if _views is None:
        _local_view_cache = {}
        sv = source_views

        def views_fn(src_text, src_suffix, *, test_bodies=False, legacy_rust=False):
            key = (src_text is text, 0 if src_text is text else id(src_text), src_suffix, test_bodies, legacy_rust)
            res = _local_view_cache.get(key)
            if res is None:
                res = sv(src_text, src_suffix, test_bodies=test_bodies, legacy_rust=legacy_rust)
                _local_view_cache[key] = res
            return res
    else:
        views_fn = _views
    if not excluded(path):
        portable = project_portability.scan(path, text, views_fn)
        for at, end, why in portable:
            add(at, "C1", "project-portability", why, end)
        if suffix == ".rs" and portable:
            # Frozen pre-repair recognition is evidence for rekeying old debt,
            # never an alternate detector or an allowance for a new destination.
            legacy_views = views_fn(text, suffix, legacy_rust=True)
            legacy = project_portability.scan(path, text, lambda t, s: legacy_views, legacy_rust=True)
            legacy_literals = {(a, b) for a, b, _ in
                               project_portability.literals(legacy_views[0], suffix, legacy_rust=True)}
            legacy_rows = with_occurrences([finding(path, text, a, "C1", "project-portability", why, b, source_sha=_get_source_sha())
                                           for a, b, why in legacy])
            legacy_hashes = {row["span_sha256"] for row in legacy_rows}
            for (start, end, _), row in zip(portable, out):
                # Coverage is lexical, not a reset for newly refined context
                # rules: an already recognized literal cannot be seeded.
                row["legacy_portability_literal_seen"] = any(
                    a <= start and end <= b or start <= a and b <= end
                    for a, b in legacy_literals)
                # An unseen equal token can shift onto a previously seen
                # token's ordinal. Coverage must not relabel old unrecorded
                # debt as that hidden token when either occurrence disappears.
                row["legacy_portability_seen_span"] = row["span_sha256"] in legacy_hashes
            for (a, b, _), old in zip(legacy, legacy_rows):
                for (start, end, _), row in zip(portable, out):
                    # Legacy lifetimes can enclose real literals. Conversely,
                    # br/cr raw tokens can enclose the old quoted-body finding.
                    # Reverse containment requires literal evidence: an old
                    # spurious executable call inside raw text is not debt.
                    if (a <= start and end <= b or
                            (a, b) in legacy_literals and start <= a and b <= end):
                        row.setdefault("legacy_portability_keys", []).append(baseline_key(old))
    if suffix not in SOURCE | CONFIG:
        return with_occurrences(out)
    scope_reason = None
    for at, end, why in deal_randomness.scan(path, text, views_fn, named_bodies, matching_end, tree=tree):
        add(at, "C1", "deal-randomness", why, end)
        scope_reason = scope_reason or deal_randomness.scope_reason(repository, path, text, tree)
        out[-1]["scope_warning"] = scope_reason
        out[-1]["contract"] = "https://github.com/SylphxAI/skills/blob/main/skills/self-challenge/references/games.md#hard-lines"
    if excluded(path) or suffix in {".lua", ".cs"}:
        return with_occurrences(out)
    if suffix == ".toml":
        try:
            tomllib.loads(text)
        except ValueError:
            raise ValueError("TOML could not be parsed; this file is not reviewed clean") from None
        if pathlib.PurePosixPath(path).name == "sylphx.toml":
            missing = tenant_reads.check(tree, path, text)
            if missing:
                add(0, "C2", "tenant-read-functions", missing)
                out[-1]["contract"] = tenant_reads.CONTRACT
                # This debt belongs to the manifest's production database,
                # independently of unrelated edits to that manifest.
                out[-1]["span_sha256"] = hashlib.sha256(("production-database:" + path).encode()).hexdigest()
            if "min_instances" in text:
                masked = _apply_spans(text, [(m.start(), m.end()) for m in _TOML_MASK_RE.finditer(text)])
                for m in _TOML_IDLE_FLOOR_RE.finditer(masked):
                    add(m.start(), "C5", "idle-floor", "Positive service idle floor requires a recorded always-on decision with its reason")
        return with_occurrences(out)
    if suffix in CONFIG:
        boundaries = [0] + [m.end() for m in re.finditer(r"(?m)^---[^\S\n]*\n", text)] + [len(text)]
        for offset, finish in zip(boundaries, boundaries[1:]):
            document = text[offset:finish]
            lines = document.splitlines(keepends=True)
            kind = re.search(r"(?m)^kind:\s*(\w+)", document)
            request_driven = bool(re.search(r'(?m)^\s*conformance\.request-driven:\s*["\x27]?true\b|^\s*-?\s*type:\s*http\b', document))
            floor = r'autoscaling\.knative\.dev/min(?:-scale|Scale)'
            if re.search(r"(?m)^apiVersion:\s*serving\.knative\.dev/", document):
                floor += r'|minScale'
            for m in re.finditer(r'(?m)^[^\S\n]*["\x27]?(?:' + floor + r')["\x27]?\s*:\s*[\"\x27]?([1-9]\d*)', document):
                add(offset + m.start(), "C5", "idle-floor", "Positive idle floor requires a recorded always-on decision with its reason")
            if request_driven and kind and kind.group(1) in {"HorizontalPodAutoscaler", "ScaledObject"}:
                for m in re.finditer(r'(?m)^[^\S\n]*(?:minReplicas|minReplicaCount):\s*["\x27]?([1-9]\d*)', document):
                    add(offset + m.start(), "C5", "idle-floor", "Request-driven autoscaler retains idle replicas without an always-on decision")
            if kind and kind.group(1) == "CronJob":
                for m in re.finditer(r"(?m)^\s*schedule:\s*.+", document):
                    # Calendar work declares the obligation, not merely a cron frequency.
                    if not re.search(r"conformance.calendar-obligation:\s*[\"']?\S+[#/]\S+", document):
                        add(offset + m.start(), "C5", "cron-progress", "Cron must name its calendar obligation or documented safety-resync contract")
            for m in re.finditer(r"(?m)^[^\S\n]*(?:if:|(?:run:\s*(?:[|>][-+]?\s*)?)?jq\b)[^\n]*\.result\s*!=\s*[\"']failure[\"'][^\n]*$", document):
                if not re.search(r"\.result\s*!=\s*[\"']cancelled[\"']", m.group()):
                    add(offset + m.start(), "C7", "failure-only-verdict", "A failure-only aggregate accepts canceled dependencies")
        return with_occurrences(out)
    clean, code, comments = views_fn(text, suffix)
    if cache_economy_applicability(repository, path):
        for at, end in economy_cache_reads(clean, code, suffix):
            add(at, "C1", "cache-economy", "Player economy handler reads cached authority; validate balance, grant, reward, purchase or entitlement against authority in the granting transaction", end)
            out[-1]["contract"] = "https://github.com/SylphxAI/skills/blob/main/skills/self-challenge/references/games.md#hard-lines"
    if suffix == ".java":
        return with_occurrences(out)
    clean_lower = clean.lower()
    # Require a control-flow token in real code; quoted examples never count.
    if "if" in clean_lower and any(tok in clean_lower for tok in ("first_party", "is_internal", "is_loopback", "is_private", "cluster_local", "tenant")):
        for m in _BRANCH_RE.finditer(clean):
            if code[m.start():m.start()+2] != "if":
                continue
            end = brace_end(code, m.end()) if m.group().endswith("{") else m.end() + len(clean[m.end():].split("\n\n")[0])
            body = code[m.end():end]
            if _BRANCH_RET_RE.match(body):
                add(m.start(), "C1", "privileged-branch", "First-party identity or network position reaches an unconditional success return", end)
    if "stripe" in clean_lower or "TWILIO_" in clean or "VONAGE_" in clean or "AUTH0_" in clean:
        for m in _PROVIDER_RE.finditer(clean):
            capability = "money" if "stripe" in m.group().lower() else "auth" if "AUTH0" in m.group() else "notify"
            if owns_provider(repository, path, capability):
                continue
            # An env name is literal data attached to a real read; constructor must be code.
            line = clean[clean.rfind("\n", 0, m.start())+1:clean.find("\n", m.end()) if "\n" in clean[m.end():] else len(clean)]
            if code[m.start():m.end()].strip() or _ENV_LINE_RE.search(line):
                add(m.start(), "C2", "provider-bypass", "Product uses provider capability directly; use its ordinary platform operation")
    if "://" in clean_lower:
        for m in _PROVIDER_HOST_RE.finditer(clean):
            capability = "money" if any(s in m.group().lower() for s in ("stripe", "itunes", "androidpublisher")) else "ai" if any(s in m.group().lower() for s in ("fal.ai", "replicate")) else "notify"
            if owns_provider(repository, path, capability):
                continue
            # Literal host in executable code; comments/docstrings were removed.
            line_start = clean.rfind("\n", 0, m.start()) + 1
            if code[line_start:m.start()].strip():
                add(m.start(), "C2", "provider-bypass", "Product calls an offered capability's provider host directly")
    downstream = downstream_pattern(repository)
    if downstream:
        r_use, r_from, r_const, ds_bound_re, ds_org_re = _compile_downstream(downstream)
        has_ds = True if ds_bound_re is None else bool(ds_bound_re.search(clean))
        if has_ds and ("use" in code or "import" in code or "from" in code):
            for m in r_use.finditer(code):
                add(m.start(), "C3", "downstream-import", "Shared engine imports downstream company/product behavior")
        if has_ds and ("from" in clean or "require" in clean):
            for m in r_from.finditer(clean):
                if code[m.start():m.start()+4].strip():
                    add(m.start(), "C3", "downstream-import", "Shared engine imports a downstream product module")
        if ("const " in clean or "const\t" in clean or "const\n" in clean
                or "static " in clean or "static\t" in clean or "static\n" in clean
                or "let " in clean or "let\t" in clean or "let\n" in clean):
            if has_ds or (("org_" in clean or "tenant_" in clean) and ds_org_re.search(clean)):
                for m in r_const.finditer(clean):
                    if code[m.start():m.start()+3].strip():
                        add(m.start(), "C3", "downstream-config", "Shared engine compiles a downstream identity into executable configuration")
        if "bundled_skills" in pathlib.PurePosixPath(path).name and clean.strip():
            add(0, "C3", "bundled-method", "Company Skill bodies belong in replaceable configuration, not engine source")
    if "env::var" in clean or "os.getenv" in clean or "os.environ" in clean or "process.env" in clean:
        for m in _ENV_READS_RE.finditer(clean):
            if _env_tracker is not None:
                _env_tracker[0] = True
            name = m.group(1) or m.group(2)
            if not code[m.start():m.start()+2].strip():
                continue
            if pathlib.PurePosixPath(path).name == "build.rs" and (name.startswith("CARGO_") or name in {"OUT_DIR", "TARGET", "HOST", "PROFILE", "OPT_LEVEL", "DEBUG", "NUM_JOBS", "RUSTC", "RUSTDOC", "RUSTC_LINKER", "RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER", "RUSTFLAGS"}):
                continue
            if name not in declared and not name.startswith(("GITHUB_", "RUNNER_")) and name not in {"CI", "HOME", "PATH", "TMPDIR"}:
                add(m.start(), "C4", "undeclared-setting", "Literal environment read lacks a declaration in the environment schema or sample")
    if suffix == ".py" and "DEFAULT_" in text:
        defaults = {}
        for node in ast.parse(text).body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id.startswith("DEFAULT_"):
                        if target.id in defaults and defaults[target.id] != node.value.value:
                            at = sum(map(len, clean.splitlines(keepends=True)[:node.lineno-1]))
                            add(at, "C4", "competing-default", "The same unconditional default has conflicting literal authorities")
                        defaults[target.id] = node.value.value
    has_loop_hint = ("while" in text) if suffix == ".py" else ("while" in code) if suffix in {".sh", ".bash"} else ("while" in code or "loop" in code) if suffix == ".rs" else ("while" in code or "for" in code)
    if has_loop_hint:
        for start, end, bounded in loops(text if suffix == ".py" else clean, code, suffix):
            if bounded:
                continue
            if re.search(r"\b(?:sleep\s*\(|sleep\s+\d|(?:fetch|get_status|query|poll|probe|list_items|status)\w*\s*\()", code[start:end], re.I):
                add(start, "C5", "polling-loop", "An unbounded loop advances by a transport read or sleep instead of an event", end)
    if "setInterval" in code:
        for m in _SET_INTERVAL_RE.finditer(code):
            end = matching_end(code, m.end(), "(", ")")
            if READ.search(code[m.end():end]):
                add(m.start(), "C5", "polling-timer", "An interval advances ordinary state reads instead of subscribing to events", end)
    if suffix in {".ts", ".tsx", ".js", ".mjs", ".cjs"} and "setTimeout" in code:
        for name, header, start, end in named_bodies(clean, code, suffix):
            body = code[start:end]
            rearm = r"\bsetTimeout\s*\(\s*(?:\(\s*\)\s*=>\s*)?" + re.escape(name) + r"\b"
            if READ.search(body) and re.search(rearm, body) and not re.search(r"\b(?:if|return|throw)\b", body):
                add(header, "C5", "polling-timer", "A timer re-arms a state read without an exit instead of subscribing to events", end)
    if "eslint-disable" in text or ("#" in text and ("allow" in text or "noqa" in text or "type:" in text)):
        for m in _SUPPRESSION_RE.finditer(text):
            # A suppression must be an actual comment or Rust attribute, not quoted text.
            if text[m.start():m.start()+2] != "#[" and not any(start <= m.start() < end for start, end in comments):
                continue
            if text[m.start():m.start()+2] == "#[" and not code[m.start():m.start()+2].strip():
                continue
            line = text[text.rfind("\n", 0, m.start())+1:text.find("\n", m.end()) if "\n" in text[m.end():] else len(text)]
            if not _SUPPRESSION_REASON_RE.search(line):
                add(m.start(), "C6", "unexplained-suppression", "Lint suppression needs its narrow constraint and reason")
    for route in retired_routes:
        if route not in clean:
            continue
        for m in _retired_route_re(route).finditer(clean):
            if code[m.start():m.start()+4].strip():
                add(m.start(), "C6", "retired-route", "A completed migration contract marks this still-executable route retired")
    if "merged" in code and ("settle_completed" in code or "mark_complete" in code or "complete_goal" in code):
        for m in _MERGE_COMPLETE_RE.finditer(code):
            header = re.split(r"[{:}]", m.group(), maxsplit=1)[0]
            terms = re.split(r"&&|\band\b", header)
            positive_evidence = any(_MERGE_EVIDENCE_RE.search(term)
                                    and not _MERGE_NEGATION_RE.search(term)
                                    for term in terms[1:])
            if not positive_evidence:
                add(m.start(), "C7", "merge-only-completion", "Merge-only branch settles delivery without required environment evidence")
    unique = {(f["line"], f["column"], f["class"], f["rule"]): f for f in out}
    return with_occurrences(list(unique.values()))


def decisions(text, repository, today, *, ignore_expired=False):
    """Return active exemptions and policy errors without aborting the census."""
    if text is None:
        return [], []
    data = json.loads(text)
    if not isinstance(data, dict) or set(data) != {"version", "decisions"} or data["version"] != 1:
        raise ValueError("invalid architecture decisions schema")
    rows = data["decisions"]
    if not isinstance(rows, list):
        raise ValueError("architecture decisions must be an array")
    seen, active, errors = set(), [], []
    for row in rows:
        required = {"repository", "path", "line", "class", "rule", "source_sha256", "owner", "reason", "contract", "expires", "authority", "evidence", "reconsideration"}
        if (not isinstance(row, dict) or set(row) != required
                or any(not isinstance(v, str) or not v.strip() for k, v in row.items() if k != "line")
                or type(row.get("line")) is not int or row["line"] < 1):
            raise ValueError("decision must name exact finding, owner, reason, contract and expiry")
        if (repository not in {row["repository"], canonical_repository(row["repository"])}
                or row["class"] not in {f"C{i}" for i in range(1, 8)}
                or any(c in row["path"] for c in "*?[")):
            raise ValueError("decision scope does not match this repository or contains a wildcard")
        if not re.fullmatch(r"[a-f0-9]{64}", row["source_sha256"]) or len(row["reason"].split()) < 3:
            raise ValueError("decision needs an exact source hash and specific rationale")
        if not all(row[k].startswith("https://") for k in ("contract", "authority", "evidence")):
            raise ValueError("decision must link its contract, authority and evidence")
        identity = tuple(row[k] for k in ("path", "line", "class", "rule", "source_sha256"))
        if identity in seen:
            raise ValueError("duplicate architecture decision: " + row["path"])
        seen.add(identity)
        try:
            expires = datetime.date.fromisoformat(row["expires"])
        except ValueError:
            raise ValueError("architecture decision expiry must be an ISO date") from None
        if expires < today:
            if not ignore_expired:
                # The schema has no authored id; bind a stable diagnostic id to
                # the exact exemption scope, including repository and source.
                decision_id = hashlib.sha256(json.dumps(
                    [row["repository"], *identity], separators=(",", ":")).encode()).hexdigest()
                errors.append({"decision_id": decision_id,
                    **{k: row[k] for k in ("repository", "path", "line", "class", "rule",
                                           "source_sha256", "expires", "authority")},
                    "reason": (f"architecture decision expired: {decision_id} "
                               f"({row['repository']} {row['class']}/{row['rule']} at "
                               f"{row['path']}:{row['line']}) expired on {row['expires']}; "
                               f"authority: {row['authority']}")})
            continue
        active.append(row)
    return active, errors


class _DepTrackTree:
    """Proxy tracking tree reads and path-listing dependencies per scanned file."""

    def __init__(self, tree, oid_fn):
        self._tree = tree
        self._cargo_owner = tree
        self._oid_fn = oid_fn
        self.reads = {}
        self.used_paths = False

    def __getattr__(self, name):
        return getattr(self._tree, name)

    def paths(self):
        self.used_paths = True
        return self._tree.paths()

    def read(self, path, **kwargs):
        self.reads[path] = self._oid_fn(path)
        return self._tree.read(path, **kwargs)

    def read_model_source(self, path):
        self.reads[path] = self._oid_fn(path)
        return self._tree.read_model_source(path)


_SURROGATE_EXTS = (".java", ".lua", ".cs", ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".m", ".mm", ".php", ".rb")


def _copy_rows(rows):
    copied = []
    for row in rows:
        item = dict(row)
        if "legacy_portability_keys" in item:
            item["legacy_portability_keys"] = list(item["legacy_portability_keys"])
        copied.append(item)
    return copied


def evaluate(tree, repository, changed, today, *, ignore_expired=False, file_cache=None, shared_views=None):
    """Pure git-tree input -> report. No network, environment, clock or mutation."""
    declared = env_declarations(tree)
    retired = []
    migration = tree.read(".github/architecture-migrations.json")
    if migration:
        data = json.loads(migration)
        retired = data.get("retired_routes", []) if data.get("completed") is True else []
        if not isinstance(retired, list) or any(not isinstance(r, str) or not r.startswith("/") for r in retired):
            raise ValueError("invalid retired-route migration contract")
    allowed, decision_errors = decisions(tree.read(".github/architecture-decisions.json"), repository, today,
                        ignore_expired=ignore_expired)
    findings, unreadable, reviewed, skipped = [], [], [], []
    # Always expose inherited findings. Changed lines control regression admission,
    # never which old debt is visible in the report.
    rescan = changed is None or any(path in changed for path in (
        ".github/architecture-decisions.json", ".github/architecture-migrations.json"))
    if changed is not None and any(pathlib.PurePosixPath(p).name in {
        ".env.example", ".env.sample", "env.schema.json", "sylphx.toml"} for p in changed):
        rescan = True
    if changed is not None and any(p.startswith(".github/") and pathlib.PurePosixPath(p).suffix in {".yml", ".yaml"} for p in changed):
        rescan = True
    paths = sorted(tree.paths())
    path_set = set(paths)
    blob_oid_fn = getattr(tree, "blob_oid", None) if (file_cache is not None or shared_views is not None) else None
    retired_key = tuple(retired)
    _cargo_fp_val = None
    _paths_fp_val = None

    def _get_cargo_fp():
        nonlocal _cargo_fp_val
        if _cargo_fp_val is None:
            _cargo_fp_val = tuple(
                (p, blob_oid_fn(p)) for p in paths if p == "Cargo.toml" or p.endswith("/Cargo.toml")
            )
        return _cargo_fp_val

    def _get_paths_fp():
        nonlocal _paths_fp_val
        if _paths_fp_val is None:
            _paths_fp_val = hashlib.sha256("\0".join(paths).encode("utf-8", errors="surrogateescape")).digest()
        return _paths_fp_val

    def _deps_valid(has_env_reads, dep_reads, dep_cargo_fp, dep_paths_fp, entry_declared):
        return ((not has_env_reads or entry_declared == declared)
                and (dep_cargo_fp is None or dep_cargo_fp == _get_cargo_fp())
                and (dep_paths_fp is None or dep_paths_fp == _get_paths_fp())
                and (not dep_reads or all(blob_oid_fn(dp) == doid for dp, doid in dep_reads)))

    def _make_views(primary_text, primary_oid):
        sv = source_views
        local = {}

        def _views(src_text, src_suffix, *, test_bodies=False, legacy_rust=False):
            if src_text is primary_text and shared_views is not None and primary_oid is not None:
                skey = (primary_oid, src_suffix, test_bodies, legacy_rust)
                res = shared_views.get(skey)
                if res is None:
                    res = sv(src_text, src_suffix, test_bodies=test_bodies, legacy_rust=legacy_rust)
                    shared_views[skey] = res
                return res
            lkey = (src_text is primary_text, 0 if src_text is primary_text else id(src_text), src_suffix, test_bodies, legacy_rust)
            res = local.get(lkey)
            if res is None:
                res = sv(src_text, src_suffix, test_bodies=test_bodies, legacy_rust=legacy_rust)
                local[lkey] = res
            return res

        return _views

    for path in paths:
        p = pathlib.PurePosixPath(path)
        is_excluded = excluded(path)
        if (is_excluded and deal_randomness.excluded(path)) or not (p.suffix in SOURCE | CONFIG or project_portability.supported(path)):
            skipped.append({"path": path, "reason": "fixture/documentation/dependency" if is_excluded else "unsupported format"})
            continue
        oid = blob_oid_fn(path) if blob_oid_fn is not None else None
        is_vendor_rs = "vendor" in p.parts and path.endswith(".rs")
        ckey = (path, oid, repository, retired_key) if (file_cache is not None and oid is not None) else None
        if ckey is not None and ckey in file_cache:
            entry = file_cache[ckey]
            kind = entry[0]
            if kind == "skipped_compiler":
                skipped.append({"path": path, "reason": "protocol compiler output; scan its authored schema instead"})
                continue
            if kind == "error":
                _, err_msg, has_env_reads, dep_reads, dep_cargo_fp, dep_paths_fp, entry_declared = entry
                if _deps_valid(has_env_reads, dep_reads, dep_cargo_fp, dep_paths_fp, entry_declared):
                    reviewed.append(path)
                    unreadable.append({"path": path, "reason": err_msg})
                    continue
            elif kind == "ok":
                _, cached_rows, has_env_reads, dep_reads, dep_cargo_fp, dep_paths_fp, entry_declared = entry
                if _deps_valid(has_env_reads, dep_reads, dep_cargo_fp, dep_paths_fp, entry_declared):
                    reviewed.append(path)
                    rows = _copy_rows(cached_rows)
                    text = None
                    for row in rows:
                        lines = None if rescan else changed.get(path, set())
                        row["changed"] = lines is None or any(row["line"] <= n <= row["end_line"] for n in lines)
                        if row["rule"] == "cache-economy":
                            if text is None:
                                text = tree.read_model_source(path) if path.endswith(_SURROGATE_EXTS) else tree.read(path)
                            row["decision"] = cache_display_proof(tree, path, text, row["line"])
                        else:
                            row["decision"] = next((d for d in allowed if all(d[k] == row[k] for k in ("path", "line", "class", "rule", "source_sha256"))), None)
                        if row["rule"] in {"tenant-read-functions", "deal-randomness", "project-portability"}:
                            row["decision"] = None
                        findings.append(row)
                    continue
        # Legacy Java plugins contain non-UTF-8 comments. Preserve every byte
        # while recognizing ASCII syntax; other languages still fail unreadable.
        text = tree.read_model_source(path) if path.endswith(_SURROGATE_EXTS) else tree.read(path)
        if text is None:
            # Deleted paths are outside the head, not unreadable inputs.
            if path in path_set:
                unreadable.append({"path": path, "reason": "git blob unreadable"})
            continue
        if source_scope.compiler_generated(text):
            skipped.append({"path": path, "reason": "protocol compiler output; scan its authored schema instead"})
            if ckey is not None:
                file_cache[ckey] = ("skipped_compiler",)
            continue
        reviewed.append(path)
        env_tracker = [False] if ckey is not None else None
        dep_tree = _DepTrackTree(tree, blob_oid_fn) if ckey is not None else tree
        views_fn = _make_views(text, oid)
        try:
            rows = scan_file(path, text, repository, declared, retired, tree=dep_tree, _views=views_fn, _env_tracker=env_tracker)
        except (ValueError, SyntaxError) as error:
            err_msg = str(error)
            unreadable.append({"path": path, "reason": err_msg})
            if ckey is not None:
                file_cache[ckey] = (
                    "error", err_msg, env_tracker[0],
                    tuple(dep_tree.reads.items()),
                    _get_cargo_fp() if is_vendor_rs else None,
                    _get_paths_fp() if dep_tree.used_paths else None,
                    declared if env_tracker[0] else None,
                )
            continue
        if ckey is not None:
            file_cache[ckey] = (
                "ok", _copy_rows(rows), env_tracker[0],
                tuple(dep_tree.reads.items()),
                _get_cargo_fp() if is_vendor_rs else None,
                _get_paths_fp() if dep_tree.used_paths else None,
                declared if env_tracker[0] else None,
            )
        for row in rows:
            lines = None if rescan else changed.get(path, set())
            row["changed"] = lines is None or any(row["line"] <= n <= row["end_line"] for n in lines)
            row["decision"] = (cache_display_proof(tree, path, text, row["line"]) if row["rule"] == "cache-economy" else
                               next((d for d in allowed if all(d[k] == row[k] for k in ("path", "line", "class", "rule", "source_sha256"))), None))
            if row["rule"] in {"tenant-read-functions", "deal-randomness", "project-portability"}:
                row["decision"] = None
            findings.append(row)
    matched = {tuple(f[k] for k in ("path", "line", "class", "rule", "source_sha256")) for f in findings}
    for row in allowed:
        if tuple(row[k] for k in ("path", "line", "class", "rule", "source_sha256")) not in matched:
            unreadable.append({"path": ".github/architecture-decisions.json", "reason": "stale or mismatched architecture decision for " + row["path"]})
    return {"findings": findings, "unreadable": unreadable, "reviewed_paths": reviewed,
            "excluded_paths": skipped, "decisions": allowed, "decision_errors": decision_errors,
            "scope": "changed lines with full-file context" if changed is not None else "supported constructs in the supplied tree",
            "semantic_census_complete": False, "limits": LIMITS}


def baseline_key(row):
    return " ".join(("architecture", row["class"], row["rule"], quote(row["path"], safe="/._-"),
                     row["span_sha256"], str(row["occurrence"])))


def adoption_eligible(row, introduced_rules):
    """One permission predicate for the offline helper and required ratchet.

    Rust lexical coverage admits only findings wholly unseen by the frozen
    detector, not previously recognized but unrecorded debt or malformed spans.
    Exact base/head keys and cardinalities are checked by each caller.
    """
    rule = "architecture/" + row["class"] + "/" + row["rule"]
    return (rule in introduced_rules or
            rule == "architecture/C1/project-portability" and row["path"].endswith(".rs")
            and rule + "-rust-literals" in introduced_rules
            and not row.get("legacy_portability_keys")
            and not row.get("legacy_portability_seen_span")
            and not row.get("legacy_portability_literal_seen"))


def baseline_errors(findings, head_records, base_records, base_findings, introduced_rules=(), *, notice=lambda _: None, has_base=True):
    """The existing stack baseline can track exact inherited architecture debt.

    A record never suppresses a regression or replaces an exception decision.
    On adoption, it may seed only findings independently present at the base.
    Thereafter the recorded set can only shrink.
    """
    actual = {baseline_key(row) for row in findings}
    existing = ({baseline_key(row) for row in base_findings
                 if adoption_eligible(row, introduced_rules)}
                & {baseline_key(row) for row in findings
                   if adoption_eligible(row, introduced_rules)})
    for key in sorted(head_records - actual):
        notice("stale architecture baseline record: " + key + "; consider removing it")
    # Explicit constituent correction is not new destination debt. The old key must
    # independently resolve at the actual base to corrected literal occurrences;
    # those exact path/span/occurrences must survive at the head. Old records must
    # be removed in the same change. No equivalence is inferred from head text.
    replacements = portability_rekeys(base_findings, base_records)
    corrected = {key for members in replacements.values() for key in members}
    existing |= {new for old, members in replacements.items()
                 if (old not in head_records or old in corrected) and members <= actual for new in members}
    # An old key can collide with a different current occurrence after
    # renumbering. Once it resolves to corrected constituents it is no longer
    # an unconditional allowance, even when its bytes remain an actual key.
    allowed = (base_records - set(replacements)) | existing
    errors = ["architecture baseline cannot grow or seed new debt: " + key for key in sorted(head_records - allowed)]
    if has_base:
        inherited = {baseline_key(row) for row in base_findings}
        errors += ["stale architecture baseline cannot authorize a reintroduced departure: " + key
                   for key in sorted((head_records & base_records & actual) - inherited - existing)]
    for row in findings:
        row["baseline_recorded"] = baseline_key(row) in head_records
    return errors


def portability_rekeys(base_findings, base_records):
    """Enumerate corrected occurrences inside each exact recorded legacy span.

    A formerly overbroad token can contain several real literals; a raw token
    can instead wrap a formerly quoted-body finding. All members are individually
    identified at the base, never inferred from the head. Once a shifted
    occurrence is already recorded at the base under its current key and its
    legacy key belongs to an earlier current occurrence whose own legacy span
    was already retired, no rekey remains pending.
    """
    actual = {baseline_key(row) for row in base_findings}
    candidates = {}
    for row in base_findings:
        key = baseline_key(row)
        for old in row.get("legacy_portability_keys", ()):
            if old in base_records and old != key and (old not in actual or key not in base_records):
                candidates.setdefault(old, set()).add(key)
    return candidates
