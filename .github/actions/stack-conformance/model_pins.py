"""Offline catalog-alias policy and shrinking, per-repository model-pin debt."""
from __future__ import annotations

import collections
import json
import pathlib
import re
from urllib.parse import quote

POLICY = pathlib.Path(__file__).resolve().parents[3] / "policy/model-aliases.json"
BASELINE_MARKER = "# model-alias-ratchet-v1"
CONTRACT = ("https://github.com/SylphxAI/cloud/blob/main/docs/adr/"
            "ADR-01M58DAMBRRTBRYRD3HSQQVD1S-model-identity-naming-and-price-binding.md")
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

# Recognition is deliberately separate from authorization. Versioned and
# undated upstream names are candidates; the alias contract authorizes names.
# An optional pinned catalog can tighten that contract to exact declarations.
FAMILY = (r"(?:(?:deepseek|claude|gpt|chatgpt|gemini|grok|mistral|mixtral|codestral|"
          r"ministral|magistral|devstral|pixtral|llama|qwen|qwq|kimi|glm|doubao|"
          r"command-r|jamba|nova|phi|gemma|yi|sonar|text-embedding|whisper|"
          r"dall-e|tts)[-.][a-z0-9][a-z0-9.:-]*|o[134](?:[-.][a-z0-9][a-z0-9.:-]*)?)")
TOKEN = re.compile(r"(?<![\w./-])(?:[a-z0-9][a-z0-9_.-]*/)*" + FAMILY + r"(?![\w/-])", re.I)
IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9_.:-]*(?:/[a-z0-9][a-z0-9_.:-]*)+", re.I)
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
    return (bool(set(p.parts) & SKIP) or p.name.startswith(("test_", "tests_")) or p.stem in {"test", "tests", "spec"}
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
    if before in {"\"", "'", "`"}:
        return after == before or clean[end:end + 2] == "\\" + before
    if not config:
        return False
    line_start = clean.rfind("\n", 0, start) + 1
    line_end = clean.find("\n", end)
    prefix = clean[line_start:start].rstrip()
    suffix = clean[end:line_end if line_end >= 0 else len(clean)].strip()
    return prefix.endswith((":", "=")) and not suffix


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
        # Most blobs contain no model-shaped token; they need no parser. This
        # also avoids treating unrelated legacy language syntax as model debt.
        if not TOKEN.search(text) and not any(v + "/" in text for v in vendors):
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
        # Model fields cover vendor families absent from the recognizer. A
        # vendor namespace alone could also be a dependency or repository id.
        for m in IDENTIFIER.finditer(clean):
            context = clean[clean.rfind("\n", 0, m.start()) + 1:m.start()]
            if (m.group().count("/") == 1 and m.group().split("/", 1)[0] in vendors
                    and re.search(r"\b\w*model\w*\b", context, re.I)):
                matches[(m.start(), m.end())] = m.group()
        seen = collections.Counter()
        for (start, end), model in sorted(matches.items()):
            if code is not None and code[start:start + len(model)].strip():
                continue  # A variable, method, import or enum key is not a literal model id.
            if not scalar_model(clean, start, end, code is None):
                continue
            if model.endswith((".tar.bz2", ".tar.gz", ".zip", ".json", ".rs", ".py")):
                continue
            if re.fullmatch(r"o[134]", model) and not re.search(
                    r"\b\w*model\w*\b", clean[clean.rfind("\n", 0, start) + 1:start], re.I):
                continue  # Short reasoning-model ids also occur as ordinary record ids.
            if ALIAS.fullmatch(model) and (aliases is None or model in aliases):
                continue
            seen[model] += 1
            findings.append({"path": path, "line": clean.count("\n", 0, start) + 1,
                             "model": model, "occurrence": seen[model], "contract": CONTRACT})
    return {"findings": findings, "unreadable": unreadable, "exemptions": exemptions, "limits": LIMITS}


def baseline_key(row):
    return f"model-pin {quote(row['path'], safe='/._-')} {row['model']} {row['occurrence']}"


def decide(findings, head_records, base_records, base_findings, head_adopted, base_adopted):
    actual = {baseline_key(row) for row in findings}
    inherited = {baseline_key(row) for row in base_findings}
    errors = []
    if base_adopted and not head_adopted:
        errors.append("model-alias ratchet marker cannot be removed")
    if head_records and not head_adopted:
        errors.append("model-pin baseline records require " + BASELINE_MARKER)
    allowed = base_records if base_adopted else inherited
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
