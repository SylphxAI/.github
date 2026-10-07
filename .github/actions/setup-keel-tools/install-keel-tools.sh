#!/usr/bin/env bash
# Installs the keel CLI (built from the Keel rev a title pins) and, unless
# disabled, the wasm-bindgen CLI (at the version the title's Cargo.lock pins
# for the wasm-bindgen crate) into one root, and puts its bin/ on PATH.
#
# The root carries a stamp of both versions. When the stamp matches (the
# action restores the root from a cache keyed on the key this script prints),
# nothing is built and the step takes seconds; otherwise both are built from
# source and the stamp is written last, so a half-built root never matches.
#
#   install-keel-tools.sh key            # print the cache key part
#   install-keel-tools.sh install DIR    # install into DIR
#
# Environment (all optional):
#   KEEL_PIN        the Keel commit; default: the contents of KEEL_PIN_FILE
#   KEEL_PIN_FILE   default: KEEL_PIN
#   WASM_BINDGEN    "auto" (from CARGO_LOCK, the default), "none", or x.y.z
#   CARGO_LOCK      default: Cargo.lock
#   KEEL_SRC        an existing Keel checkout at the pin (skips the clone)
#   KEEL_REPO       default: https://github.com/SylphxAI/keel
#   KEEL_FEATURES   extra cargo features for the keel CLI, space or comma
#                   separated; empty builds it with its defaults
set -euo pipefail
mode="${1:-}"
[ "$mode" = key ] || [ "$mode" = install ] || { echo "usage: $0 key | install DIR" >&2; exit 2; }

pin="${KEEL_PIN:-}"
if [ -z "$pin" ]; then
  pin_file="${KEEL_PIN_FILE:-KEEL_PIN}"
  [ -f "$pin_file" ] || { echo "no Keel pin: set the pin input or provide $pin_file" >&2; exit 1; }
  pin="$(tr -d '[:space:]' < "$pin_file")"
fi
[[ "$pin" =~ ^[0-9a-f]{40}$ ]] || { echo "the Keel pin is not a full commit sha: '$pin'" >&2; exit 1; }

bindgen="${WASM_BINDGEN:-auto}"
if [ "$bindgen" = auto ]; then
  lock="${CARGO_LOCK:-Cargo.lock}"
  [ -f "$lock" ] || { echo "no $lock to read the wasm-bindgen version from (set wasm-bindgen: none to skip it)" >&2; exit 1; }
  bindgen="$(awk '/^name = "wasm-bindgen"$/ { getline; gsub(/version = |"/, ""); print; exit }' "$lock")"
  [ -n "$bindgen" ] || { echo "no wasm-bindgen crate in $lock (set wasm-bindgen: none to skip it)" >&2; exit 1; }
fi
[ "$bindgen" = none ] || [[ "$bindgen" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] \
  || { echo "the wasm-bindgen version is not x.y.z: '$bindgen'" >&2; exit 1; }

features="${KEEL_FEATURES:-}"
features="${features//,/ }"
features="$(printf '%s' "$features" | tr -s '[:space:]' ' ' | sed -e 's/^ //' -e 's/ $//')"

stamp="keel=$pin wasm-bindgen=$bindgen"
[ -z "$features" ] || stamp="$stamp features=$features"
if [ "$mode" = key ]; then
  # The cache key carries the features too, or two shapes of one pin restore
  # each other's root, find a different stamp, rebuild and then never save
  # (cache-hit is true), so each shape rebuilds on every run. Commas would
  # split the key's parts, and line 46 already replaced them with spaces.
  echo "keel-$pin-wasm-bindgen-$bindgen${features:+ features=$features}"
  exit 0
fi

dir="${2:?usage: $0 install DIR}"
start=$SECONDS
if [ "$(cat "$dir/stamp" 2>/dev/null || true)" = "$stamp" ] && [ -x "$dir/bin/keel" ] \
  && { [ "$bindgen" = none ] || [ -x "$dir/bin/wasm-bindgen" ]; }; then
  echo "keel tools from cache: $stamp"
else
  echo "building keel tools: $stamp"
  rm -rf "$dir"
  mkdir -p "$dir"
  if [ "$bindgen" != none ]; then
    cargo install wasm-bindgen-cli --version "$bindgen" --locked --root "$dir"
  fi
  src="${KEEL_SRC:-}"
  cloned=""
  if [ -z "$src" ]; then
    src="${RUNNER_TEMP:-/tmp}/keel-src-$pin"
    rm -rf "$src"
    git clone -q --filter=blob:none --no-checkout "${KEEL_REPO:-https://github.com/SylphxAI/keel}" "$src"
    git -C "$src" checkout -q --detach "$pin"
    cloned=1
  fi
  [ "$(git -C "$src" rev-parse HEAD)" = "$pin" ] || { echo "the Keel checkout $src is not at the pin $pin" >&2; exit 1; }
  feature_args=()
  if [ -n "$features" ]; then feature_args=(--features "$features"); fi
  cargo install --path "$src/crates/keel-cli" --locked --root "$dir" "${feature_args[@]}"
  if [ -n "$cloned" ]; then rm -rf "$src"; fi
  printf '%s' "$stamp" > "$dir/stamp"
fi

if [ "$bindgen" != none ]; then
  have="$("$dir/bin/wasm-bindgen" --version | awk '{print $2}')"
  [ "$have" = "$bindgen" ] || { echo "wasm-bindgen is $have, the lockfile pins $bindgen" >&2; exit 1; }
fi
[ -x "$dir/bin/keel" ] || { echo "no keel binary in $dir/bin" >&2; exit 1; }
if [ -n "${GITHUB_PATH:-}" ]; then echo "$dir/bin" >> "$GITHUB_PATH"; fi
echo "keel tools ready in $((SECONDS - start)) s"
