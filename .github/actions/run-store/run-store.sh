#!/usr/bin/env bash
# run-store: hand a file or directory between the jobs of one workflow run
# through the BuildCache gateway's Turbo door (`{TURBO_API}/v8/artifacts/<key>`),
# never through GitHub artifact storage. Runs on Linux and macOS (bash 3.2).
#
# In:  MODE (put|get), NAME, STORE_PATH, STORE_RUN_ID, REPO_ID, REQUIRED,
#      BUILD_CACHE_URL, BUILD_CACHE_NETWORK, BUILD_CACHE_MAX_TIME (seconds for
#      the token calls, default 10), RUN_STORE_MAX_BYTES (default 1 GiB, the
#      gateway's largest entry), RUN_STORE_RETRY_DELAY (seconds, default 5),
#      ACTIONS_ID_TOKEN_REQUEST_URL, ACTIONS_ID_TOKEN_REQUEST_TOKEN,
#      GITHUB_OUTPUT, RUNNER_TEMP.
# Out: key, found, bytes in $GITHUB_OUTPUT.
#
# Both tokens are masked before any other use and travel only in a curl
# config on stdin, never in an argument list or the job's environment.
set -uo pipefail

out="${GITHUB_OUTPUT:-/dev/null}"
max_bytes="${RUN_STORE_MAX_BYTES:-1073741824}"
delay="${RUN_STORE_RETRY_DELAY:-5}"
required="${REQUIRED:-true}"

fail() {
  # A put with required=false and every get miss with required=false only warn.
  if [ "$required" = false ]; then
    echo "::warning::run-store $MODE $NAME: $1"
    exit 0
  fi
  echo "::error::run-store $MODE $NAME: $1"
  exit 1
}

case "${MODE:-}" in put | get) ;; *) echo "::error::run-store: mode must be put or get"; exit 1 ;; esac
if ! [[ "${NAME:-}" =~ ^[A-Za-z0-9._-]{1,100}$ ]]; then
  echo "::error::run-store: name must be 1-100 of [A-Za-z0-9._-]"
  exit 1
fi
if ! [[ "${STORE_RUN_ID:-}" =~ ^[0-9]{1,20}$ ]] || ! [[ "${REPO_ID:-}" =~ ^[0-9]{1,20}$ ]]; then
  echo "::error::run-store: run id and repository id must be numbers"
  exit 1
fi
[ -n "${STORE_PATH:-}" ] || { echo "::error::run-store: path is required"; exit 1; }

# One key per repository, run and name. The gateway's org prefix and the
# event's scope sit above it, so another org or a pull request run cannot
# name it for a main run.
key="run-store.v1.${REPO_ID}.${STORE_RUN_ID}.${NAME}"
echo "key=$key" >> "$out"
[ "$MODE" = get ] && echo "found=false" >> "$out"

json_field() { # json_field <file> <dotted.path>: prints a string field or fails
  if command -v jq > /dev/null 2>&1; then
    jq -er ".$2 | strings" "$1" 2>/dev/null
  else
    python3 -c '
import json, sys
v = json.load(open(sys.argv[1]))
for k in sys.argv[2].split("."):
    v = v.get(k) if isinstance(v, dict) else None
if not isinstance(v, str):
    sys.exit(1)
print(v)' "$1" "$2" 2>/dev/null
  fi
}

work="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/run-store.XXXXXX")" || fail "no temp directory"
trap 'rm -rf "$work"' EXIT
chmod 700 "$work"

# --- token: the job's OIDC token for a BuildCache run token ----------------
exchange() {
  local url network max sep oidc_url code rc jwt
  local url_re='^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~-]+)*/?$'
  if [ -z "${ACTIONS_ID_TOKEN_REQUEST_URL:-}" ] || [ -z "${ACTIONS_ID_TOKEN_REQUEST_TOKEN:-}" ]; then
    reason="the job has no id-token: write permission"
    return 2
  fi
  echo "::add-mask::$ACTIONS_ID_TOKEN_REQUEST_TOKEN"
  url="${BUILD_CACHE_URL:-https://build-cache.sylphx.net}"
  network="${BUILD_CACHE_NETWORK:-public}"
  max="${BUILD_CACHE_MAX_TIME:-10}"
  [[ "$max" =~ ^[0-9]{1,3}$ ]] || max=10
  if ! [[ "$url" =~ $url_re ]]; then reason="build-cache-url is not an http(s) URL"; return 2; fi
  url="${url%/}"
  if [ "$network" != public ] && [ "$network" != cluster ]; then
    reason="build-cache-network must be public or cluster"
    return 2
  fi
  sep='?'
  case "$ACTIONS_ID_TOKEN_REQUEST_URL" in *\?*) sep='&' ;; esac
  oidc_url="${ACTIONS_ID_TOKEN_REQUEST_URL}${sep}audience=sylphx-build-cache"
  rc=0
  code="$(printf 'header = "Authorization: Bearer %s"\n' "$ACTIONS_ID_TOKEN_REQUEST_TOKEN" \
    | curl -sS -K - --max-time "$max" -o "$work/tok" -w '%{http_code}' "$oidc_url" 2>/dev/null)" || rc=$?
  if [ "$rc" -ne 0 ]; then reason="OIDC token request: curl exit $rc"; return 1; fi
  if [ "$code" != 200 ]; then reason="OIDC token request: HTTP $code"; return 1; fi
  if ! jwt="$(json_field "$work/tok" value)"; then reason="OIDC token request: invalid response"; return 1; fi
  echo "::add-mask::$jwt"
  rc=0
  code="$(printf 'header = "Authorization: Bearer %s"\n' "$jwt" \
    | curl -sS -K - --max-time "$max" -o "$work/tok" -w '%{http_code}' -X POST \
      -H 'Content-Type: application/json' \
      --data "$(printf '{"ttl_seconds":7200,"network":"%s"}' "$network")" \
      "$url/v1/tokens/github" 2>/dev/null)" || rc=$?
  jwt=""
  if [ "$rc" -ne 0 ]; then rm -f "$work/tok"; reason="token exchange: curl exit $rc"; return 1; fi
  if [ "$code" != 200 ]; then rm -f "$work/tok"; reason="token exchange: HTTP $code"; return 1; fi
  token="$(json_field "$work/tok" env.TURBO_TOKEN)" || token="$(json_field "$work/tok" token)" || token=""
  api="$(json_field "$work/tok" env.TURBO_API)" || api=""
  rm -f "$work/tok"
  if ! [[ "$token" =~ ^[A-Za-z0-9._~+/=-]{1,8192}$ ]] || ! [[ "$api" =~ ^https?://[A-Za-z0-9.:/_~-]+$ ]]; then
    token=""
    reason="token exchange: invalid response"
    return 1
  fi
  echo "::add-mask::$token"
  api="${api%/}"
  return 0
}

token=""
api=""
reason=""
for attempt in 1 2 3; do
  exchange
  rc=$?
  [ "$rc" -eq 0 ] && break
  [ "$rc" -eq 2 ] && fail "BuildCache unavailable ($reason)"
  [ "$attempt" -lt 3 ] && sleep "$delay"
done
[ -n "$token" ] || fail "BuildCache unavailable ($reason)"
entry="$api/v8/artifacts/$key"

# call <method> <out-file> [curl args...]: prints the HTTP status, or 000.
call() {
  local method="$1" dest="$2" c
  shift 2
  c="$(printf 'header = "Authorization: Bearer %s"\n' "$token" \
    | curl -sS -K - -X "$method" --connect-timeout 20 --max-time 3600 \
      -D "$work/headers" -o "$dest" -w '%{http_code}' "$@" "$entry" 2>/dev/null)" || true
  [[ "$c" =~ ^[0-9]{3}$ ]] || c=000
  echo "$c"
}

file_bytes() { wc -c < "$1" | tr -d ' '; }

if [ "$MODE" = put ]; then
  if [ -d "$STORE_PATH" ]; then
    tar -C "$STORE_PATH" -cf - . | gzip -1 > "$work/bundle.tgz"
    rc=("${PIPESTATUS[@]}")
  elif [ -f "$STORE_PATH" ]; then
    tar -C "$(dirname "$STORE_PATH")" -cf - "$(basename "$STORE_PATH")" | gzip -1 > "$work/bundle.tgz"
    rc=("${PIPESTATUS[@]}")
  else
    fail "path $STORE_PATH does not exist"
  fi
  if [ "${rc[0]}" -ne 0 ] || [ "${rc[1]}" -ne 0 ]; then fail "could not pack $STORE_PATH"; fi
  bytes="$(file_bytes "$work/bundle.tgz")"
  echo "bytes=$bytes" >> "$out"
  [ "$bytes" -le "$max_bytes" ] || fail "the packed bundle is $bytes bytes, over the gateway's $max_bytes"
  stored=""
  for attempt in 1 2 3; do
    code="$(call PUT "$work/resp" -H 'Content-Type: application/octet-stream' -T "$work/bundle.tgz")"
    case "$code" in
      202)
        if grep -qi '^x-build-cache: *dropped' "$work/headers" 2>/dev/null; then
          reason="the gateway dropped the write (store unavailable)"
        else
          stored=yes
          break
        fi
        ;;
      409)
        # A protected entry is written once: a re-run of the producer keeps the
        # first attempt's bundle, built from the same commit.
        echo "::notice::run-store put $NAME: the entry exists from an earlier attempt of this run; it is kept"
        stored=yes
        break
        ;;
      403 | 411 | 413 | 507) reason="HTTP $code"; break ;;
      *) reason="HTTP $code" ;;
    esac
    [ "$attempt" -lt 3 ] && sleep "$delay"
  done
  [ -n "$stored" ] || fail "could not store the bundle ($reason)"
  code="$(call HEAD /dev/null -I)"
  [ "$code" = 200 ] || fail "the stored bundle does not read back (HTTP $code)"
  echo "run-store put $NAME: $bytes bytes as $key"
  exit 0
fi

# get
code=000
for attempt in 1 2 3; do
  code="$(call GET "$work/bundle.tgz")"
  # A gateway whose store is down answers reads 404, so a miss is retried too.
  case "$code" in 200 | 401 | 403) break ;; esac
  [ "$attempt" -lt 3 ] && sleep "$delay"
done
if [ "$code" = 404 ]; then
  fail "no entry $key (the producer job did not store it in run $STORE_RUN_ID, or it expired)"
fi
[ "$code" = 200 ] || fail "could not fetch $key (HTTP $code)"
bytes="$(file_bytes "$work/bundle.tgz")"
mkdir -p "$STORE_PATH" || fail "cannot create $STORE_PATH"
gzip -dc "$work/bundle.tgz" | tar -C "$STORE_PATH" -xf -
rc=("${PIPESTATUS[@]}")
if [ "${rc[0]}" -ne 0 ] || [ "${rc[1]}" -ne 0 ]; then fail "could not unpack $key"; fi
echo "bytes=$bytes" >> "$out"
echo "found=true" >> "$out"
echo "run-store get $NAME: $bytes bytes into $STORE_PATH"
exit 0
