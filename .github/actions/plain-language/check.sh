#!/usr/bin/env bash
# Report listed terms. Usage: check.sh <git-range> <terms.tsv> [pathspec...]
# Default: only lines the range adds. With PLAIN_LANGUAGE_BASELINE=<file>: ratchet over every
# tracked file against that baseline (new hits fail; hits gone from the tree must leave the file).
# PLAIN_LANGUAGE_WRITE_BASELINE=init|shrink prints a baseline instead (init = everything, shrink = never grows).
# Allow marker: "plain-language: allow(<reason>; until=YYYY-MM-DD)"; a bare "plain-language: allow"
# works only until PLAIN_LANGUAGE_BARE_UNTIL (2026-10-15) and warns.
set -euo pipefail
export LC_ALL=C
range="$1"; terms="$2"; shift 2
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
excl=( ':(exclude)*.lock' ':(exclude)*package-lock.json' ':(exclude)*/vendor/*'
  ':(exclude)*/node_modules/*' ':(exclude)*.generated.*' ':(exclude)*.tsv' "$@" )
export TERMS="$terms" MODE="${PLAIN_LANGUAGE_MODE:-fail}" TODAY="${PLAIN_LANGUAGE_TODAY:-}" \
  BARE_UNTIL="${PLAIN_LANGUAGE_BARE_UNTIL:-}" BASELINE="${PLAIN_LANGUAGE_BASELINE:-}" \
  WRITE_BASELINE="${PLAIN_LANGUAGE_WRITE_BASELINE:-}"
if [ -n "$BASELINE" ]; then
  git ls-files -z -- . "${excl[@]}" ":(exclude)$BASELINE" | SCAN=tree perl "$here/scan.pl"
else
  git diff --unified=0 --no-color "$range" -- . "${excl[@]}" | SCAN=diff perl "$here/scan.pl"
fi
