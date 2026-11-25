"""Offline catalog-alias policy and shrinking, per-repository model-pin debt."""
from __future__ import annotations

import collections
import ast
import json
import importlib.util
import pathlib
import re
from urllib.parse import quote

PLATFORM_REPOSITORIES = frozenset({"SylphxAI/sylphx", "SylphxAI/cloud"})

POLICY = pathlib.Path(__file__).resolve().parents[3] / "policy/model-aliases.json"
BASELINE_MARKER = "# model-alias-ratchet-v1"
SELECTOR_RULE = "model-selector"
SELECTOR_MARKER = "# stack-rule-adopted-v1 " + SELECTOR_RULE
CONTRACT = ("https://github.com/SylphxAI/sylphx/blob/main/docs/adr/"
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
    {"repository": "SylphxAI/sylphx", "path": "services/ai/crates/ai-core/src/",
     "reason": "AI catalog identity, revision and offering policy owns vendor model identities."},
    {"repository": "SylphxAI/sylphx", "path": "services/ai/crates/ai-server/src/capabilities/control_plane/",
     "reason": "AI catalog ingestion, alias resolution and provider-locator declarations own upstream ids."},
    {"repository": "SylphxAI/sylphx", "path": "services/ai/migrations/",
     "reason": "AI catalog migrations declare stored model and offering identities."},
    {"repository": "SylphxAI/sylphx", "path": "services/ai/crates/ai-server/src/bin/ai_dev_seed.rs",
     "reason": "The development catalog seeder declares model and offering rows, not a product request default."},
    {"repository": "SylphxAI/sylphx", "path": "services/ai/crates/ai-server/src/capabilities/capacity/adapters/grok_quota.rs",
     "reason": "The provider quota adapter owns the upstream billing model locator."},
    {"repository": "SylphxAI/sylphx", "path": "services/ai/conformance/",
     "reason": "Provider protocol conformance schemas are vendor-contract test fixtures."},
    {"repository": "SylphxAI/sylphx", "path": "contracts/compat/openresponses/",
     "reason": "Imported OpenResponses compatibility schemas are upstream contract fixtures."},
    {"repository": "SylphxAI/sylphx", "path": "services/ai/scripts/compliance/openresponses/src/generated/",
     "reason": "Generated upstream OpenResponses conformance fixtures retain the provider contract's examples."},
    {"repository": "SylphxAI/sylphx", "path": "scripts/ci/verify-fresh-database-bootstrap.sh",
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
_EXCLUDED_NAME = re.compile(r"(?:[._-](?:tests?|spec))\.")
_FAMILY_HINTS = tuple(family + separator for family in FAMILIES for separator in ("-", "."))
_FAMILY_STEM_PAIRS = tuple((family, (family + "-", family + ".")) for family in FAMILIES)
_O_HINT = re.compile(r"(?<![\w.-])o[134](?!\w)")
_BARE_CORE = (
    r"(?:" + "|".join(name for name in FAMILIES if name != "tts")
    + r"|o[134])(?:[.-][a-z0-9][a-z0-9.:-]*)?|tts[.-][a-z0-9][a-z0-9.:-]*"
)
_BARE_QUOTED = re.compile(r"[\"'`](?P<val>" + _BARE_CORE + r")", re.I)
_BARE_CONFIG = re.compile(r"(?:[\"'`]|[:=]\s*)(?P<val>" + _BARE_CORE + r")", re.I)
_UNQUOTED_CONFIG_START = re.compile(r"[:=]\s*(?!(?:true|false|null)\b)[A-Za-z_]")
_CFG_TOKENS = {
    "json_proto": re.compile(r'"""[\s\S]*?"""|\x27\x27\x27[\s\S]*?\x27\x27\x27|"(?:\\.|[^"\\])*"|\x27(?:\\.|[^\x27\\])*\x27|//[^\n]*|/\*.*?\*/', re.S),
    "sql": re.compile(r'"""[\s\S]*?"""|\x27\x27\x27[\s\S]*?\x27\x27\x27|"(?:\\.|[^"\\])*"|\x27(?:\\.|[^\x27\\])*\x27|--[^\n]*|/\*.*?\*/', re.S),
    "hash": re.compile(r'"""[\s\S]*?"""|\x27\x27\x27[\s\S]*?\x27\x27\x27|"(?:\\.|[^"\\])*"|\x27(?:\\.|[^\x27\\])*\x27|\#[^\n]*', re.S),
}
_SYNONYM_ASSIGN = re.compile(r"\b\w*synonym\w*\s*=\s*$", re.I)
_SELECTOR_CANDIDATE = re.compile(
    r"(?:^|[\s.,(\[{])[\"']?(?P<name>[A-Za-z_$][\w$]*)[\"']?\s*"
    r"(?:\s*:\s*(?:&(?:'static\s+)?str|[A-Za-z_][\w:<>?\[\] &.]*))?"
    r"\s*(?P<operator>:=|[:=]|\()\s*(?:[\w.]+\s*\(\s*)?"
    r"(?:\[\s*)?$"
)
_SELECTOR_FALLBACK = re.compile(
    r"\b(?P<name>[A-Za-z_$][\w$]*)\s*[:=]\s*"
    r"[^;{}\n]*?(?:\?\?|\|\|)\s*$"
)
_CAMEL_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")
_SELECTOR_CALL = re.compile(r"(?:(?:language|chat|text|image|embedding|ai|llm|select|set|get|with|resolve)_)?model")
_SELECTOR_NAME = re.compile(r"(?:^|_)(?:models?|model_(?:id|name|slug))(?:_(?:default|fallback))?$")
_JSON_SPACE = re.compile(r"\s*")
_CLASSIFIER_ANCESTORS = frozenset({"taxonomy", "quotatiers", "modellimits", "match"})
_VERSIONED_TOKEN = re.compile(r"\d|-latest$")
_CONTEXT_MODEL = re.compile(r"\b\w*model\w*\b", re.I)
_O_SERIES_MODEL = re.compile(r"o[134]", re.I)
_SKIP_EXTENSIONS = SOURCE | {
    ".prefab", ".asset", ".unity", ".scene", ".fbx", ".gltf", ".glb", ".obj",
    ".png", ".jpg", ".webp", ".svg",
}


def _blank_preserving_newlines(text):
    if "\n" not in text:
        return " " * len(text)
    return "\n".join(" " * len(part) for part in text.split("\n"))


def load_aliases(path=None):
    if path is None:
        return None  # Default: the product-facing <provider>/<line>-latest contract.
    data = json.loads(pathlib.Path(path).read_text())
    if data.get("version") != 1 or not isinstance(data.get("aliases"), list):
        raise ValueError("invalid pinned model-alias declaration")
    source = data.get("source", {})
    if (source.get("repository") not in PLATFORM_REPOSITORIES
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
            or bool(_EXCLUDED_NAME.search(p.name)))


def exemption(repository, path):
    return next((row for row in EXEMPTIONS
                 if (row["repository"] == repository or
                     row["repository"] in PLATFORM_REPOSITORIES and repository in PLATFORM_REPOSITORIES) and
                 (path.startswith(row["path"]) if row["path"].endswith("/") else path == row["path"])), None)


def config_view(text, suffix):
    """Mask comments, retaining strings and offsets in data/config formats."""
    if suffix in {".json", ".proto"}:
        if "//" not in text and "/*" not in text:
            return text
        token = _CFG_TOKENS["json_proto"]
    elif suffix == ".sql":
        if "--" not in text and "/*" not in text:
            return text
        token = _CFG_TOKENS["sql"]
    else:
        if "#" not in text:
            return text
        token = _CFG_TOKENS["hash"]
    parts = []
    last = 0
    for m in token.finditer(text):
        group = m.group()
        if group.startswith(("#", "--", "//", "/*")):
            parts.append(text[last:m.start()])
            parts.append(_blank_preserving_newlines(group))
            last = m.end()
    if not parts:
        return text
    parts.append(text[last:])
    return "".join(parts)


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
        nl = view.rfind("\n", 0, opening)
        lb = view.rfind("{", max(0, nl + 1), opening)
        rb = view.rfind("}", max(0, nl + 1, lb + 1), opening)
        sc = view.rfind(";", max(0, nl + 1, lb + 1, rb + 1), opening)
        declaration_start = max(nl, lb, rb, sc) + 1
        declaration = view[declaration_start:opening]
        if "synonym" in declaration.lower() and _SYNONYM_ASSIGN.search(declaration):
            return False  # Search vocabulary labels do not select a model.
    boundary = max(clean.rfind(",", 0, start), clean.rfind("{", 0, start), clean.rfind("}", 0, start))
    semi = clean.rfind(";", max(0, boundary + 1), start)
    if semi > boundary:
        boundary = semi
    prefix = clean[boundary + 1:start].rstrip("\"'` \t\r\n")
    # An array item, fallback, optional wrapper or typed assignment keeps the
    # same selecting field. Stop at a statement/object boundary; quoted values
    # cannot supply selector names for their neighbours.
    candidate = _SELECTOR_CANDIDATE.search(prefix)
    if candidate is None:
        # Null-coalescing / environment fallbacks are still hard-coded
        # defaults for the directly assigned model selector.
        candidate = _SELECTOR_FALLBACK.search(prefix)
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
    name = _CAMEL_BOUNDARY.sub(r"\1_\2", candidate["name"]).lower()
    if name.endswith("models") and not prefix.endswith("["):
        return False  # A family-name policy string is not a selected model.
    if (candidate.groupdict().get("operator") == "(" and not _SELECTOR_CALL.fullmatch(name)):
        return False  # Classifier/parser vocabulary is not a selection call.
    return bool(_SELECTOR_NAME.search(name))


def sql_selection(clean, start):
    """A comparison identifies existing rows; SET/VALUES/DEFAULT select values."""
    view = re.sub(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"",
                  lambda match: "".join("\n" if ch == "\n" else " " for ch in match.group()), clean)
    statement = view[view.rfind(";", 0, start) + 1:start]
    clauses = re.findall(r"\b(where|on|having|when|set|values|default|then|else)\b", statement, re.I)
    return not clauses or clauses[-1].lower() not in {"where", "on", "having", "when"}


def _json_has_classifier_model(clean):
    """Fast C-parser precheck before walking offsets with JSONDecoder."""
    try:
        # Use tuple as a C-speed marker for JSON objects while keeping duplicate keys.
        root = json.loads(clean, object_pairs_hook=tuple)
    except (ValueError, RecursionError):
        return True
    stack = [(root, False, False)]
    while stack:
        obj, in_classifier, last_key_is_model = stack.pop()
        if isinstance(obj, tuple):
            for key, value in obj:
                norm = key.replace("_", "").replace("-", "").lower()
                child_is_model = norm in {"model", "models"}
                if in_classifier and child_is_model and isinstance(value, str):
                    return True
                if isinstance(value, (tuple, list)):
                    stack.append((value, in_classifier or (norm in _CLASSIFIER_ANCESTORS), child_is_model))
        elif isinstance(obj, list):
            for item in obj:
                if isinstance(item, str):
                    if in_classifier and last_key_is_model:
                        return True
                elif isinstance(item, (tuple, list)):
                    stack.append((item, in_classifier, last_key_is_model))
    return False


def metadata_ranges(clean, suffix, original=None):
    """Locate classifier membership and weight provenance, leaving defaults visible."""
    ranges = []
    if suffix == ".json":
        if not _json_has_classifier_model(clean):
            return ranges
        decoder = json.JSONDecoder()
        space = _JSON_SPACE

        def visit(at, in_classifier, last_key_is_model):
            at = space.match(clean, at).end()
            if clean[at] == "{":
                at = space.match(clean, at + 1).end()
                while clean[at] != "}":
                    key, at = decoder.raw_decode(clean, at)
                    at = space.match(clean, at).end()
                    if not isinstance(key, str) or clean[at] != ":":
                        raise ValueError("invalid JSON object")
                    norm = key.replace("_", "").replace("-", "").lower()
                    child_classifier = in_classifier or (norm in _CLASSIFIER_ANCESTORS)
                    at = space.match(clean, visit(at + 1, child_classifier, norm in {"model", "models"})).end()
                    if clean[at] != ",":
                        break
                    at = space.match(clean, at + 1).end()
                if clean[at] != "}":
                    raise ValueError("invalid JSON object")
                return at + 1
            if clean[at] == "[":
                at = space.match(clean, at + 1).end()
                while clean[at] != "]":
                    at = space.match(clean, visit(at, in_classifier, last_key_is_model)).end()
                    if clean[at] != ",":
                        break
                    at = space.match(clean, at + 1).end()
                if clean[at] != "]":
                    raise ValueError("invalid JSON array")
                return at + 1
            value, end = decoder.raw_decode(clean, at)
            if isinstance(value, str) and last_key_is_model and in_classifier:
                ranges.append((at, end, "Classifier membership identifies existing models; it selects no request/default."))
            return end

        try:
            if space.match(clean, visit(0, False, False)).end() != len(clean):
                return []
        except (ValueError, IndexError, RecursionError):
            return []  # No metadata exemption without a complete parsed declaration.
    elif suffix == ".py":
        # source_views masks docstrings for recognition. A docstring-only
        # class is valid source, but its masked view has an empty suite.
        source = original if original is not None else clean
        lines = source.splitlines(keepends=True)
        offsets, total = [], 0
        for line in lines:
            offsets.append(total)
            total += len(line)
        for node in ast.walk(ast.parse(source)):
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
    size = len(code)
    if start >= size:
        return None
    depth = 1
    pos = start
    find = code.find
    next_open = find(opening, pos)
    next_close = find(closing, pos)
    while depth:
        if next_close < 0:
            return None
        if 0 <= next_open < next_close:
            depth += 1
            pos = next_open + 1
            next_open = find(opening, pos)
        else:
            depth -= 1
            pos = next_close + 1
            next_close = find(closing, pos)
    return pos


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


def _token_windows(lowered):
    """Find character ranges around family/o-series needles so TOKEN never scans whole large files."""
    wins = []
    size = len(lowered)
    find = lowered.find
    for stem, seps in _FAMILY_STEM_PAIRS:
        if stem not in lowered:
            continue
        for needle in seps:
            idx = find(needle)
            while idx != -1:
                s = idx
                while s > 0 and (lowered[s - 1].isalnum() or lowered[s - 1] in "_./:-"):
                    s -= 1
                e = idx + len(needle)
                while e < size and (lowered[e].isalnum() or lowered[e] in "_./:-"):
                    e += 1
                wins.append((max(0, s - 1), min(size, e + 1)))
                idx = find(needle, idx + 1)
    for needle in ("o1", "o3", "o4"):
        idx = find(needle)
        while idx != -1:
            if idx == 0 or not (lowered[idx - 1].isalnum() or lowered[idx - 1] in "_.-"):
                s = idx
                while s > 0 and (lowered[s - 1].isalnum() or lowered[s - 1] in "_./:-"):
                    s -= 1
                e = idx + 2
                while e < size and (lowered[e].isalnum() or lowered[e] in "_./:-"):
                    e += 1
                wins.append((max(0, s - 1), min(size, e + 1)))
            idx = find(needle, idx + 1)
    if not wins:
        return ()
    wins.sort()
    merged = [wins[0]]
    for s, e in wins[1:]:
        if s <= merged[-1][1]:
            if e > merged[-1][1]:
                merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    return merged


def _collect_selector_matches(clean, code, suffix, vendors, matches, original_matches):
    """Scan full clean source for arbitrary provider/unversioned selector values.

    Must run across the entire buffer even when _token_windows() is empty, so
    explicit selectors naming custom providers or unversioned lines still hit.
    """
    is_config = code is None
    for m in IDENTIFIER.finditer(clean):
        val = m.group()
        if not val[-1].isalnum():
            continue
        start, end = m.start(), m.end()
        if val.count("/") == 1 and val.split("/", 1)[0] in vendors:
            context = clean[clean.rfind("\n", 0, start) + 1:start]
            if _CONTEXT_MODEL.search(context) and model_selector(clean, start, code):
                matches[(start, end)] = val
                original_matches.add((start, end))
                continue
        if scalar_model(clean, start, end, is_config) and model_selector(clean, start, code):
            matches[(start, end)] = val
    bare_re = _BARE_QUOTED if (not is_config or (suffix == ".json" and not _UNQUOTED_CONFIG_START.search(clean))) else _BARE_CONFIG
    for m in bare_re.finditer(clean):
        val = m.group("val")
        s, e = m.start("val"), m.end("val")
        if val[-1].isalnum() and scalar_model(clean, s, e, is_config) and model_selector(clean, s, code):
            matches[(s, e)] = val


def scan(tree, repository, aliases, source_views, *, cache=None, shared_views=None):
    findings, unreadable, exemptions = [], [], []
    vendors = VENDOR_NAMESPACES | {alias.split("/", 1)[0] for alias in aliases or ()}
    vendor_slashes = tuple(v + "/" for v in vendors)
    blob_oid_fn = getattr(tree, "blob_oid", None) if (cache is not None or shared_views is not None) else None
    aliases_key = frozenset(aliases) if aliases is not None else None
    for path in sorted(tree.paths()):
        suffix = pathlib.PurePosixPath(path).suffix
        if excluded(path) or suffix not in SOURCE:
            continue
        allowed = exemption(repository, path)
        if allowed:
            exemptions.append({"path": path, "reason": allowed["reason"]})
            continue
        oid = blob_oid_fn(path) if blob_oid_fn is not None else None
        cache_key = (path, oid, repository, aliases_key) if (cache is not None and oid is not None) else None
        if cache_key is not None and cache_key in cache:
            kind, f_rows, e_rows, u_rows = cache[cache_key]
            for r in f_rows:
                findings.append(dict(r))
            for r in e_rows:
                exemptions.append(dict(r))
            for r in u_rows:
                unreadable.append(dict(r))
            continue
        text = tree.read_model_source(path)
        if text is None:
            unreadable.append({"path": path, "reason": "git blob unreadable"})
            continue
        if source_scope.compiler_generated(text):
            row = {"path": path, "reason": "protocol compiler output; scan its authored schema instead"}
            exemptions.append(row)
            if cache_key is not None:
                cache[cache_key] = ("ok", (), (dict(row),), ())
            continue
        # Most blobs contain no model-shaped token; they need no parser. This
        # also avoids treating unrelated legacy language syntax as model debt.
        # Literal substring searches run in C and avoid the full boundary regex
        # across large generated protocols and data assets with no candidate.
        lowered = text.lower()
        if ("model" not in lowered
                and not any(f in lowered for f in FAMILIES)
                and not any(vs in lowered for vs in vendor_slashes)
                and not _O_HINT.search(lowered)):
            if cache_key is not None:
                cache[cache_key] = ("empty", (), (), ())
            continue
        try:
            if suffix in {".json", ".toml", ".yaml", ".yml", ".sql", ".proto"}:
                clean = config_view(text, suffix)
                code = None
            else:
                if shared_views is not None and oid is not None:
                    sv_key = (oid, suffix, False, False)
                    views_res = shared_views.get(sv_key)
                    if views_res is None:
                        views_res = source_views(text, suffix)
                        shared_views[sv_key] = views_res
                    clean, code, _ = views_res
                else:
                    clean, code, _ = source_views(text, suffix)
        except (ValueError, SyntaxError) as error:
            row = {"path": path, "reason": str(error)}
            unreadable.append(row)
            if cache_key is not None:
                cache[cache_key] = ("err", (), (), (dict(row),))
            continue
        clean_lower = clean.lower() if code is not None or clean is not text else lowered
        # Only use lowered offsets for _token_windows when case folding preserves string length.
        windows = _token_windows(clean_lower) if len(clean_lower) == len(clean) else ((0, len(clean)),)
        matches = {}
        for ws, we in windows:
            for m in TOKEN.finditer(clean, ws, we):
                val = m.group()
                if _VERSIONED_TOKEN.search(val) or val in {"deepseek-chat", "deepseek-reasoner"}:
                    matches[(m.start(), m.end())] = val
        original_matches = set(matches)
        # Model fields cover arbitrary providers and unversioned lines across the
        # entire file independently of _token_windows().
        if "model" in clean_lower:
            _collect_selector_matches(clean, code, suffix, vendors, matches, original_matches)
        seen = collections.Counter()
        nonselectors = metadata_ranges(clean, suffix, text) if matches else []
        nonselectors += diagnostic_ranges(repository, path, clean, code) if matches else []
        file_findings, file_exemptions = [], []
        for (start, end), model in sorted(matches.items()):
            metadata = next((reason for begin, finish, reason in nonselectors
                             if begin <= start and end <= finish), None)
            if metadata:
                file_exemptions.append({"path": path, "line": clean.count("\n", 0, start) + 1, "reason": metadata})
                continue
            if suffix == ".sql" and not sql_selection(clean, start):
                continue
            if code is not None and code[start:start + len(model)].strip():
                continue  # A variable, method, import or enum key is not a literal model id.
            if not scalar_model(clean, start, end, code is None):
                continue
            if (model.endswith((".tar.bz2", ".tar.gz", ".zip"))
                    or pathlib.PurePosixPath(model).suffix.lower() in _SKIP_EXTENSIONS):
                continue
            if (_O_SERIES_MODEL.fullmatch(model.rsplit("/", 1)[-1])
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
            file_findings.append(row)
        findings.extend(file_findings)
        exemptions.extend(file_exemptions)
        if cache_key is not None:
            cache[cache_key] = ("ok", [dict(r) for r in file_findings], [dict(r) for r in file_exemptions], ())
    return {"findings": findings, "unreadable": unreadable, "exemptions": exemptions, "limits": LIMITS}


def baseline_key(row):
    return f"model-pin {quote(row['path'], safe='/._-')} {row['model']} {row['occurrence']}"


def rule_id(row):
    return row.get("rule", "model-pin")


def relocated_records(base_tree, head_tree, base_findings, findings, base_records, head_records):
    """Rekey recorded findings only across unambiguous, exact Git file moves."""
    removed = base_records - head_records
    added = head_records - base_records
    if not (removed and added):
        return set()

    before, after = base_tree.entries(), head_tree.entries()

    def absent_blobs(source, target):
        paths = collections.defaultdict(list)
        for path, entry in source.items():
            if path not in target and entry[0] in {"100644", "100755"}:
                paths[entry].append(path)
        return paths

    old_paths, new_paths = absent_blobs(before, after), absent_blobs(after, before)
    moves = {old[0]: new_paths[entry][0] for entry, old in old_paths.items()
             if len(old) == len(new_paths.get(entry, ())) == 1}
    # The base record must still describe a real finding. Carry its exact rule,
    # model and occurrence to the one destination, never the rest of the blob's debt.
    destinations = {(baseline_key(row), rule_id(row)) for row in findings}
    rekeyed = set()
    for row in base_findings:
        if row["path"] in moves and baseline_key(row) in removed:
            key = baseline_key({**row, "path": moves[row["path"]]})
            if key in added and (key, rule_id(row)) in destinations:
                rekeyed.add(key)
    return rekeyed


def decide(findings, head_records, base_records, base_findings, head_adopted, base_adopted,
           *, allow_seed=False, seed_rules=frozenset(), relocated=frozenset(), notice=lambda _: None):
    actual = {baseline_key(row) for row in findings}
    inherited = {baseline_key(row) for row in base_findings}
    errors = []
    if base_adopted and not head_adopted:
        errors.append("model-alias ratchet marker cannot be removed")
    if head_records and not head_adopted:
        errors.append("model-pin baseline records require " + BASELINE_MARKER)
    allowed = base_records | relocated | (inherited if allow_seed and not base_adopted else set())
    allowed |= {baseline_key(row) for row in base_findings
                if rule_id(row) == SELECTOR_RULE and SELECTOR_RULE in seed_rules}
    for key in sorted(head_records - allowed):
        errors.append("model-pin baseline cannot grow or seed new debt: " + key)
    for key in sorted(head_records - actual):
        notice("stale model-pin baseline record: " + key + "; remove it")
    for row in findings:
        key = baseline_key(row)
        row["baseline_recorded"] = key in head_records
        if key not in head_records & allowed & (inherited | relocated):
            errors.append(f"{row['path']}:{row['line']}: vendor model id {row['model']!r} is pinned in source; "
                          "omit the model id to use the organization's runtime default, or write an exact "
                          "Sylphx AI catalog alias of the form <provider>/<line>-latest (" + CONTRACT + ")")
    return errors
