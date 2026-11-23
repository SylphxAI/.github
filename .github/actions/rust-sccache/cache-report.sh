#!/usr/bin/env bash
# End-of-job report for the sccache server started by the rust-sccache action.
# Warns (never fails) when a cache expected to be writable is not being written.
#
# In: BACKEND (optional label), GITHUB_STEP_SUMMARY, GITHUB_EVENT_NAME.
set -uo pipefail

stats="$(sccache --show-stats 2>&1)" || { echo "sccache stats unavailable"; exit 0; }
printf '%s\n' "$stats"

num() { printf '%s\n' "$stats" | grep -E "^$1 +[0-9]+\$" | head -n1 | awk '{print $NF}'; }
hits="$(num 'Cache hits')"; misses="$(num 'Cache misses')"; write_errors="$(num 'Cache write errors')"
json="$(sccache --show-stats --stats-format json 2>/dev/null)" || json=""
writes="$(jq -r '.stats.cache_writes // empty' <<< "$json" 2>/dev/null)"
mode="$(jq -r '.effective_cache_mode // empty' <<< "$json" 2>/dev/null)"
hits="${hits:-0}"; misses="${misses:-0}"; write_errors="${write_errors:-0}"

# A failed startup write probe can also produce ReadOnly. Only treat refused
# writes as expected when the selected backend was deliberately set read-only.
case "${BACKEND:-}" in
  buildcache) configured="${SCCACHE_WEBDAV_RW_MODE:-}" ;;
  static) configured="${SCCACHE_S3_RW_MODE:-}" ;;
  gha) configured="${SCCACHE_GHA_RW_MODE:-}" ;;
  *) configured="" ;;
esac

mode_note=""
if [ "$mode" = ReadOnly ] && [ "$configured" = READ_ONLY ]; then
  # sccache counts locally refused puts on misses as write errors, even
  # though it never sends those puts to the backend.
  mode_note="Read-only by policy; sccache counted $write_errors refused write(s). This job does not warm the cache."
  printf '%s\n' "$mode_note"
elif [ "$mode" = ReadOnly ]; then
  echo "::warning::sccache: the server is read-only without an explicit read-only setting for this backend (hits $hits, misses $misses, write errors $write_errors). Check the cache startup write probe and backend configuration."
elif [ "$write_errors" -gt 0 ]; then
  echo "::warning::sccache: $write_errors cache write(s) failed (hits $hits, misses $misses). Check the cache backend and its credential."
elif [ "$misses" -gt 0 ] && [ "$writes" = 0 ]; then
  echo "::warning::sccache: $misses cache miss(es) but 0 writes. Check the cache backend and its credential."
fi

if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  {
    echo "### Rust compile cache (${BACKEND:-sccache}, ${GITHUB_EVENT_NAME:-local})"
    echo '```'
    printf '%s\n' "$stats" | grep -E '^(Compile requests|Cache hits|Cache misses|Cache hits rate|Cache write errors|Cache read errors|Non-cacheable calls|Cache location)' || true
    case "$mode" in ReadOnly|ReadWrite) printf 'Effective cache mode             %s\n' "$mode" ;; esac
    if [[ "$writes" =~ ^[0-9]+$ ]]; then printf 'Cache writes                     %s\n' "$writes"; fi
    echo '```'
    if [ -n "$mode_note" ]; then printf '\n%s\n' "$mode_note"; fi
  } >> "$GITHUB_STEP_SUMMARY"
fi
