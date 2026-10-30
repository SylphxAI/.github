# Red main handler helpers (owner#809).
WORK_DIR="${RUNNER_TEMP}/red-main"
STATE_DIR="$WORK_DIR/state"
export WORK_DIR STATE_DIR GIT_TERMINAL_PROMPT=0
mkdir -p "$STATE_DIR"
# No-action is terminal for subsequent steps, not just the resolver.
# In particular, a non-proof optional/diagnostic failure cannot reach
# revert after the resolver exits successfully.
if [ -f "$STATE_DIR/quiet" ]; then exit 0; fi

# say <line>: append a line to the summary the final step posts.
say() { printf '%s\n' "$*" >>"$WORK_DIR/summary.md"; }

state_set() { printf '%s' "$2" >"$STATE_DIR/$1"; }
state_get() { cat "$STATE_DIR/$1" 2>/dev/null || true; }

# Who the writes belong to, for the messages that name a missing grant.
if [ "$(state_get token-mode)" = yes ]; then
  GRANT_HOLDER="the workflow token on $REPO"
else
  GRANT_HOLDER="the App installation on $REPO"
fi

# grant_of <surface>: granted | read-only | missing | unknown
grant_of() {
  awk -F'\t' -v k="$1" '$1==k {print $2}' "$STATE_DIR/grant.tsv" 2>/dev/null | head -1
}

# require_grant <surface> <permission> <what>: fail closed, naming the
# permission the installation does not hold. The caller stops.
require_grant() {
  local verdict
  verdict=$(grant_of "$1")
  # The probe writes `granted` (token mode) or `granted (<holder>)`.
  if [ "$verdict" != "granted" ] && [ "${verdict#granted (}" = "$verdict" ]; then
    printf '::error::%s does not grant %s (grant probe: %s); %s was not performed\n' \
      "$GRANT_HOLDER" "$2" "${verdict:-unknown}" "$3" >&2
    state_set blocked "$2"
    return 1
  fi
}

# write_refused <stderr> <permission> <what>: report a write the forge
# refused for want of a permission. Rejected credentials (HTTP 401: an
# expired or revoked token) are no missing grant: the step fails at
# once, so a handler that acted on nothing never reads as a success.
write_refused() {
  if grep -qE 'HTTP 401|Bad credentials' <<<"$1"; then
    printf '::error::%s failed: the forge rejected the credentials (HTTP 401, an expired or revoked token)\n' "$3" >&2
    exit 1
  fi
  if grep -qE 'HTTP (403|404)' <<<"$1"; then
    printf '::error::%s was refused: %s is missing %s\n' \
      "$3" "$GRANT_HOLDER" "$2" >&2
    state_set blocked "$2"
  else
    printf '::error::%s failed: %s\n' "$3" "$(head -c 300 <<<"$1" | tr '\n' ' ')" >&2
  fi
  return 1
}

# commit_prs <sha> <jq filter>: the pull requests a commit landed
# through, filtered. A failed read is not "no pull request": the
# forge's answer is named and the call returns non-zero, so the caller
# stops instead of reading an error body as a pull request number.
commit_prs() {
  local out err
  err=$(mktemp)
  if ! out=$(gh api "repos/$REPO/commits/$1/pulls" --jq "$2" 2>"$err"); then
    printf '::error::reading the pull requests of %s failed: %s\n' "$1" "$(head -c 300 "$err" | tr '\n' ' ')" >&2
    rm -f "$err"
    return 1
  fi
  rm -f "$err"
  printf '%s' "$out"
}

# git_c <git args...>: git with the App token as an in-memory
# credential. The token is read from the environment at call time and
# never written to a configuration file, a remote URL, or a prompt.
git_c() {
  # shellcheck disable=SC2016
  git -c credential.helper= \
      -c credential.helper='!f() { printf "username=x-access-token\npassword=%s\n" "$GH_TOKEN"; }; f' \
      "$@"
}

# push_branch <branch>: push HEAD normally to a fresh branch.
push_branch() {
  local branch="$1"
  [ "$#" -eq 1 ] || return 2
  git_c push origin "HEAD:refs/heads/$branch"
}

# bot_identity: the App's own git identity, from the step environment.
bot_identity() {
  local slug="${APP_SLUG:-red-main}"
  export GIT_AUTHOR_NAME="${slug}[bot]"
  export GIT_AUTHOR_EMAIL="${APP_ID:-0}+${slug}[bot]@users.noreply.github.com"
  export GIT_COMMITTER_NAME="$GIT_AUTHOR_NAME"
  export GIT_COMMITTER_EMAIL="$GIT_AUTHOR_EMAIL"
}

# labels_that_exist <name>...: JSON array of the labels the repository
# actually defines. A label the repository does not define is not a
# reason to lose the whole record.
labels_that_exist() {
  local have want out='[]'
  have=$(gh api "repos/$REPO/labels?per_page=100" --paginate --jq '.[].name' 2>/dev/null || true)
  for want in "$@"; do
    if grep -qxF "$want" <<<"$have"; then
      out=$(jq -c --arg l "$want" '. + [$l]' <<<"$out")
    fi
  done
  printf '%s' "$out"
}

# gha <gh args...>: gh on the caller-granted workflow token. Every
# Actions API call (runs, jobs, artifacts, dispatch) goes through
# it: the builder App installation holds no `actions` permission, and
# a workflow_dispatch made with GITHUB_TOKEN still starts a run.
gha() { GH_TOKEN="$ACTIONS_TOKEN" gh "$@"; }

# classify_run <run id>: infra classification of a run's failed jobs,
# `infra <signatures>` or `none`. A jobs or annotations API error is
# NOT `none`: it returns non-zero, and the caller stops, so a
# classifier error never falls into the revert path.
classify_run() {
  local run_id="$1" jobs_json enriched id check_id notes
  jobs_json=$(gha api "repos/$REPO/actions/runs/$run_id/jobs?filter=latest&per_page=100" --paginate \
    --jq '.jobs[] | select(.conclusion == "failure" or .conclusion == "timed_out")
          | {id, check_run_url, name, conclusion, steps: [(.steps // [])[] | {name, conclusion}]}' \
    | jq -s '.') || return 1
  enriched='[]'
  for id in $(jq -r '.[].id' <<<"$jobs_json"); do
    check_id=$(jq -c --argjson id "$id" '.[] | select(.id == $id)' <<<"$jobs_json" \
      | python3 "$WORK_DIR/proof.py" check-id "$REPO") || return 1
    notes=$(gha api "repos/$REPO/check-runs/$check_id/annotations?per_page=100" --jq '[.[].message]') || return 1
    enriched=$(jq -c --argjson id "$id" --argjson notes "$notes" --argjson jobs "$jobs_json" \
      '. + [ ($jobs[] | select(.id == $id)) + {annotations: $notes} ]' <<<"$enriched")
  done
  jq -c '{jobs: .}' <<<"$enriched" | python3 "$WORK_DIR/infra.py"
}

# read_proof <run|sha> <args...>: unavailable is unverified, never
# success or a reason to silence recovery under set -euo pipefail.
read_proof() {
  local verdict
  if verdict=$(python3 "$WORK_DIR/proof.py" "$@" 2>"$WORK_DIR/proof.err"); then
    printf '%s\n' "$verdict"
  else
    say "**Unverified:** the post-main proof could not be read ($(head -c 200 "$WORK_DIR/proof.err" | tr '\n' ' ')); investigation or escalation remains active."
    printf '%s\n' unknown
  fi
}

# enc <value>: percent-encode a query parameter value.
enc() { jq -rn --arg v "$1" '$v|@uri'; }
