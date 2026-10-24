#!/usr/bin/env bash
# End-of-job report for the sccache server started by the rust-sccache action:
# prints the stats, appends the hit rate to the job summary, and WARNS (never
# fails) when the cache is not being written, so a silent cold cache shows up.
#
# In: BACKEND (optional label), GITHUB_STEP_SUMMARY, GITHUB_EVENT_NAME.
set -uo pipefail

stats="$(sccache --show-stats 2>&1)" || { echo "sccache stats unavailable"; exit 0; }
printf '%s\n' "$stats"

num() { printf '%s\n' "$stats" | grep -E "^$1 +[0-9]+\$" | head -n1 | awk '{print $NF}'; }
hits="$(num 'Cache hits')"; misses="$(num 'Cache misses')"; write_errors="$(num 'Cache write errors')"
writes="$(sccache --show-stats --stats-format json 2>/dev/null | grep -oE '"cache_writes":[0-9]+' | head -n1 | cut -d: -f2)"
hits="${hits:-0}"; misses="${misses:-0}"; write_errors="${write_errors:-0}"

if [ "$write_errors" -gt 0 ]; then
  echo "::warning::sccache: $write_errors cache write(s) failed (hits $hits, misses $misses), so the next run compiles cold. Check the cache bucket and its credential."
elif [ "$misses" -gt 0 ] && [ "${writes:-}" = 0 ]; then
  echo "::warning::sccache: $misses cache miss(es) but 0 writes, so the next run compiles cold. Check the cache bucket and its credential."
fi

if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  {
    echo "### Rust compile cache (${BACKEND:-sccache}, ${GITHUB_EVENT_NAME:-local})"
    echo '```'
    printf '%s\n' "$stats" | grep -E '^(Compile requests|Cache hits|Cache misses|Cache hits rate|Cache write errors|Cache read errors|Non-cacheable calls|Cache location)' || true
    echo '```'
  } >> "$GITHUB_STEP_SUMMARY"
fi
