"""Offline catalog-alias policy and shrinking, per-repository model-pin debt."""
from __future__ import annotations

import collections
import ast
import json
import importlib.util
import pathlib
import re
from urllib.parse import quote

POLICY = pathlib.Path(__file__).resolve().parents[3] / "policy/model-aliases.json"
BASELINE_MARKER = "# model-alias-ratchet-v1"
SELECTOR_RULE = "model-selector"
SELECTOR_MARKER = "# stack-rule-adopted-v1 " + SELECTOR_RULE
CONTRACT = ("https://github.com/SylphxAI/cloud/blob/main/docs/adr/"
            "ADR-01M58DAMBRRTBRYRD3HSQQVD1S-model-identity-naming-and-price-binding.md")
_scope_spec = importlib.util.spec_from_file_location("source_scope", pathlib.Path(__file__).with_name("source_scope.py"))
source_scope = importlib.util.module_from_spec(_scope_spec)
_scope_spec.loader.exec_module(source_scope)

SOURCE = {".rs", ".py", ".go", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
          ".cs", ".java", ".kt", ".swift", ".dart", ".c", ".h", ".cpp", ".hpp",
          ".vue", ".svelte", ".sh", ".bash", ".json", ".toml", ".yaml", ".yml", ".sql", ".proto"}
SKIP = {"tests", "test", "fixtures", "__fixtures__", "__tests__", "docs", "examples",
        "node_modules", "vendor", "third_party", "thirdparty", "third-party", "external",
        "target", "dist", "build", ".next", ".git"}

# These are owners of catalog records and upstream locators, not product
# callers. Scope by repository AND path: a product cannot inherit an exemption
# by naming a directory after a platform component.
EXEMPTIONS = [
    {"repository": "SylphxAI/.github", "path": ".github/actions/stack-conformance/model_pins.py",
     "reason": "The static recognizer vocabulary identifies forbidden upstream names and never selects a runtime model."},
    {"repository": "SylphxAI/cloud", "path": "services/ai/crates/ai-core/src/",
     "reason": "AI catalog identity, revision and offering policy owns vendor model identities."},
    {"repository": "SylphxAI/cloud", "path": "services/ai/crates/ai-server/src/capabilities/control_plane/",
     "reason": "AI catalog ingestion, alias resolution and provider-locator declarations own upstream ids."},
    {"repository": "SylphxAI/cloud", "path": "services/ai/migrations/",
     "reason": "AI catalog migrations declare stored model and offering identities."},
    {"repository": "SylphxAI/cloud", "path": "services/ai/crates/ai-server/src/bin/ai_dev_seed.rs",
     "reason": "The development catalog seeder declares model and offering rows, not a product request default."},
    {"repository": "SylphxAI/cloud", "path": "services/ai/crates/ai-server/src/capabilities/capacity/adapters/grok_quota.rs",
     "reason": "The provider quota adapter owns the upstream billing model locator."},
    {"repository": "SylphxAI/cloud", "path": "services/ai/conformance/",
     "reason": "Provider protocol conformance schemas are vendor-contract test fixtures."},
    {"repository": "SylphxAI/cloud", "path": "contracts/compat/openresponses/",
     "reason": "Imported OpenResponses compatibility schemas are upstream contract fixtures."},
    {"repository": "SylphxAI/cloud", "path": "services/ai/scripts/compliance/openresponses/src/generated/",
     "reason": "Generated upstream OpenResponses conformance fixtures retain the provider contract's examples."},
    {"repository": "SylphxAI/cloud", "path": "scripts/ci/verify-fresh-database-bootstrap.sh",
     "reason": "The database-bootstrap integration test asserts the catalog's seeded records."},
]
DIAGNOSTIC_LISTS = [
    {"repository": "SylphxAI/spiron", "path": "crates/spiron-tools/src/tool_packs.rs",
     "function": "provider_portable_tool_surface_proof",
     "schema": "spi_tool_surface_model_independent_projection/v1",
     "kind": "local_model_independent_projection_check",
     "reason": "Diagnostic fixture tags only; the projection receives no model id."},
]

# Recognition is deliberately separate from authorization. Versioned and
# undated upstream names are candidates; the alias contract authorizes names.
# An optional pinned catalog can tighten that contract to exact declarations.
FAMILIES = ("deepseek", "claude", "gpt", "chatgpt", "gemini", "grok", "mistral", "mixtral", "codestral",
            "ministral", "magistral", "devstral", "pixtral", "llama", "qwen", "qwq", "kimi", "glm", "doubao",
            "command-r", "jamba", "nova", "phi", "gemma", "yi", "sonar", "text-embedding", "whisper", "dall-e", "tts")
FAMILY = (r"(?:(?:" + "|".join(FAMILIES) + r")[.-][a-z0-9][a-z0-9.:-]*"
          r"|o[134](?:[-.][a-z0-9][a-z0-9.:-]*)?)")
TOKEN = re.compile(r"(?<![\w./-])(?:[a-z0-9][a-z0-9_.-]*/)*" + FAMILY + r"(?![\w/-])", re.I)
IDENTIFIER = re.compile(r"(?<![\w./:-])[a-z0-9][a-z0-9_.:-]*(?:/[a-z0-9][a-z0-9_.:-]*)+(?![\w/:-])", re.I)
BARE_FAMILY = re.compile(r"(?:" + "|".join(name for name in FAMILIES if name != "tts")
                         + r"|o[134])(?:[.-][a-z0-9][a-z0-9.:-]*)?|tts[.-][a-z0-9][a-z0-9.:-]*", re.I)
ALIAS = re.compile(r"[a-z0-9][a-z0-9_.-]*/[a-z0-9][a-z0-9_.-]*-latest")
VENDOR_NAMESPACES = {"anthropic", "openai", "deepseek", "google", "mistralai", "meta-llama",
                     "qwen", "moonshotai", "x-ai", "z-ai", "cohere", "amazon", "microsoft", "soniox"}
LIMITS = [
    "Recognizes literal vendor model names in supported source/configuration; computed or encoded model ids need semantic review.",
    "Alias existence and its resolved revision are checked by the catalog/runtime; optional catalog snapshots can tighten admission offline.",
]


def load_aliases(path=None):
    if path is None:
        return None  # Default: the product-facing <provider>/<line>-latest contract.
    data = json.loads(pathlib.Path(path).read_text())
    if data.get("version") != 1 or not isinstance(data.get("aliases"), list):
        raise ValueError("invalid pinned model-alias declaration")
    source = data.get("source", {})
    if (source.get("repository") != "SylphxAI/cloud"
            or not re.fullmatch(r"[0-9a-f]{40}", source.get("commit", ""))
            or not source.get("path")
            or not re.fullmatch(r"[0-9a-f]{64}", source.get("sha256", ""))):
        raise ValueError("model aliases require an immutable catalog source and content digest")
    aliases = data["aliases"]
    if not aliases or any(not isinstance(a, str) or not IDENTIFIER.fullmatch(a) for a in aliases):
        raise ValueError("catalog alias ids must be nonempty namespaced identifiers")
    if len(aliases) != len(set(aliases)):
        raise ValueError("duplicate catalog alias declaration")
    return frozenset(aliases)


def excluded(path):
    p = pathlib.PurePosixPath(path)
    return (bool({part.casefold() for part in p.parts} & SKIP)
            or any(part.casefold().endswith(("-fixtures", "_fixtures")) for part in p.parts[:-1])
            or p.name.startswith(("test_", "tests_", "test-", "tests-", "smoke_", "smoke-"))
            or p.stem in {"test", "tests", "spec", "smoke", "smokes"}
            or bool(re.search(r"(?:[._-](?:tests?|spec))\.", p.name)))


def exemption(repository, path):
    return next((row for row in EXEMPTIONS
                 if row["repository"] == repository and
                 (path.startswith(row["path"]) if row["path"].endswith("/") else path == row["path"])), None)


def config_view(text, suffix):
    """Mask comments, retaining strings and offsets in data/config formats."""
    comment = r"--[^\n]*|/\*.*?\*/" if suffix == ".sql" else r"\#[^\n]*"
    if suffix in {".json", ".proto"}:
        comment = r"//[^\n]*|/\*.*?\*/"
    token = re.compile(r'"""[\s\S]*?"""|\x27\x27\x27[\s\S]*?\x27\x27\x27|"(?:\\.|[^"\\])*"|\x27(?:\\.|[^\x27\\])*\x27|' + comment, re.S)
    clean = list(text)
    for m in token.finditer(text):
        if m.group().startswith(("#", "--", "//", "/*")):
            clean[m.start():m.end()] = ["\n" if c == "\n" else " " for c in m.group()]
    return "".join(clean)


def scalar_model(clean, start, end, config):
    """Require a complete scalar, not a model-shaped word in prose or a path."""
    before, after = clean[start - 1:start], clean[end:end + 1]
    if before in {"\"", "'"} or before == "`" and not config:
        if config and clean[start - 2:start - 1] == "\\":
            return False  # Quotes inside a description are not JSON scalar delimiters.
        return after == before or clean[end:end + 2] == "\\" + before
    if not config:
        return False
    line_start = clean.rfind("\n", 0, start) + 1
    line_end = clean.find("\n", end)
    prefix = clean[line_start:start].rstrip()
    suffix = clean[end:line_end if line_end >= 0 else len(clean)].strip()
    if not prefix:
        prefix = clean[:start].rstrip()
    return prefix.endswith((":", "=")) and not suffix


def model_selector(clean, start, code=None):
    """Recognize selection fields, constants and calls, across line breaks.

    Inspect the adjacent expression rather than any earlier word containing
    'model': repository paths and provider dependencies are not selectors.
    """
    opening = clean.rfind("{", 0, start)
    if opening >= 0:
        view = code if code is not None else clean
        declaration_start = max(view.rfind(ch, 0, opening) for ch in ";{}\n") + 1
        declaration = view[declaration_start:opening]
        if re.search(r"\b\w*synonym\w*\s*=\s*$", declaration, re.I):
            return False  # Search vocabulary labels do not select a model.
    boundary = max(clean.rfind(delimiter, 0, start) for delimiter in ";{},")
    prefix = clean[boundary + 1:start].rstrip("\"'` \t\r\n")
    # An array item, fallback, optional wrapper or typed assignment keeps the
    # same selecting field. Stop at a statement/object boundary; quoted values
    # cannot supply selector names for their neighbours.
    candidate = re.search(
        r"(?:^|[\s.,(\[{])[\"']?(?P<name>[A-Za-z_$][\w$]*)[\"']?\s*"
        r"(?:\s*:\s*(?:&(?:'static\s+)?str|[A-Za-z_][\w:<>?\[\] &.]*))?"
        r"\s*(?P<operator>:=|[:=]|\()\s*(?:[\w.]+\s*\(\s*)?"
        r"(?:\[\s*)?$", prefix)
    if candidate is None:
        # Null-coalescing / environment fallbacks are still hard-coded
        # defaults for the directly assigned model selector.
        candidate = re.search(
            r"\b(?P<name>[A-Za-z_$][\w$]*)\s*[:=]\s*"
            r"[^;{}\n]*?(?:\?\?|\|\|)\s*$", prefix)
    if candidate is None:
        # Every literal in a declared model list is a selector, including
        # later items whose immediate prefix is another literal and a comma.
        view = code if code is not None else clean
        opening = view.rfind("[", 0, start)
        if opening > view.rfind("]", 0, start) and opening + 1 < start:
            return model_selector(clean, opening + 1, code)
        return False
    if code is not None:
        at = boundary + 1 + candidate.start("name")
        end = boundary + 1 + candidate.end("name")
        if not code[at:end].strip():
            # A quoted object key has a real colon outside its string. Words
            # inside a description literal cannot select the next literal.
            tail = end + 1
            while tail < start and clean[tail].isspace():
                tail += 1
            if not (clean[at - 1:at] == clean[end:end + 1] in {"\"", "'"}
                    and code[tail:tail + 1] == ":"):
                return False
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", candidate["name"]).lower()
    if name.endswith("models") and not prefix.endswith("["):
        return False  # A family-name policy string is not a selected model.
    if (candidate.groupdict().get("operator") == "(" and not re.fullmatch(
                r"(?:(?:language|chat|text|image|embedding|ai|llm|select|set|get|with|resolve)_)?model", name)):
        return False  # Classifier/parser vocabulary is not a selection call.
    return bool(re.search(r"(?:^|_)(?:models?|model_(?:id|name|slug))(?:_(?:default|fallback))?$", name))


def sql_selection(clean, start):
    """A comparison identifies existing rows; SET/VALUES/DEFAULT select values."""
    view = re.sub(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"",
                  lambda match: "".join("\n" if ch == "\n" else " " for ch in match.group()), clean)
    statement = view[view.rfind(";", 0, start) + 1:start]
    clauses = re.findall(r"\b(where|on|having|when|set|values|default|then|else)\b", statement, re.I)
    return not clauses or clauses[-1].lower() not in {"where", "on", "having", "when"}


def metadata_ranges(clean, suffix):
    """Locate classifier membership and weight provenance, leaving defaults visible."""
    ranges = []
    if suffix == ".json":
        decoder = json.JSONDecoder()
        space = re.compile(r"\s*")

        def visit(at, path):
            at = space.match(clean, at).end()
            if clean[at] == "{":
                at = space.match(clean, at + 1).end()
                while clean[at] != "}":
                    key, at = decoder.raw_decode(clean, at)
                    at = space.match(clean, at).end()
                    if not isinstance(key, str) or clean[at] != ":":
                        raise ValueError("invalid JSON object")
                    at = space.match(clean, visit(at + 1, path + (key,))).end()
                    if clean[at] != ",":
                        break
                    at = space.match(clean, at + 1).end()
                if clean[at] != "}":
                    raise ValueError("invalid JSON object")
                return at + 1
            if clean[at] == "[":
                at, index = space.match(clean, at + 1).end(), 0
                while clean[at] != "]":
                    at = space.match(clean, visit(at, path + (index,))).end()
                    index += 1
                    if clean[at] != ",":
                        break
                    at = space.match(clean, at + 1).end()
                if clean[at] != "]":
                    raise ValueError("invalid JSON array")
                return at + 1
            value, end = decoder.raw_decode(clean, at)
            keys = [re.sub(r"[_-]", "", key).lower() for key in path if isinstance(key, str)]
            if (isinstance(value, str) and keys and keys[-1] in {"model", "models"}
                    and {"taxonomy", "quotatiers", "modellimits", "match"} & set(keys[:-1])):
                ranges.append((at, end, "Classifier membership identifies existing models; it selects no request/default."))
            return end

        try:
            if space.match(clean, visit(0, ())).end() != len(clean):
                return []
        except (ValueError, IndexError, RecursionError):
            return []  # No metadata exemption without a complete parsed declaration.
    elif suffix == ".py":
        lines = clean.splitlines(keepends=True)
        offsets, total = [], 0
        for line in lines:
            offsets.append(total)
            total += len(line)
        for node in ast.walk(ast.parse(clean)):
            if not isinstance(node, ast.Dict):
                continue
            fields = {key.value: value for key, value in zip(node.keys, node.values)
                      if isinstance(key, ast.Constant) and isinstance(key.value, str)}
            if not {"repo", "revision", "sha256", "base_model"} <= fields.keys() or not {"license", "licence"} & fields.keys():
                continue
            value = fields["base_model"]
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                start = offsets[value.lineno - 1] + len(lines[value.lineno - 1].encode()[:value.col_offset].decode())
                end = offsets[value.end_lineno - 1] + len(lines[value.end_lineno - 1].encode()[:value.end_col_offset].decode())
                ranges.append((start, end, "Digest-bound weight provenance records an upstream identity; inference uses its supplied model directory."))
    return ranges


def balanced_end(code, start, opening, closing):
    depth, end = 1, start
    while depth and end < len(code):
        depth += (code[end] == opening) - (code[end] == closing)
        end += 1
    return end if not depth else None


def diagnostic_ranges(repository, path, clean, code):
    ranges = []
    for entry in DIAGNOSTIC_LISTS:
        if entry["repository"] != repository or entry["path"] != path or code is None:
            continue
        function = re.search(r"\bfn\s+" + re.escape(entry["function"]) + r"\([^;{}]*\)[^{;]*\{", code)
        if function is None:
            continue
        end = balanced_end(code, function.end(), "{", "}")
        if end is None:
            continue
        body = code[function.end():end]
        if len(re.findall(r"\bmodels\b", body)) != 2:
            continue
        declaration = re.search(r"\blet\s+models\s*=\s*\[", body)
        loop = re.search(r"\bfor\s+model\s+in\s+models\s*\{", body)
        if declaration is None or loop is None:
            continue
        loop_end = balanced_end(body, loop.end(), "{", "}")
        if loop_end is None or len(re.findall(r"\bmodel\b", body[loop.end():loop_end])) != 1:
            continue
        original = clean[function.end():end]
        if not re.search(r'"modelId"\s*:\s*model\b', original[loop.end():loop_end]):
            continue
        if not all(re.search('"' + key + r'"\s*:\s*"' + re.escape(entry[key]) + '"', original)
                   for key in ("schema", "kind")):
            continue
        start = function.end() + declaration.end()
        finish = balanced_end(code, start, "[", "]")
        if finish is not None:
            ranges.append((start, finish, entry["reason"]))
    return ranges


def scan(tree, repository, aliases, source_views):
    findings, unreadable, exemptions = [], [], []
    vendors = VENDOR_NAMESPACES | {alias.split("/", 1)[0] for alias in aliases or ()}
    for path in sorted(tree.paths()):
        suffix = pathlib.PurePosixPath(path).suffix
        if excluded(path) or suffix not in SOURCE:
            continue
        allowed = exemption(repository, path)
        if allowed:
            exemptions.append({"path": path, "reason": allowed["reason"]})
            continue
        text = tree.read_model_source(path)
        if text is None:
            unreadable.append({"path": path, "reason": "git blob unreadable"})
            continue
        if source_scope.compiler_generated(text):
            exemptions.append({"path": path, "reason": "protocol compiler output; scan its authored schema instead"})
            continue
        # Most blobs contain no model-shaped token; they need no parser. This
        # also avoids treating unrelated legacy language syntax as model debt.
        # Literal substring searches run in C and avoid the full boundary regex
        # across large generated protocols and data assets with no candidate.
        lowered = text.lower()
        if "model" not in lowered and not any(family + separator in lowered for family in FAMILIES for separator in ("-", ".")) and not re.search(
                r"(?<![\w.-])o[134](?!\w)", lowered) and not any(v + "/" in lowered for v in vendors):
            continue
        try:
            if suffix in {".json", ".toml", ".yaml", ".yml", ".sql", ".proto"}:
                clean = config_view(text, suffix)
                code = None
            else:
                clean, code, _ = source_views(text, suffix)
        except (ValueError, SyntaxError) as error:
            unreadable.append({"path": path, "reason": str(error)})
            continue
        matches = {(m.start(), m.end()): m.group() for m in TOKEN.finditer(clean)
                   if re.search(r"\d|-latest$", m.group()) or m.group() in {"deepseek-chat", "deepseek-reasoner"}}
        original_matches = set(matches)
        # Model fields cover vendor families absent from the recognizer. A
        # vendor namespace alone could also be a dependency or repository id.
        for m in IDENTIFIER.finditer(clean):
            context = clean[clean.rfind("\n", 0, m.start()) + 1:m.start()]
            if (m.group().count("/") == 1 and m.group().split("/", 1)[0] in vendors
                    and m.group()[-1].isalnum()
                    and re.search(r"\b\w*model\w*\b", context, re.I)
                    and model_selector(clean, m.start(), code)):
                matches[(m.start(), m.end())] = m.group()
        original_matches |= set(matches)
        # A selecting field supplies the context for arbitrary provider lines
        # and unversioned families. No provider allowlist determines coverage.
        for pattern in (IDENTIFIER, BARE_FAMILY):
            for m in pattern.finditer(clean):
                if (m.group()[-1].isalnum() and scalar_model(clean, m.start(), m.end(), code is None)
                        and model_selector(clean, m.start(), code)):
                    matches[(m.start(), m.end())] = m.group()
        seen = collections.Counter()
        nonselectors = metadata_ranges(clean, suffix) if matches else []
        nonselectors += diagnostic_ranges(repository, path, clean, code) if matches else []
        for (start, end), model in sorted(matches.items()):
            metadata = next((reason for begin, finish, reason in nonselectors
                             if begin <= start and end <= finish), None)
            if metadata:
                exemptions.append({"path": path, "line": clean.count("\n", 0, start) + 1, "reason": metadata})
                continue
            if suffix == ".sql" and not sql_selection(clean, start):
                continue
            if code is not None and code[start:start + len(model)].strip():
                continue  # A variable, method, import or enum key is not a literal model id.
            if not scalar_model(clean, start, end, code is None):
                continue
            if (model.endswith((".tar.bz2", ".tar.gz", ".zip"))
                    or pathlib.PurePosixPath(model).suffix.lower() in SOURCE | {
                        ".prefab", ".asset", ".unity", ".scene", ".fbx", ".gltf", ".glb", ".obj",
                        ".png", ".jpg", ".webp", ".svg"}):
                continue
            if (re.fullmatch(r"o[134]", model.rsplit("/", 1)[-1], re.I)
                    and not ("/" in model and model.split("/", 1)[0].lower() in vendors)
                    and not model_selector(clean, start, code)):
                continue  # Short reasoning-model ids also occur as ordinary record ids.
            if ALIAS.fullmatch(model) and (aliases is None or model in aliases):
                continue
            seen[model] += 1
            row = {"path": path, "line": clean.count("\n", 0, start) + 1,
                   "model": model, "occurrence": seen[model], "contract": CONTRACT}
            if (start, end) not in original_matches:
                row["rule"] = SELECTOR_RULE
            findings.append(row)
    return {"findings": findings, "unreadable": unreadable, "exemptions": exemptions, "limits": LIMITS}


def baseline_key(row):
    return f"model-pin {quote(row['path'], safe='/._-')} {row['model']} {row['occurrence']}"


def rule_id(row):
    return row.get("rule", "model-pin")


def decide(findings, head_records, base_records, base_findings, head_adopted, base_adopted,
           *, allow_seed=False, seed_rules=frozenset()):
    actual = {baseline_key(row) for row in findings}
    inherited = {baseline_key(row) for row in base_findings}
    errors = []
    if base_adopted and not head_adopted:
        errors.append("model-alias ratchet marker cannot be removed")
    if head_records and not head_adopted:
        errors.append("model-pin baseline records require " + BASELINE_MARKER)
    allowed = base_records | (inherited if allow_seed and not base_adopted else set())
    allowed |= {baseline_key(row) for row in base_findings
                if rule_id(row) == SELECTOR_RULE and SELECTOR_RULE in seed_rules}
    for key in sorted(head_records - allowed):
        errors.append("model-pin baseline cannot grow or seed new debt: " + key)
    for key in sorted(head_records - actual):
        errors.append("stale model-pin baseline record: " + key + "; remove it")
    for row in findings:
        key = baseline_key(row)
        row["baseline_recorded"] = key in head_records
        if key not in head_records & allowed:
            errors.append(f"{row['path']}:{row['line']}: vendor model id {row['model']!r} is pinned in source; "
                          "omit the model id to use the organization's runtime default, or write an exact "
                          "Sylphx AI catalog alias of the form <provider>/<line>-latest (" + CONTRACT + ")")
    return errors
