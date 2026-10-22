# Brand build

The shared generator owns the build/check implementation. Each caller keeps its
SVG masters, pixel grids, `brand.json`, `tokens.json`, generated files and
`provenance.json`. Python 3 is required. Check mode needs only the standard
library and never writes; write mode needs Pillow, numpy and resvg-py installed
by the caller. The action does not fetch dependencies or update its own pin.

```yaml
- uses: SylphxAI/.github/.github/actions/brand@<full-commit-sha>
  with:
    mode: check
    brand-directory: brand
```

`root-directory` defaults to the parent of `brand-directory`. Both accept
workspace-relative or absolute paths, including spaces. `render-fit: canvas`
(the default) centres non-square renders on the requested canvas.
`render-fit: intrinsic` preserves the renderer's original output dimensions.
Use the latter when adopting a generator that returned the raw renderer output.
`resnap: true` redraws the favicon grids and is valid only with `mode: write`.
Keep the selected render mode the same for local regeneration and CI.

## Local regeneration

From the consumer repository root, use the **same full SHA as its CI pin**:

```sh
PIN=<full-commit-sha-from-your-ci-workflow>
SCRIPT="$(mktemp)"
curl --fail --location --output "$SCRIPT" \
  "https://raw.githubusercontent.com/SylphxAI/.github/$PIN/.github/actions/brand/build.py"
python3 -m pip install pillow numpy resvg-py
python3 "$SCRIPT" --brand-dir "$PWD/brand" --root "$PWD"
python3 "$SCRIPT" --brand-dir "$PWD/brand" --root "$PWD" --check
rm "$SCRIPT"
```

Add `--render-fit intrinsic` for a caller using that input, and `--resnap` only
when intentionally replacing the hand-edited grids. A local checkout of this
repository at the pin can also supply the script, without downloading it.

## Adoption

Replace the local script invocation with this action and delete the duplicate
script. Remove **only** its `build.py` entry from `provenance.json`; keep all
other source metadata and asset hashes. If the brand usage sheet changes,
refresh its own recorded SHA-256. Do not regenerate images just to move the
implementation. Existing generated-file comments can retain the historical
`brand/build.py` spelling to preserve their hashes; this shared generator keeps
that output format for compatibility.

The tests cover check-only immutability, corrupt/missing/unlisted files,
surface copies, write mode, resnapping and both render behaviours. The owner CI
also invokes this composite action as a consumer, in both modes.
