#!/usr/bin/env bash
# Installs the keel CLI at a title's source revision or public version and, unless
# disabled, the wasm-bindgen CLI (at the version the title's Cargo.lock pins
# for the wasm-bindgen crate) into one root, and puts its bin/ on PATH.
#
# Each tool has its own stamped install root, so changing one never rebuilds
# the other. bin/ exposes both roots, including Cargo's companion executables.
# A stamp is written only after its install and version checks succeed.
#
#   install-keel-tools.sh key [keel|wasm-bindgen]  # print the cache key part
#   install-keel-tools.sh install DIR    # install into DIR
#
# Environment (all optional):
#   KEEL_PIN        full source commit or exact public x.y.z; default: KEEL_PIN_FILE
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
distribution=source
if [[ "$pin" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  distribution=registry
elif [[ ! "$pin" =~ ^[0-9a-f]{40}$ ]]; then
  echo "the Keel pin must be a full commit sha or exact x.y.z: '$pin'" >&2
  exit 1
fi

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
keel_stamp="keel=$pin${features:+ features=$features}"
if [ "$mode" = key ]; then
  case "${2:-all}" in
    all) echo "keel-$pin-wasm-bindgen-$bindgen${features:+ features=$features}" ;;
    keel) echo "keel-$pin${features:+ features=$features}" ;;
    wasm-bindgen) echo "wasm-bindgen-$bindgen" ;;
    *) echo "unknown tool: $2" >&2; exit 2 ;;
  esac
  exit 0
fi

dir="${2:?usage: $0 install DIR}"
start=$SECONDS
warm=true
if [ "$bindgen" != none ]; then
  bindgen_root="$dir/wasm-bindgen"
  if [ "$(cat "$bindgen_root/stamp" 2>/dev/null || true)" != "wasm-bindgen=$bindgen" ] \
    || [ ! -x "$bindgen_root/bin/wasm-bindgen" ]; then
    warm=false
    echo "building wasm-bindgen tools: $bindgen"
    rm -rf "$bindgen_root"
    mkdir -p "$bindgen_root"
    cargo install wasm-bindgen-cli --version "$bindgen" --locked --root "$bindgen_root"
  fi
  have="$("$bindgen_root/bin/wasm-bindgen" --version | awk '{print $2}')"
  [ "$have" = "$bindgen" ] || { echo "wasm-bindgen is $have, the lockfile pins $bindgen" >&2; exit 1; }
  printf 'wasm-bindgen=%s' "$bindgen" > "$bindgen_root/stamp"
fi

keel_root="$dir/keel"
if [ "$(cat "$keel_root/stamp" 2>/dev/null || true)" != "$keel_stamp" ] || [ ! -x "$keel_root/bin/keel" ]; then
  warm=false
  echo "building keel tools: $keel_stamp"
  rm -rf "$keel_root"
  mkdir -p "$keel_root"
  # Bash 3.2 treats an empty array as unset under nounset.
  feature_args=()
  if [ -n "$features" ]; then feature_args=(--features "$features"); fi
  if [ "$distribution" = registry ]; then
    cargo install sylphx-keel-cli --version "=$pin" --locked --root "$keel_root" ${feature_args[@]+"${feature_args[@]}"}
  else
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
    cargo install --path "$src/crates/keel-cli" --locked --root "$keel_root" ${feature_args[@]+"${feature_args[@]}"}
    if [ -n "$cloned" ]; then rm -rf "$src"; fi
  fi
  [ -x "$keel_root/bin/keel" ] || { echo "no keel binary in $keel_root/bin" >&2; exit 1; }
fi
if [ "$distribution" = registry ]; then
  have="$("$keel_root/bin/keel" --version)"
  [ "$have" = "keel $pin" ] || { echo "installed $have, expected keel $pin" >&2; exit 1; }
fi
printf '%s' "$keel_stamp" > "$keel_root/stamp"

# Expose requested tools without copying their bytes or retaining links to
# another runner. The install roots and bin are on the same filesystem.
rm -rf "${dir:?}/bin"
mkdir -p "$dir/bin"
roots=("$keel_root")
[ "$bindgen" = none ] || roots+=("$bindgen_root")
for root in "${roots[@]}"; do
  for binary in "$root"/bin/*; do
    [ -f "$binary" ] || continue
    [ -x "$binary" ] || continue
    ln "$binary" "$dir/bin/${binary##*/}"
  done
done
printf '%s' "$stamp" > "$dir/stamp"
if [ "$warm" = true ]; then echo "keel tools from cache: $stamp"; fi
if [ -n "${GITHUB_PATH:-}" ]; then echo "$dir/bin" >> "$GITHUB_PATH"; fi
echo "keel tools ready in $((SECONDS - start)) s"
