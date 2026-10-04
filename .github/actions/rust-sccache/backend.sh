# shellcheck shell=bash disable=SC2034  # sourced: the caller reads backend and backend_label
# Sourced by the rust-sccache action (start.sh) and its tests: chooses the
# sccache backend, first that applies, and exports its settings to this shell
# only (the sccache server it starts inherits them; $GITHUB_ENV never sees a
# token).
#
#   1. buildcache  the organization's BuildCache gateway. The job proves its
#                  identity with a GitHub OIDC token (audience
#                  sylphx-build-cache, needs `permissions: id-token: write`)
#                  and the gateway mints a run token scoped by the event:
#                  protected for main, the merge queue and schedules, dev for
#                  everything else. Skipped when the job has no OIDC access or
#                  BUILD_CACHE is false.
#   2. static      the organization's own object-store user, passed as the
#                  s3-access-key / s3-secret-key inputs (SYLPHX_CI_CACHE_*)
#   3. gha         the repository's GitHub Actions cache, unless ACTIONS_CACHE
#                  is false (the caller then brings its own fallback)
#   4. none
#
# Any failure of one backend selects the next, with one warning that names the
# HTTP status or curl exit code and never a body or a token. A backend named in
# SKIP_BACKENDS (space separated) is not tried: start.sh adds one whose server
# would not start and sources this file again.
#
# The platform-wide `ci-sccache` bucket is never a backend here: it belongs to
# the platform's own CI, and a repository never holds its key.
#
# In: BUILD_CACHE, BUILD_CACHE_URL, BUILD_CACHE_NETWORK, BUILD_CACHE_MAX_TIME
#     (seconds, default 10), ACTIONS_ID_TOKEN_REQUEST_URL,
#     ACTIONS_ID_TOKEN_REQUEST_TOKEN, S3_ACCESS_KEY, S3_SECRET_KEY, S3_ENDPOINT,
#     S3_BUCKET, KEY_PREFIX, ACTIONS_CACHE, SKIP_BACKENDS, GITHUB_REPOSITORY_OWNER.
# Out: backend (buildcache|static|gha|none), backend_label, and the SCCACHE_*
#      and AWS_* variables sccache reads.

# Settings of a backend tried earlier in this shell must not leak into the next.
unset SCCACHE_WEBDAV_ENDPOINT SCCACHE_WEBDAV_TOKEN SCCACHE_WEBDAV_KEY_PREFIX SCCACHE_IGNORE_SERVER_IO_ERROR \
  SCCACHE_BUCKET SCCACHE_ENDPOINT SCCACHE_REGION SCCACHE_S3_KEY_PREFIX SCCACHE_GHA_ENABLED SCCACHE_GHA_VERSION

backend=none
backend_label="no cache"

_backend_skipped() {
  case " ${SKIP_BACKENDS:-} " in *" $1 "*) return 0 ;; esac
  return 1
}

_buildcache_warn() {
  echo "::warning::BuildCache unavailable ($1); trying the next cache backend"
}

# Exchange the job's OIDC token for a run token; on success export the sccache
# WebDAV settings and return 0. Every failure returns 1 after one warning.
_buildcache() {
  local url network max sep oidc_url tmp rc code jwt mint endpoint
  local url_re='^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~-]+)*/?$'
  if [ -z "${ACTIONS_ID_TOKEN_REQUEST_URL:-}" ] || [ -z "${ACTIONS_ID_TOKEN_REQUEST_TOKEN:-}" ]; then
    _buildcache_warn "the job has no id-token: write permission"
    return 1
  fi
  # Mask before any other use.
  echo "::add-mask::$ACTIONS_ID_TOKEN_REQUEST_TOKEN"
  url="${BUILD_CACHE_URL-https://build-cache.sylphx.net}"
  network="${BUILD_CACHE_NETWORK-public}"
  max="${BUILD_CACHE_MAX_TIME:-10}"
  [[ "$max" =~ ^[0-9]{1,3}$ ]] || max=10
  if ! [[ "$url" =~ $url_re ]]; then
    _buildcache_warn "build-cache-url is not an http(s) URL"
    return 1
  fi
  url="${url%/}"
  if [ "$network" != public ] && [ "$network" != cluster ]; then
    _buildcache_warn "build-cache-network must be public or cluster"
    return 1
  fi
  tmp="$(mktemp "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/buildcache.XXXXXX")" || { _buildcache_warn "no temp file"; return 1; }

  # 1. The job's OIDC token. Credentials travel in a curl config on stdin, so
  #    they are never in a process argument list.
  sep='?'
  case "$ACTIONS_ID_TOKEN_REQUEST_URL" in *\?*) sep='&' ;; esac
  oidc_url="${ACTIONS_ID_TOKEN_REQUEST_URL}${sep}audience=sylphx-build-cache"
  rc=0
  code="$(printf 'header = "Authorization: Bearer %s"\n' "$ACTIONS_ID_TOKEN_REQUEST_TOKEN" \
    | curl -sS -K - --max-time "$max" -o "$tmp" -w '%{http_code}' "$oidc_url" 2>/dev/null)" || rc=$?
  if [ "$rc" -ne 0 ]; then rm -f "$tmp"; _buildcache_warn "OIDC token request: curl exit $rc"; return 1; fi
  if [ "$code" != 200 ]; then rm -f "$tmp"; _buildcache_warn "OIDC token request: HTTP $code"; return 1; fi
  if ! jwt="$(jq -er '.value | strings' "$tmp" 2>/dev/null)"; then
    rm -f "$tmp"; _buildcache_warn "OIDC token request: invalid response"; return 1
  fi
  echo "::add-mask::$jwt"

  # 2. The run token.
  rc=0
  code="$(printf 'header = "Authorization: Bearer %s"\n' "$jwt" \
    | curl -sS -K - --max-time "$max" -o "$tmp" -w '%{http_code}' -X POST \
      -H 'Content-Type: application/json' \
      --data "$(printf '{"ttl_seconds":22500,"network":"%s"}' "$network")" \
      "$url/v1/tokens/github" 2>/dev/null)" || rc=$?
  jwt=""
  if [ "$rc" -ne 0 ]; then rm -f "$tmp"; _buildcache_warn "token exchange: curl exit $rc"; return 1; fi
  if [ "$code" != 200 ]; then rm -f "$tmp"; _buildcache_warn "token exchange: HTTP $code"; return 1; fi
  if ! mint="$(jq -er '(.env.SCCACHE_WEBDAV_TOKEN // .token) | strings' "$tmp" 2>/dev/null)"; then
    rm -f "$tmp"; _buildcache_warn "token exchange: invalid response"; return 1
  fi
  echo "::add-mask::$mint"
  endpoint="$(jq -er '.env.SCCACHE_WEBDAV_ENDPOINT | strings' "$tmp" 2>/dev/null)" || endpoint=""
  rm -f "$tmp"
  if ! [[ "$endpoint" =~ ^https?://[A-Za-z0-9.:/_~-]+$ ]] || ! [[ "$mint" =~ ^[A-Za-z0-9._~+/=-]{1,8192}$ ]]; then
    _buildcache_warn "token exchange: invalid response"
    return 1
  fi
  export SCCACHE_WEBDAV_ENDPOINT="$endpoint" SCCACHE_WEBDAV_TOKEN="$mint" \
    SCCACHE_WEBDAV_KEY_PREFIX="$KEY_PREFIX" SCCACHE_IGNORE_SERVER_IO_ERROR=1
  backend_label="BuildCache $endpoint (prefix $KEY_PREFIX)"
  return 0
}

if ! _backend_skipped buildcache && [ "${BUILD_CACHE:-true}" != false ]; then
  if _buildcache; then backend=buildcache; fi
fi

if [ "$backend" = none ] && ! _backend_skipped static && [ -n "${S3_ACCESS_KEY:-}" ] && [ -n "${S3_SECRET_KEY:-}" ]; then
  S3_BUCKET="${S3_BUCKET:-ci-sccache-$(printf '%s' "${GITHUB_REPOSITORY_OWNER:?}" | tr '[:upper:]' '[:lower:]')}"
  if [ "$S3_BUCKET" = ci-sccache ]; then
    echo "::warning::the platform's ci-sccache bucket is not a repository cache; ignoring the S3 inputs"
  else
    backend=static
    unset AWS_SESSION_TOKEN
    export SCCACHE_BUCKET="$S3_BUCKET" SCCACHE_ENDPOINT="${S3_ENDPOINT:?}" SCCACHE_REGION=us-east-1 \
      SCCACHE_S3_KEY_PREFIX="$KEY_PREFIX" AWS_ACCESS_KEY_ID="$S3_ACCESS_KEY" AWS_SECRET_ACCESS_KEY="$S3_SECRET_KEY"
    backend_label="S3 $SCCACHE_ENDPOINT/$SCCACHE_BUCKET/$KEY_PREFIX"
  fi
fi
if [ "$backend" = none ] && ! _backend_skipped gha && [ "${ACTIONS_CACHE:-true}" != false ]; then
  backend=gha
  export SCCACHE_GHA_ENABLED=true SCCACHE_GHA_VERSION="$KEY_PREFIX"
  backend_label="GitHub Actions cache"
fi
