# Metadata sync

A pure, offline patch engine on the same pinned action/raw-script distribution
as the shared brand tool, not part of the image generator. Python 3.9+ and its
standard library are sufficient. Products supply **already-rendered values**;
this tool does not read product schemas, render positioning/benchmarks, run
subprocesses, or call GitHub/network APIs.

## Plan

A UTF-8 JSON plan, version 1, declares ordered file entries and ordered edits.
Paths are relative to `--root` (the caller workspace by default). Every target
must already exist; traversal, escaping symlinks, and aliases of the same file
are rejected. Repeated entries for the **same path** compose in plan order.
Unknown properties are errors, rather than silently ignored typos.

```json
{
  "version": 1,
  "files": [
    {
      "path": "README.md",
      "edits": [
        {"type": "region", "name": "lead", "value": "\nAlready-rendered lead.\n"},
        {"type": "region", "start": "<!-- hero -->", "end": "<!-- /hero -->", "value": "\nAlready-rendered hero.\n"}
      ]
    },
    {
      "path": "package.json",
      "edits": [{"type": "json", "path": ["description"], "value": "Already-rendered description"}]
    },
    {
      "path": "docs/index.md",
      "edits": [{"type": "regex", "pattern": "^  tagline: .+$", "value": "  tagline: \"Already-rendered tagline\""}]
    }
  ]
}
```

Supported edits:

- `region`: `name` expands to `<!-- sync:name -->` and `<!-- /sync:name -->`;
  alternatively supply literal `start`/`end` markers for existing caller fences.
  Each marker must occur exactly once, in order. Missing, duplicate and reversed
  markers are errors. Markers are retained; `value` is the exact body, including
  its desired surrounding newlines. It must not contain its own markers.
- `json`: `path` is a nonempty array of object keys or nonnegative array indexes.
  Every component must exist; absent fields are zero-match errors. Serialization
  is deterministic UTF-8, insertion-order keys, two-space indent, final LF.
  This intentionally normalizes the entire JSON file, matching the current
  anymd/repomap copy adapters; product parity tests should verify the bytes.
- `regex`: a Python regular expression with multiline anchors, matching exactly
  one nonempty span. Zero or multiple matches are errors. `value` replaces the
  whole match **literally** (no backreference expansion). This supports existing
  frontmatter/TOML/YAML line selectors without introducing structured parsers.
  The caller owns correct quoting/escaping and destination syntax.
- `file`: replace an entire existing file with `value`, for already-rendered
  SVG/CSS surfaces. A missing file is still an error.

All edits and output encodings in the **whole plan** validate before the first
write. A validation error writes nothing, even if earlier files would change.
Check and write use the same composition. Unchanged files are not written.
Non-JSON text outside edited spans retains its bytes (including CRLF).
This is validation atomicity, not a filesystem transaction: disk/permission
failures during writes can leave a partial update. Use a clean checkout and do
not concurrently mutate targets during a run.

## CLI and library

```sh
python3 sync.py --plan /path/to/rendered-plan.json --root /path/to/repo --check
python3 sync.py --plan /path/to/rendered-plan.json --root /path/to/repo --write
```

Exactly one mode is required. Exit 0 means in sync or successfully written;
exit 1 is check-mode drift, with ordered path summaries and unified diffs;
exit 2 is an invalid plan, selector, mode, encoding, or I/O error. Check never
writes. Identical inputs produce identical outputs and diagnostics.

For in-process use, load `sync.py` through `importlib` or `runpy` and call
`render_plan(plan, sources)` with a mapping of paths to strings. It returns a
mapping of composed outputs without I/O or mutation. `synchronize(plan, root,
mode)` supplies the same engine's local filesystem/check/write boundary.

## Adoption in anymd and repomap

Both products keep their current rendering, limits, CLI defaults and tests.
anymd's adapter retains `product.json`, format/former-name/sibling rendering;
repomap's adapter retains `brand.json`, hero scoring/rendering and tool-registry
invocations. Each adapter produces a temporary JSON plan with rendered values,
then delegates only file patching/check/write. Compose README/docs edits into
that plan, rather than invoking the engine once per edit. Remote descriptions
and topics remain explicit, separate product-owned operations.

Pin both local tooling and CI to the **same full commit SHA** containing this
action. The implementation pin below is supplied for consumer adoption after
this upstream outcome passes review and lands (the later documentation commit
does not change the engine):

```yaml
# After the product adapter renders its plan into RUNNER_TEMP:
- uses: SylphxAI/.github/.github/actions/metadata-sync@64a33b1973e33196c38605fb1fcc301ac3bf37a2
  with:
    plan: ${{ runner.temp }}/metadata-plan.json
    mode: check
```

The same raw-script recipe works in **anymd and repomap**, from their repository
root, after their adapter has prepared `$PLAN`. Downloading is distribution,
not an engine operation; a checkout at the pin can supply `sync.py` offline.

```sh
(
  set -eu
  PIN=64a33b1973e33196c38605fb1fcc301ac3bf37a2 # Same SHA as the CI action pin.
  PLAN=/path/to/product-rendered-plan.json
  SCRIPT="$(mktemp)"
  trap 'rm -f "$SCRIPT"' EXIT
  curl --fail --location --output "$SCRIPT" \
    "https://raw.githubusercontent.com/SylphxAI/.github/$PIN/.github/actions/metadata-sync/sync.py"
  python3 "$SCRIPT" --plan "$PLAN" --root "$PWD" --write
  python3 "$SCRIPT" --plan "$PLAN" --root "$PWD" --check
)
```

For check-only adoption use `--check` alone. Consumer owners must test byte
parity against their current generated surfaces before deleting local generic
patch loops. No product adoptions or remote metadata changes happen here.
