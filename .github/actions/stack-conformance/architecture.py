"""C1–C7 checks used by stack-conformance; findings never contain source values.

These are structural regression checks, not a semantic census. Check the whole
changed file for context, then gate findings on added lines. Unsupported source
and configured decisions remain explicit in the JSON report.
"""
from __future__ import annotations

import ast
import datetime
import functools
import hashlib
import io
import json
import pathlib
import re
import tokenize
import tomllib

CONTRACT = "https://github.com/SylphxAI/skills/blob/main/skills/self-challenge/references/architecture.md"
BASELINE_MARKER = "# architecture-ratchet-v1"
SKIP = {"tests", "test", "fixtures", "__fixtures__", "__tests__", "examples", "docs",
        "node_modules", "vendor", "target", "dist", "build", ".next"}
SOURCE = {".rs", ".py", ".go", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".sh", ".bash"}
CONFIG = {".yml", ".yaml", ".toml"}
PLATFORM = {"SylphxAI/cloud", "SylphxAI/sylphx"}
CATALOG = pathlib.Path(__file__).resolve().parents[3] / "policy/architecture-repositories.json"
try:
    CATALOG_DATA = json.loads(CATALOG.read_text())
except (OSError, ValueError):
    CATALOG_DATA = None
READ = re.compile(r"\b(?:fetch|get_status|query|poll|probe|list_items|status)\w*\s*\(", re.I)
SLEEP = re.compile(r"\b(?:sleep\s*\(|sleep\s+\d|setInterval\s*\()")
LIMITS = [
    "Source rules support Rust/Go/JavaScript/TypeScript/Python/shell, TOML and ordinary YAML blocks; macros, templates and aliases may need semantic review.",
    "C1 recognizes explicit first-party/network-trust success branches; indirect authorization behavior requires semantic review.",
    "C2 recognizes provider constructors/secret environment names; it does not prove that all duplicate capabilities are absent.",
    "C3 checks shared-engine executable imports/constants and bundled company Skills; runtime-supplied tenant configuration is allowed.",
    "C4 recognizes literal environment reads and duplicate unconditional literal defaults in Python; dynamic settings and cross-module authority need review.",
    "C5 recognizes polling loops, transport sleeps, polling interval callbacks and declared request-driven warm floors. Unclassified HPA/KEDA workloads are not assumed request-driven.",
    "C6 recognizes unexplained lint suppressions and routes declared retired in a migration contract; arbitrary dead code needs caller analysis.",
    "C7 recognizes failure-only CI verdicts and merge-only completion branches; it cannot infer every product's acceptance contract.",
    "Only findings on changed lines fail the change gate. Unchanged or unread scope is not certified clean.",
]


def excluded(path):
    p = pathlib.PurePosixPath(path)
    return (bool(set(p.parts) & SKIP) or p.name.startswith("test_")
            or bool(re.search(r"(?:[._-](?:test|spec)|_test)\.", p.name)))


@functools.lru_cache(maxsize=None)
def downstream_pattern(repository):
    data = CATALOG_DATA
    if not isinstance(data, dict):
        raise ValueError("pinned repository catalog is unreadable")
    if data.get("version") != 1 or not isinstance(data.get("shared_layers"), dict):
        raise ValueError("invalid pinned repository catalog")
    layers = data["shared_layers"]
    scope = layers.get(repository)
    if scope is None:
        return None
    tokens = set(scope) | {org.lower() for org in scope}
    repositories = {row["id"]: row for row in data["repositories"]}
    for product in data["products"]:
        row = repositories[product["repository_id"]]
        org, name = row["full_name"].split("/", 1)
        if org in scope and row["full_name"] not in layers and not row["archived"]:
            if name not in {"website", "web", "api", "app", "main", "frontend", "backend"}:
                tokens.add(name)
            brand = product["product"]
            tokens.add(brand)
            tokens.add(re.sub(r"\s+", "-", brand.lower()))
    return "(?:" + "|".join(re.escape(n).replace(r"\-", "[_-]") for n in sorted(tokens, key=lambda n: (-len(n), n))) + ")"


def blank(text):
    return "".join("\n" if c == "\n" else " " for c in text)


def source_views(text, suffix):
    """Return comment-free text and code with literal contents masked; same offsets."""
    comment_free, code, comments = list(text), list(text), []
    if suffix == ".py":
        offsets = [0]
        for line in text.splitlines(keepends=True):
            offsets.append(offsets[-1] + len(line))
        try:
            tokens = tokenize.generate_tokens(io.StringIO(text).readline)
            for token in tokens:
                if token.type not in (tokenize.COMMENT, tokenize.STRING):
                    continue
                start = offsets[token.start[0] - 1] + token.start[1]
                end = offsets[token.end[0] - 1] + token.end[1]
                code[start:end] = blank(text[start:end])
                if token.type == tokenize.COMMENT:
                    comment_free[start:end] = blank(text[start:end])
                    comments.append((start, end))
            # A module/function docstring is documentation, not executable text.
            for node in ast.walk(ast.parse(text)):
                if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    lines = text.splitlines(keepends=True)
                    start = offsets[node.lineno - 1] + len(lines[node.lineno - 1].encode()[:node.col_offset].decode())
                    end = offsets[node.end_lineno - 1] + len(lines[node.end_lineno - 1].encode()[:node.end_col_offset].decode())
                    comment_free[start:end] = blank(text[start:end])
        except (tokenize.TokenError, IndentationError, SyntaxError):
            raise ValueError("Python could not be parsed; this file is not reviewed clean") from None
    else:
        char_or_string = r"'(?:\\.|[^'\\\n])'" if suffix == ".rs" else r"'(?:\\.|[^'\\])*'"
        token = re.compile(r'//[^\n]*|/\*.*?\*/|\br(\#*)"[\s\S]*?"\1|"(?:\\.|[^"\\])*"|' + char_or_string + r'|`(?:\\.|[^`\\])*`', re.S)
        for match in token.finditer(text):
            value = match.group()
            code[match.start():match.end()] = blank(value)
            if value.startswith(("//", "/*")):
                comment_free[match.start():match.end()] = blank(value)
                comments.append((match.start(), match.end()))
        if suffix == ".rs":
            # Inline test modules are fixtures too; do not hide adjacent production.
            for match in re.finditer(r"#\[cfg\(test\)\]\s*mod\s+\w+\s*\{", "".join(code)):
                end = brace_end("".join(code), match.end())
                comment_free[match.start():end] = blank(text[match.start():end])
                code[match.start():end] = blank(text[match.start():end])
            for match in re.finditer(r"#\[(?:(?:tokio|async_std)::)?test(?:\([^\]]*\))?\]\s*"
                                     r"(?:#\[[^\]]*\]\s*)*(?:async\s+)?fn\s+\w+[^;{]*\{", "".join(code)):
                end = brace_end("".join(code), match.end())
                comment_free[match.start():end] = blank(text[match.start():end])
                code[match.start():end] = blank(text[match.start():end])
        if suffix in {".sh", ".bash"}:
            for match in re.finditer(r"(?m)^\s*#.*$", text):
                comment_free[match.start():match.end()] = blank(match.group())
                code[match.start():match.end()] = blank(match.group())
                comments.append((match.start(), match.end()))
    return "".join(comment_free), "".join(code), comments


def brace_end(code, start):
    return matching_end(code, start, "{", "}")


def matching_end(code, start, opening, closing):
    depth, end = 1, start
    while depth and end < len(code):
        depth += (code[end] == opening) - (code[end] == closing)
        end += 1
    return end


def loops(clean, code, suffix):
    """(start,end,bounded) offsets, preserving the body of multiline loops."""
    if suffix == ".py":
        lines = clean.splitlines(keepends=True)
        offsets = [0]
        for line in lines:
            offsets.append(offsets[-1] + len(line))
        tree = ast.parse(clean)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.While, ast.For, ast.AsyncFor)):
                continue
            header = lines[node.lineno - 1]
            body = clean[offsets[node.lineno - 1]:offsets[node.end_lineno]]
            bounded = bool((isinstance(node, ast.For) and isinstance(node.iter, (ast.List, ast.Tuple, ast.Set)))
                           or (isinstance(node, ast.For) and isinstance(node.iter, ast.Call)
                               and isinstance(node.iter.func, ast.Name) and node.iter.func.id == "range")
                           or re.search(r"(?:monotonic\(\)|time\(\)|now\(\))\s*<", header)
                           or (re.search(r"attempt\w*\s*<", header) and re.search(r"attempt\w*\s*\+=\s*[1-9]", body)))
            yield offsets[node.lineno - 1], offsets[node.end_lineno], bounded
        return
    if suffix in {".sh", ".bash"}:
        for m in re.finditer(r"\b(?:while|for)\b([\s\S]*?)\bdone\b", code):
            yield m.start(), m.end(), bool(re.match(r"for\s+\w+\s+in\b", m.group()))
        return
    for m in re.finditer(r"\b(?:while|loop|for)\b[^{}]*\{", code):
        end = brace_end(code, m.end())
        header = code[m.start():m.end()]
        bounded = bool(re.search(r"\bfor\b.*\bin\s+\d+\s*\.\.|now\(\)\s*<", header)
                       or (re.search(r"attempt\w*\s*<", header)
                           and re.search(r"attempt\w*\s*(?:\+\+|\+=\s*[1-9])", code[m.start():end])))
        yield m.start(), end, bounded


def finding(path, text, at, cls, rule, reason, end=None):
    line = text.count("\n", 0, at) + 1
    return {"path": path, "line": line, "end_line": text.count("\n", 0, end or at) + 1,
            "class": cls, "rule": rule, "reason": reason,
            "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "contract": CONTRACT + ("#scale" if cls == "C5" else "#hard-lines")}


def env_declarations(tree):
    names = set()
    for path in tree.paths():
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


def owns_provider(repository, path, capability):
    if repository not in PLATFORM:
        return False
    if re.search(r"(?:^|/)(?:providers?|adapters?)(?:/|$)", path):
        return True
    return bool(re.search(r"^services/" + capability + r"/|crates/sylphx-" + capability + r"/", path)
                or capability == "money" and path.startswith("kernel/billing/"))


def scan_file(path, text, repository, declared, retired_routes):
    suffix = pathlib.PurePosixPath(path).suffix
    if excluded(path) or suffix not in SOURCE | CONFIG:
        return []
    out = []
    add = lambda at, cls, rule, why, end=None: out.append(finding(path, text, at, cls, rule, why, end))
    if suffix == ".toml":
        try:
            tomllib.loads(text)
        except ValueError:
            raise ValueError("TOML could not be parsed; this file is not reviewed clean") from None
        if pathlib.PurePosixPath(path).name == "sylphx.toml":
            masked = list(text)
            for m in re.finditer(r'"""[\s\S]*?"""|\x27\x27\x27[\s\S]*?\x27\x27\x27|"(?:\\.|[^"\\])*"|\x27[^\x27]*\x27|#[^\n]*', text):
                masked[m.start():m.end()] = blank(m.group())
            for m in re.finditer(r'(?m)^[^\S\n]*min_instances\s*=\s*([1-9]\d*)\b', "".join(masked)):
                add(m.start(), "C5", "idle-floor", "Positive service idle floor requires a recorded always-on decision with its reason")
        return out
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
        return out
    clean, code, comments = source_views(text, suffix)
    # Require a control-flow token in real code; quoted examples never count.
    branch = re.compile(r"\bif\b[^\n{}:]*?(?:first_party|is_internal|is_loopback|is_private|cluster_local|tenant\w*\s*===?\s*[\"'](?:sylphx|cubeage)[^\"']*[\"'])[^\n{}:]*[{:]", re.I)
    for m in branch.finditer(clean):
        if code[m.start():m.start()+2] != "if":
            continue
        end = brace_end(code, m.end()) if m.group().endswith("{") else m.end() + len(clean[m.end():].split("\n\n")[0])
        body = code[m.end():end]
        if re.match(r"\s*return\s+(?:true\b|True\b|Ok\s*\()", body):
            add(m.start(), "C1", "privileged-branch", "First-party identity or network position reaches an unconditional success return", end)
    provider = re.compile(r"\b(?:new\s+Stripe|stripe\s*::\s*Client\s*::\s*new|stripe\.Stripe|(?:STRIPE|TWILIO|VONAGE|AUTH0)_(?:SECRET|API|AUTH)[A-Z_]*(?:KEY|TOKEN))\b")
    for m in provider.finditer(clean):
        capability = "money" if "stripe" in m.group().lower() else "auth" if "AUTH0" in m.group() else "notify"
        if owns_provider(repository, path, capability):
            continue
        # An env name is literal data attached to a real read; constructor must be code.
        line = clean[clean.rfind("\n", 0, m.start())+1:clean.find("\n", m.end()) if "\n" in clean[m.end():] else len(clean)]
        if code[m.start():m.end()].strip() or re.search(r"getenv|env::var|process\.env|environ", line):
            add(m.start(), "C2", "provider-bypass", "Product uses provider capability directly; use its ordinary platform operation")
    provider_host = re.compile(r'https?://(?:api\.stripe\.com|rest\.nexmo\.com|(?:buy|sandbox)\.itunes\.apple\.com|androidpublisher\.googleapis\.com|fcm\.googleapis\.com|(?:api\.|queue\.)?fal\.ai|api\.replicate\.com)(?=[:/"\x27\s]|$)', re.I)
    for m in provider_host.finditer(clean):
        capability = "money" if any(s in m.group().lower() for s in ("stripe", "itunes", "androidpublisher")) else "ai" if any(s in m.group().lower() for s in ("fal.ai", "replicate")) else "notify"
        if owns_provider(repository, path, capability):
            continue
        # Literal host in executable code; comments/docstrings were removed.
        line_start = clean.rfind("\n", 0, m.start()) + 1
        if code[line_start:m.start()].strip():
            add(m.start(), "C2", "provider-bypass", "Product calls an offered capability's provider host directly")
    downstream = downstream_pattern(repository)
    if downstream:
        for m in re.finditer(r"\b(?:use|import|from)\s+" + downstream + r"(?=\W|$)", code):
            add(m.start(), "C3", "downstream-import", "Shared engine imports downstream company/product behavior")
        for m in re.finditer(r'\b(?:from\s+|require\s*\(\s*)["\x27]@?' + downstream + r'(?=[/"\x27])[^"\x27]*["\x27]', clean):
            if code[m.start():m.start()+4].strip():
                add(m.start(), "C3", "downstream-import", "Shared engine imports a downstream product module")
        for m in re.finditer(r"\b(?:const|static|let)\b[^\n;=]*=\s*[\"'](?:" + downstream + r"|org_[A-Za-z0-9]+|tenant_[A-Za-z0-9]+)[\"']", clean):
            if code[m.start():m.start()+3].strip():
                add(m.start(), "C3", "downstream-config", "Shared engine compiles a downstream identity into executable configuration")
        if "bundled_skills" in pathlib.PurePosixPath(path).name and clean.strip():
            add(0, "C3", "bundled-method", "Company Skill bodies belong in replaceable configuration, not engine source")
    reads = re.compile(r'''(?:\b(?:std::)?env::var(?:_os)?\s*\(\s*|\bos\.(?:getenv\s*\(|environ(?:\.get\s*\(|\s*\[))\s*|\bprocess\.env\s*\[\s*)["']([A-Z][A-Z0-9_]+)["']|\bprocess\.env\.([A-Z][A-Z0-9_]+)''')
    for m in reads.finditer(clean):
        name = m.group(1) or m.group(2)
        if not code[m.start():m.start()+2].strip():
            continue
        if name not in declared and not name.startswith(("GITHUB_", "RUNNER_")) and name not in {"CI", "HOME", "PATH", "TMPDIR"}:
            add(m.start(), "C4", "undeclared-setting", "Literal environment read lacks a declaration in the environment schema or sample")
    if suffix == ".py":
        defaults = {}
        for node in ast.parse(text).body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id.startswith("DEFAULT_"):
                        if target.id in defaults and defaults[target.id] != node.value.value:
                            at = sum(map(len, clean.splitlines(keepends=True)[:node.lineno-1]))
                            add(at, "C4", "competing-default", "The same unconditional default has conflicting literal authorities")
                        defaults[target.id] = node.value.value
    for start, end, bounded in loops(text if suffix == ".py" else clean, code, suffix):
        if bounded:
            continue
        if re.search(r"\b(?:sleep\s*\(|sleep\s+\d|(?:fetch|get_status|query|poll|probe|list_items|status)\w*\s*\()", code[start:end], re.I):
            add(start, "C5", "polling-loop", "An unbounded loop advances by a transport read or sleep instead of an event", end)
    for m in re.finditer(r"\bsetInterval\s*\(", code):
        end = matching_end(code, m.end(), "(", ")")
        if READ.search(code[m.end():end]):
            add(m.start(), "C5", "polling-timer", "An interval advances ordinary state reads instead of subscribing to events", end)
    suppressions = re.finditer(r"#\s*\[allow\s*\([^)]*\)\]|eslint-disable[^\n]*|#\s*(?:noqa|type:\s*ignore)[^\n]*", text)
    for m in suppressions:
        # A suppression must be an actual comment or Rust attribute, not quoted text.
        if text[m.start():m.start()+2] != "#[" and not any(start <= m.start() < end for start, end in comments):
            continue
        if text[m.start():m.start()+2] == "#[" and not code[m.start():m.start()+2].strip():
            continue
        line = text[text.rfind("\n", 0, m.start())+1:text.find("\n", m.end()) if "\n" in text[m.end():] else len(text)]
        if not re.search(r"(?:reason|because)\s*[:=]?\s*\S+(?:\s+\S+){2,}", line, re.I):
            add(m.start(), "C6", "unexplained-suppression", "Lint suppression needs its narrow constraint and reason")
    for route in retired_routes:
        for m in re.finditer(r"\b(?:route|path|add_route)\s*\(\s*[\"']" + re.escape(route) + r"[\"']", clean):
            if code[m.start():m.start()+4].strip():
                add(m.start(), "C6", "retired-route", "A completed migration contract marks this still-executable route retired")
    for m in re.finditer(r"\bif\b[^\n{}:]*\bmerged\b[^\n{}:]*[{:][\s\S]{0,180}?\b(?:settle_completed|mark_complete|complete_goal)\s*\(", code):
        header = re.split(r"[{:}]", m.group(), maxsplit=1)[0]
        terms = re.split(r"&&|\band\b", header)
        positive_evidence = any(re.search(r"\b(?:evidence|receipt|deployed|verified)\b", term)
                                and not re.search(r"!|\bnot\b|\|\||\bor\b|==\s*(?:false|False|None|null)", term)
                                for term in terms[1:])
        if not positive_evidence:
            add(m.start(), "C7", "merge-only-completion", "Merge-only branch settles delivery without required environment evidence")
    unique = {(f["line"], f["class"], f["rule"]): f for f in out}
    return list(unique.values())


def decisions(text, repository, today):
    if text is None:
        return []
    data = json.loads(text)
    if not isinstance(data, dict) or set(data) != {"version", "decisions"} or data["version"] != 1:
        raise ValueError("invalid architecture decisions schema")
    rows = data["decisions"]
    if not isinstance(rows, list):
        raise ValueError("architecture decisions must be an array")
    seen = set()
    for row in rows:
        required = {"repository", "path", "line", "class", "rule", "source_sha256", "owner", "reason", "contract", "expires", "authority", "evidence", "reconsideration"}
        if (not isinstance(row, dict) or set(row) != required
                or any(not isinstance(v, str) or not v.strip() for k, v in row.items() if k != "line")
                or type(row.get("line")) is not int or row["line"] < 1):
            raise ValueError("decision must name exact finding, owner, reason, contract and expiry")
        if row["repository"] != repository or row["class"] not in {f"C{i}" for i in range(1, 8)} or any(c in row["path"] for c in "*?["):
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
            raise ValueError("architecture decision expired: " + row["path"])
    return rows


def evaluate(tree, repository, changed, today):
    """Pure git-tree input -> report. No network, environment, clock or mutation."""
    declared = env_declarations(tree)
    retired = []
    migration = tree.read(".github/architecture-migrations.json")
    if migration:
        data = json.loads(migration)
        retired = data.get("retired_routes", []) if data.get("completed") is True else []
        if not isinstance(retired, list) or any(not isinstance(r, str) or not r.startswith("/") for r in retired):
            raise ValueError("invalid retired-route migration contract")
    allowed = decisions(tree.read(".github/architecture-decisions.json"), repository, today)
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
    for path in sorted(tree.paths()):
        if excluded(path) or pathlib.PurePosixPath(path).suffix not in SOURCE | CONFIG:
            skipped.append({"path": path, "reason": "fixture/documentation/dependency" if excluded(path) else "unsupported format"})
            continue
        text = tree.read(path)
        if text is None:
            # Deleted paths are outside the head, not unreadable inputs.
            if path in tree.paths():
                unreadable.append({"path": path, "reason": "git blob unreadable"})
            continue
        reviewed.append(path)
        try:
            rows = scan_file(path, text, repository, declared, retired)
        except (ValueError, SyntaxError) as error:
            unreadable.append({"path": path, "reason": str(error)})
            continue
        for row in rows:
            lines = None if rescan else changed.get(path, set())
            row["changed"] = lines is None or any(row["line"] <= n <= row["end_line"] for n in lines)
            row["decision"] = next((d for d in allowed if all(d[k] == row[k] for k in ("path", "line", "class", "rule", "source_sha256"))), None)
            findings.append(row)
    matched = {tuple(f[k] for k in ("path", "line", "class", "rule", "source_sha256")) for f in findings}
    for row in allowed:
        if tuple(row[k] for k in ("path", "line", "class", "rule", "source_sha256")) not in matched:
            unreadable.append({"path": ".github/architecture-decisions.json", "reason": "stale or mismatched architecture decision for " + row["path"]})
    return {"findings": findings, "unreadable": unreadable, "reviewed_paths": reviewed,
            "excluded_paths": skipped, "decisions": allowed,
            "scope": "changed lines with full-file context" if changed is not None else "supported constructs in the supplied tree",
            "semantic_census_complete": False, "limits": LIMITS}


def baseline_key(row):
    return " ".join(("architecture", row["class"], row["rule"], row["path"] + ":" + str(row["line"]), row["source_sha256"]))


def baseline_errors(findings, head_records, base_records, base_findings, adopted=False):
    """The existing stack baseline can track exact inherited architecture debt.

    A record never suppresses a regression or replaces an exception decision.
    On adoption, it may seed only findings independently present at the base.
    Thereafter the recorded set can only shrink.
    """
    actual = {baseline_key(row) for row in findings}
    existing = {baseline_key(row) for row in base_findings}
    errors = ["stale architecture baseline record: " + key for key in sorted(head_records - actual)]
    allowed = base_records if adopted else existing
    errors += ["architecture baseline cannot grow or seed new debt: " + key for key in sorted(head_records - allowed)]
    for row in findings:
        row["baseline_recorded"] = baseline_key(row) in head_records
    return errors
