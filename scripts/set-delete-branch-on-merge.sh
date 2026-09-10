#!/usr/bin/env bash
#
# set-delete-branch-on-merge.sh -- enable auto-delete of merged PR head branches.
#
# Policy: docs/repository-settings.md
#
# Usage:
#   scripts/set-delete-branch-on-merge.sh [--apply] [owner...]
#
# Default owner is SylphxAI. Without --apply the script only reports what it
# would change. Requires an authenticated `gh` with admin rights on each
# targeted repository.

set -euo pipefail

APPLY=0
OWNERS=()

usage() {
  cat <<'USAGE'
Usage: set-delete-branch-on-merge.sh [--apply] [owner...]

Enable delete_branch_on_merge=true ("Automatically delete head branches") on
every eligible repository owned by each owner. Default owner: SylphxAI.

Eligible repositories are not archived, not forks, and not disabled.

Options:
  --apply     write the setting (default: dry-run, prints WOULD-PATCH only)
  -h, --help  show this help

Examples:
  scripts/set-delete-branch-on-merge.sh
  scripts/set-delete-branch-on-merge.sh --apply SylphxAI shtse8

Per repository the script reports OK (already true), WOULD-PATCH / PATCHED,
SKIP (no admin permission), or FAILED (API error or unverified write).
It exits non-zero when any repository FAILED.
USAGE
}

while (($# > 0)); do
  case "$1" in
    --apply)
      APPLY=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      break
      ;;
    -*)
      printf 'unknown option: %s\n\n' "$1" >&2
      usage >&2
      exit 2
      ;;
    *)
      OWNERS+=("$1")
      shift
      ;;
  esac
done

while (($# > 0)); do
  OWNERS+=("$1")
  shift
done

if ((${#OWNERS[@]} == 0)); then
  OWNERS=(SylphxAI)
fi

if ! command -v gh >/dev/null 2>&1; then
  printf 'ERROR: the GitHub CLI (gh) is required\n' >&2
  exit 2
fi

if ! AUTH_LOGIN="$(gh api user --jq .login 2>/dev/null)"; then
  printf 'ERROR: gh is not authenticated; run `gh auth login` first\n' >&2
  exit 2
fi

if ((APPLY)); then
  MODE=apply
else
  MODE=dry-run
fi

already_true=0
pending=0
failed=0
skipped=0

# Eligible repositories (not archived, not forks, not disabled) for one owner.
eligible_repos() {
  local owner="$1" endpoint
  local filter='.[] | select(.archived == false and .fork == false and .disabled == false) | .full_name'

  if [[ "${owner,,}" == "${AUTH_LOGIN,,}" ]]; then
    endpoint='user/repos?per_page=100&affiliation=owner'
  else
    endpoint="orgs/$owner/repos?per_page=100"
  fi

  gh api "$endpoint" --paginate --jq "$filter"
}

printf 'delete_branch_on_merge sweep -- mode: %s -- owners: %s\n' "$MODE" "${OWNERS[*]}"

for owner in "${OWNERS[@]}"; do
  printf '\n== %s ==\n' "$owner"

  repo_list=""
  if ! repo_list="$(eligible_repos "$owner")"; then
    printf '%-11s %s (repository enumeration failed)\n' FAILED "$owner"
    failed=$((failed + 1))
    continue
  fi

  if [[ -z "$repo_list" ]]; then
    printf 'no eligible repositories\n'
    continue
  fi

  while IFS= read -r full; do
    [[ -z "$full" ]] && continue

    meta=""
    if ! meta="$(gh api "repos/$full" --jq '[.delete_branch_on_merge, (.permissions.admin // false)] | @tsv' 2>/dev/null)"; then
      printf '%-11s %s (repository read failed)\n' FAILED "$full"
      failed=$((failed + 1))
      continue
    fi

    current="${meta%%$'\t'*}"
    admin="${meta##*$'\t'}"

    if [[ "$admin" != "true" ]]; then
      printf '%-11s %s (no admin permission)\n' SKIP "$full"
      skipped=$((skipped + 1))
      continue
    fi

    if [[ "$current" == "true" ]]; then
      printf '%-11s %s\n' OK "$full"
      already_true=$((already_true + 1))
      continue
    fi

    if ((APPLY == 0)); then
      printf '%-11s %s\n' WOULD-PATCH "$full"
      pending=$((pending + 1))
      continue
    fi

    if ! gh api --method PATCH "repos/$full" -F delete_branch_on_merge=true >/dev/null 2>&1; then
      printf '%-11s %s (patch rejected)\n' FAILED "$full"
      failed=$((failed + 1))
      continue
    fi

    verified=""
    if ! verified="$(gh api "repos/$full" --jq '.delete_branch_on_merge' 2>/dev/null)"; then
      printf '%-11s %s (verification read failed)\n' FAILED "$full"
      failed=$((failed + 1))
      continue
    fi

    if [[ "$verified" == "true" ]]; then
      printf '%-11s %s\n' PATCHED "$full"
      pending=$((pending + 1))
    else
      printf '%-11s %s (still false after patch)\n' FAILED "$full"
      failed=$((failed + 1))
    fi
  done <<< "$repo_list"
done

printf '\nSummary (%s)\n' "$MODE"
if ((APPLY)); then
  printf '  patched      : %d\n' "$pending"
else
  printf '  would_patch  : %d\n' "$pending"
fi
printf '  already_true : %d\n' "$already_true"
printf '  skipped      : %d\n' "$skipped"
printf '  failed       : %d\n' "$failed"

if ((APPLY == 0)); then
  printf '\ndry-run: no settings written; re-run with --apply to converge.\n'
fi

if ((failed > 0)); then
  printf 'FAILED: %d target(s) did not converge\n' "$failed" >&2
  exit 1
fi

exit 0
