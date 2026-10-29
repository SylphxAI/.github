#!/usr/bin/env bash
# Only PR merge-base calculation needs a commit graph. Merge groups compare
# exact tree endpoints; fetching a missing base never downloads its history.
set -euo pipefail
case "$EVENT_NAME" in
  pull_request)
    # The event's base sha can predate commits the branch merged in from
    # its base; diff from the merge base with the current base branch so
    # only the pull request's own lines are checked.
    base="$PR_BASE_SHA"
    if git fetch --quiet --no-tags --filter=blob:none origin "$PR_BASE_REF" 2>/dev/null; then
      base="$(git merge-base FETCH_HEAD "$PR_HEAD_SHA")"
    fi
    range="${base}..${PR_HEAD_SHA}" ;;
  merge_group)
    for revision in "$MG_BASE_SHA" "$MG_HEAD_SHA"; do
      [[ "$revision" =~ ^[0-9a-f]{40}$ ]] || { echo 'invalid merge-group revision' >&2; exit 1; }
      if ! git cat-file -e "$revision^{commit}" 2>/dev/null; then
        git fetch --quiet --no-tags --depth=1 --filter=blob:none origin "$revision"
      fi
    done
    range="${MG_BASE_SHA}..${MG_HEAD_SHA}" ;;
  *) range="${PUSH_BEFORE:-${GIT_SHA}~1}..${GIT_SHA}" ;;
esac
printf '%s\n' "$range"
