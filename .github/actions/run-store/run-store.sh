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
# Out: key, found, bytes, unavailable in $GITHUB_OUTPUT.
#
# A 404 is the only "no entry". The gateway answers 503 while its object
# store is down: that is "store unavailable", said as such and never reported
# as a producer that did not store the entry.
#
# Both tokens are masked before any other use and travel only in a curl
# config on stdin, never in an argument list or the job's environment.
set -uo pipefail

out="${GITHUB_OUTPUT:-/dev/null}"
max_bytes="${RUN_STORE_MAX_BYTES:-1073741824}"
delay="${RUN_STORE_RETRY_DELAY:-5}"
part_bytes="${RUN_STORE_PART_BYTES:-67108864}"
parts_helper="$(dirname "${BASH_SOURCE[0]}")/parts.py"
required="${REQUIRED:-true}"
retention_days="${RETENTION_DAYS:-}"
if [ -n "$retention_days" ]; then
  if [ "${MODE:-}" != put ] || ! [[ "$retention_days" =~ ^[0-9]{1,2}$ ]] || [ "$retention_days" -lt 1 ] || [ "$retention_days" -gt 30 ]; then
    echo "::error::run-store: retention-days is put only and must be 1-30"
    exit 1
  fi
  retention_days="$((10#$retention_days))"
fi
mask() { [ "${RUN_STORE_MANUAL:-false}" = true ] || echo "::add-mask::$1"; }
if ! [[ "$part_bytes" =~ ^[0-9]{1,8}$ ]] || [ "$part_bytes" -eq 0 ] || [ "$part_bytes" -gt 67108864 ]; then
  echo "::error::run-store: part size must be 1-67108864 bytes"
  exit 1
fi

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
echo "unavailable=false" >> "$out"

# unavailable_report <detail>: the caller gets the one answer for a store
# outage, never the miss message. With required=false it warns and succeeds,
# and `unavailable=true` says found=false is not about the producer.
unavailable_report() {
  echo "unavailable=true" >> "$out"
  if [ "$required" = false ]; then
    echo "::warning::run-store $MODE $NAME: store unavailable ($1); an entry may exist but could not be read"
    exit 0
  fi
  echo "::error::run-store $MODE $NAME: store unavailable ($1), not a missing entry; the cache's object store (Ceph RGW) did not answer"
  exit 1
}

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
  if [ "${RUN_STORE_MANUAL:-false}" = true ]; then
    [ "$MODE" = get ] || { reason="manual credentials are read only"; return 2; }
    [ -n "${SYLPHX_API_KEY:-}" ] || { reason="a caller-owned Sylphx Access key is required"; return 2; }
  elif [ -z "${ACTIONS_ID_TOKEN_REQUEST_URL:-}" ] || [ -z "${ACTIONS_ID_TOKEN_REQUEST_TOKEN:-}" ]; then
    reason="the job has no id-token: write permission"
    return 2
  fi
  mask "${ACTIONS_ID_TOKEN_REQUEST_TOKEN:-}"
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
  if [ "${RUN_STORE_MANUAL:-false}" = true ]; then
    rc=0
    code="$(printf 'header = "Authorization: Bearer %s"\n' "$SYLPHX_API_KEY" \
      | curl -sS -K - --max-time "$max" -o "$work/tok" -w '%{http_code}' -X POST \
        -H 'Content-Type: application/json' \
        --data "$(printf '{"cache":"ci","access":"read","ttl_seconds":3600,"network":"%s"}' "$network")" \
        "$url/v1/tokens" 2>/dev/null)" || rc=$?
    if [ "$rc" -ne 0 ]; then reason="read token exchange: curl exit $rc"; return 1; fi
    if [ "$code" != 200 ]; then rm -f "$work/tok"; reason="read token exchange: HTTP $code"; return 2; fi
  else
  sep='?'
  case "$ACTIONS_ID_TOKEN_REQUEST_URL" in *\?*) sep='&' ;; esac
  oidc_url="${ACTIONS_ID_TOKEN_REQUEST_URL}${sep}audience=sylphx-build-cache"
  rc=0
  code="$(printf 'header = "Authorization: Bearer %s"\n' "$ACTIONS_ID_TOKEN_REQUEST_TOKEN" \
    | curl -sS -K - --max-time "$max" -o "$work/tok" -w '%{http_code}' "$oidc_url" 2>/dev/null)" || rc=$?
  if [ "$rc" -ne 0 ]; then reason="OIDC token request: curl exit $rc"; return 1; fi
  if [ "$code" != 200 ]; then reason="OIDC token request: HTTP $code"; return 1; fi
  if ! jwt="$(json_field "$work/tok" value)"; then reason="OIDC token request: invalid response"; return 1; fi
  mask "$jwt"
  rc=0
  code="$(printf 'header = "Authorization: Bearer %s"\n' "$jwt" \
    | curl -sS -K - --max-time "$max" -o "$work/tok" -w '%{http_code}' -X POST \
      -H 'Content-Type: application/json' \
      --data "$(printf '{"ttl_seconds":7200,"network":"%s"}' "$network")" \
      "$url/v1/tokens/github" 2>/dev/null)" || rc=$?
  jwt=""
  if [ "$rc" -ne 0 ]; then rm -f "$work/tok"; reason="token exchange: curl exit $rc"; return 1; fi
  if [ "$code" != 200 ]; then rm -f "$work/tok"; reason="token exchange: HTTP $code"; return 1; fi
  fi
  token="$(json_field "$work/tok" env.TURBO_TOKEN)" || token="$(json_field "$work/tok" token)" || token=""
  api="$(json_field "$work/tok" env.TURBO_API)" || api=""
  rm -f "$work/tok"
  # The length is checked apart from the pattern: macOS's regex library caps a
  # bounded repeat at 255 (RE_DUP_MAX), so {1,8192} would never match there.
  if [ -z "$token" ] || [ "${#token}" -gt 8192 ] || ! [[ "$token" =~ ^[A-Za-z0-9._~+/=-]+$ ]] \
    || ! [[ "$api" =~ ^https?://[A-Za-z0-9.:/_~-]+$ ]]; then
    token=""
    reason="token exchange: invalid response"
    return 1
  fi
  mask "$token"
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
if [ "$MODE" = get ] || [ -n "$retention_days" ]; then entry="$api/v1/retained/$key"; fi

# call <method> <out-file> [curl args...]: prints the HTTP status, or 000.
call() {
  local method="$1" dest="$2" c
  shift 2
  if [ "$method" = PUT ] && [ -n "$retention_days" ]; then
    # The root commits last. Pieces get an extra day, and the root receipt
    # below must expire no later than any piece; no shorter bundle succeeds.
    local requested_days="$retention_days"
    [ "${is_part:-false}" != true ] || requested_days=$((retention_days + 1))
    set -- -H "x-build-cache-retention-days: $requested_days" "$@"
    if [ "${is_part:-false}" != true ] && [ -n "${earliest_part_until:-}" ]; then
      set -- -H "x-build-cache-retention-not-after: $earliest_part_until" "$@"
    fi
  fi
  c="$(printf 'header = "Authorization: Bearer %s"\n' "$token" \
    | curl -sS -K - -X "$method" --connect-timeout 20 --max-time 3600 \
      -D "$work/headers" -o "$dest" -w '%{http_code}' "$@" "$entry" 2>/dev/null)" || true
  [[ "$c" =~ ^[0-9]{3}$ ]] || c=000
  echo "$c"
}

file_bytes() { wc -c < "$1" | tr -d ' '; }

# Only canonical trace identifiers are diagnostics. Never emit response bodies,
# arbitrary header values, URLs or authorization material from a refused PUT.
response_id() {
  python3 - "$work/headers" <<'PYID'
import pathlib, re, sys
try:
    headers = pathlib.Path(sys.argv[1]).read_text(errors="replace")[-16384:]
except OSError:
    print("unavailable")
    raise SystemExit
for line in reversed(headers.splitlines()):
    name, sep, value = line.partition(":")
    value = value.strip()
    if sep and name.lower() == "cf-ray" and re.fullmatch(r"[0-9a-fA-F]{16}-[A-Z]{3}", value):
        print("cf-ray:" + value)
        break
    if sep and name.lower() in ("x-request-id", "request-id") and re.fullmatch(r"[0-9a-fA-F-]{16,64}", value):
        print("request-id:" + value)
        break
else:
    print("unavailable")
PYID
}

retention_receipt() {
  [ -n "$retention_days" ] || return 0
  python3 - "$work/headers" "$retention_days" <<'PYRETENTION'
import pathlib, sys, time
headers = {}
for line in pathlib.Path(sys.argv[1]).read_text().splitlines():
    name, sep, value = line.partition(":")
    if sep:
        headers[name.lower()] = value.strip()
try:
    days = int(headers["x-build-cache-retention-days"])
    until = int(headers["x-build-cache-retain-until"])
except (ValueError, KeyError):
    raise SystemExit(1)
if days < int(sys.argv[2]) or until <= time.time():
    raise SystemExit(1)
print("retained_until=" + str(until))
PYRETENTION
}

verify_download() {
  python3 - "$work/headers" "$1" "$entry" <<'PYDIGEST'
import hashlib, pathlib, re, sys
headers = {}
for line in pathlib.Path(sys.argv[1]).read_text().splitlines():
    name, sep, value = line.partition(":")
    if sep:
        headers[name.lower()] = value.strip()
expected = headers.get("x-build-cache-sha256")
if expected is None and "/v1/retained/" not in sys.argv[3]:
    raise SystemExit(0)  # old gateway's existing cache contract
if not expected or not re.fullmatch(r"[0-9a-f]{64}", expected):
    raise SystemExit(1)
digest = hashlib.sha256()
with open(sys.argv[2], "rb") as source:
    for chunk in iter(lambda: source.read(1024 * 1024), b""):
        digest.update(chunk)
actual = digest.hexdigest()
raise SystemExit(0 if actual == expected else 1)
PYDIGEST
}

# A retry keeps the first immutable root and its original deadline. Validate
# the whole published graph before acknowledging it, including a raced root.
check_existing_retained() {
  local root_until existing_parts suffix index code receipt until saved_entry
  saved_entry="$entry"
  receipt="$(retention_receipt)" || fail "the existing bundle does not satisfy retention-days=$retention_days"
  root_until="${receipt#retained_until=}"
  code="$(call GET "$work/existing-root" --max-filesize "$max_bytes")"
  [ "$code" = 200 ] || fail "the existing bundle is unreadable (HTTP $code)"
  verify_download "$work/existing-root" || fail "the existing bundle failed SHA256 verification"
  existing_parts="$(python3 "$parts_helper" inspect "$work/existing-root" "$work" "$max_bytes" "$part_bytes")" || fail "invalid existing parts manifest"
  if [ "$existing_parts" -gt 0 ]; then
    suffix="$(cat "$work/part-suffix")"
    index=0
    while [ "$index" -lt "$existing_parts" ]; do
      entry="$saved_entry$suffix$index"
      code="$(call GET "$work/part-$index" --max-filesize "$part_bytes")"
      [ "$code" = 200 ] || fail "an existing bundle part is missing or unreadable (HTTP $code)"
      verify_download "$work/part-$index" || fail "an existing bundle part failed SHA256 verification"
      receipt="$(retention_receipt)" || fail "an existing bundle part has no retention receipt"
      until="${receipt#retained_until=}"
      [ "$until" -ge "$root_until" ] || fail "the existing bundle outlives a part"
      index=$((index + 1))
    done
    python3 "$parts_helper" join "$work/existing-root" "$work" "$max_bytes" "$part_bytes" || fail "incomplete or corrupt existing bundle"
  fi
  entry="$saved_entry"
  echo "retained_until=$root_until" >> "$out"
}

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
  echo "run-store put $NAME: packed_bytes=$bytes"
  [ "$bytes" -le "$max_bytes" ] || fail "the packed bundle is $bytes bytes, over the gateway's $max_bytes"
  parts=0
  if [ "$bytes" -gt "$part_bytes" ]; then
    generation=""
    if [ -n "$retention_days" ]; then generation="$(python3 -c 'import secrets; print(secrets.token_hex(16))')" || fail "could not name the upload generation"; fi
    parts="$(python3 "$parts_helper" split "$work/bundle.tgz" "$work" "$max_bytes" "$part_bytes" "$generation")" || fail "could not split the packed bundle"
    part_suffix="$(cat "$work/part-suffix")"
  fi
  root_entry="$entry"
  # Keep an already committed protected root before touching any of its parts.
  if [ "$parts" -gt 0 ] || [ -n "$retention_days" ]; then
    code="$(call HEAD /dev/null -I)"
    case "$code" in
      200)
        if grep -qi '^x-build-cache-scope: *protected' "$work/headers" || grep -qi '^x-build-cache-retain-until:' "$work/headers"; then
          if [ -n "$retention_days" ]; then check_existing_retained; fi
          echo "run-store put $NAME: an entry already exists; it is kept"
          exit 0
        fi
        ;;
      404) ;;
      503) unavailable_report "HTTP 503 while checking the existing bundle" ;;
      *) fail "could not check the existing bundle (HTTP $code)" ;;
    esac
  fi
  store_entry() {
    local payload="$1"
    stored=""
    for attempt in 1 2 3; do
      code="$(call PUT "$work/resp" -H 'Content-Type: application/octet-stream' -T "$payload")"
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
          if [ -n "$retention_days" ] && [ "${is_part:-false}" != true ]; then
            code="$(call HEAD /dev/null -I)"
            [ "$code" = 200 ] || fail "the existing bundle is unreadable (HTTP $code)"
            check_existing_retained
            exit 0
          fi
          if [ "${is_part:-false}" = true ]; then
            code="$(call GET "$work/existing-part" --max-filesize "$part_bytes")"
            if [ "$code" != 200 ] || ! cmp -s "$payload" "$work/existing-part"; then
              fail "an existing immutable part differs; no manifest was committed"
            fi
          fi
          stored=yes
          break
          ;;
        503)
          reason="store unavailable (HTTP 503 from the build cache)"
          unavailable_report "$reason"
          ;;
        400 | 403 | 411 | 413 | 507) reason="HTTP $code packed_bytes=$bytes request_bytes=$(file_bytes "$payload") response_id=$(response_id)"; break ;;
        *) reason="HTTP $code" ;;
      esac
      [ "$attempt" -lt 3 ] && sleep "$delay"
    done
    [ -n "$stored" ] || fail "could not store the bundle ($reason)"
    # The PUT has already been accepted. Recheck its receipt with the same
    # bounded read policy as GET; never send the accepted upload again.
    for attempt in 1 2 3; do
      code="$(call HEAD /dev/null -I)"
      case "$code" in 200 | 401 | 403) break ;; esac
      [ "$attempt" -lt 3 ] && sleep "$delay"
    done
    if [ "$code" = 503 ]; then
      unavailable_report "HTTP $code while reading back the accepted bundle"
    fi
    [ "$code" = 200 ] || fail "the stored bundle does not read back (HTTP $code)"
    local receipt until
    receipt="$(retention_receipt)" || fail "the gateway did not confirm retention-days=$retention_days"
    if [ -n "$receipt" ]; then
      until="${receipt#retained_until=}"
      if [ "${is_part:-false}" = true ]; then
        if [ -z "${earliest_part_until:-}" ] || [ "$until" -lt "$earliest_part_until" ]; then earliest_part_until="$until"; fi
      elif [ -n "${earliest_part_until:-}" ] && [ "$until" -gt "$earliest_part_until" ]; then
        fail "the bundle would outlive a part; retention was not established"
      fi
      if [ "${is_part:-false}" != true ]; then echo "$receipt" >> "$out"; fi
    fi
  }
  if [ "$parts" -gt 0 ]; then
    index=0
    is_part=true
    while [ "$index" -lt "$parts" ]; do
      entry="$root_entry$part_suffix$index"
      store_entry "$work/part-$index"
      index=$((index + 1))
    done
    is_part=false
    entry="$root_entry"
    # Commit last: an absent or refused part never exposes a completed bundle.
    store_entry "$work/manifest"
  else
    store_entry "$work/bundle.tgz"
  fi
  echo "run-store put $NAME: $bytes bytes as $key"
  exit 0
fi

# get
code=000
for attempt in 1 2 3; do
  entry="$api/v1/retained/$key"
  code="$(call GET "$work/bundle.tgz" --max-filesize "$max_bytes")"
  if [ "$code" = 404 ] && [ "$entry" = "$api/v1/retained/$key" ]; then
    # Existing seven-day callers continue to read the original Turbo door.
    entry="$api/v8/artifacts/$key"
    code="$(call GET "$work/bundle.tgz" --max-filesize "$max_bytes")"
  fi
  case "$code" in 200 | 401 | 403) break ;; esac
  [ "$attempt" -lt 3 ] && sleep "$delay"
done
if [ "$code" = 503 ]; then
  unavailable_report "HTTP $code from the build cache's object store"
fi
if [ "$code" = 404 ]; then
  fail "no entry $key (the producer job did not store it in run $STORE_RUN_ID, or it expired)"
fi
[ "$code" = 200 ] || fail "could not fetch $key (HTTP $code)"
verify_download "$work/bundle.tgz" || fail "the downloaded entry failed SHA256 verification"
parts="$(python3 "$parts_helper" inspect "$work/bundle.tgz" "$work" "$max_bytes" "$part_bytes")" || fail "invalid parts manifest"
if [ "$parts" -gt 0 ]; then
  root_entry="$entry"
  part_suffix="$(cat "$work/part-suffix")"
  index=0
  while [ "$index" -lt "$parts" ]; do
    entry="$root_entry$part_suffix$index"
    for attempt in 1 2 3; do
      code="$(call GET "$work/part-$index" --max-filesize "$part_bytes")"
      case "$code" in 200 | 401 | 403 | 503) break ;; esac
      [ "$attempt" -lt 3 ] && sleep "$delay"
    done
    [ "$code" != 503 ] || unavailable_report "HTTP 503 while reading bundle parts"
    [ "$code" = 200 ] || fail "bundle part $index is missing or unreadable (HTTP $code)"
    verify_download "$work/part-$index" || fail "bundle part $index failed SHA256 verification"
    index=$((index + 1))
  done
  python3 "$parts_helper" join "$work/bundle.tgz" "$work" "$max_bytes" "$part_bytes" || fail "incomplete or corrupt parts bundle"
  mv "$work/bundle-joined.tgz" "$work/bundle.tgz" || fail "could not assemble bundle"
fi
bytes="$(file_bytes "$work/bundle.tgz")"
[ "$bytes" -le "$max_bytes" ] || fail "the stored bundle exceeds the size limit"
# The bundle's tar members are untrusted: reject an escaping or special member
# before anything is written, so a bundle can never overwrite a trusted file or
# reach outside STORE_PATH.
if ! command -v python3 > /dev/null 2>&1; then
  fail "python3 is required to validate the bundle"
fi
gzip -t "$work/bundle.tgz" 2>/dev/null || fail "refusing to unpack $key: corrupt gzip archive"
python3 "$(dirname "${BASH_SOURCE[0]}")/validate_archive.py" "$work/bundle.tgz" "$STORE_PATH" || fail "refusing to unpack $key: unsafe archive member"
mkdir -p "$STORE_PATH" || fail "cannot create $STORE_PATH"
gzip -dc "$work/bundle.tgz" | tar -C "$STORE_PATH" -xf -
rc=("${PIPESTATUS[@]}")
if [ "${rc[0]}" -ne 0 ] || [ "${rc[1]}" -ne 0 ]; then fail "could not unpack $key"; fi
echo "bytes=$bytes" >> "$out"
echo "found=true" >> "$out"
echo "run-store get $NAME: $bytes bytes into $STORE_PATH"
exit 0
