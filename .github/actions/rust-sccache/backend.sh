# shellcheck shell=bash disable=SC2034  # sourced: the caller reads backend and backend_label
# Sourced by the rust-sccache action (and its tests): chooses the sccache
# backend, first that applies, and exports its settings to this shell only.
#
#   1. org     the per-org credential Hands injects into every Sylphx runner
#              (SYLPHX_SCCACHE_*: short-lived, scoped to
#              sccache/<installation id>/ of the platform cache bucket)
#   2. static  the organization's own object-store user, passed as the
#              s3-access-key / s3-secret-key inputs (SYLPHX_CI_CACHE_*)
#   3. gha     the repository's GitHub Actions cache, unless ACTIONS_CACHE is
#              false (the caller then brings its own fallback)
#   4. none
#
# The platform-wide `ci-sccache` bucket is never a backend here: it belongs to
# the platform's own CI, and a repository never holds its key.
#
# In: SYLPHX_SCCACHE_*, S3_ACCESS_KEY, S3_SECRET_KEY, S3_ENDPOINT, S3_BUCKET,
#     KEY_PREFIX, ACTIONS_CACHE, GITHUB_REPOSITORY_OWNER.
# Out: backend (org|static|gha|none), backend_label, and the SCCACHE_* and
#      AWS_* variables sccache reads.

backend=none
backend_label="no cache"
if [ -n "${SYLPHX_SCCACHE_ACCESS_KEY_ID:-}" ] && [ -n "${SYLPHX_SCCACHE_SECRET_ACCESS_KEY:-}" ] \
  && [ -n "${SYLPHX_SCCACHE_SESSION_TOKEN:-}" ] && [ -n "${SYLPHX_SCCACHE_BUCKET:-}" ] \
  && [ -n "${SYLPHX_SCCACHE_ENDPOINT:-}" ] && [ -n "${SYLPHX_SCCACHE_KEY_PREFIX:-}" ]; then
  backend=org
  export SCCACHE_BUCKET="$SYLPHX_SCCACHE_BUCKET" SCCACHE_ENDPOINT="$SYLPHX_SCCACHE_ENDPOINT" \
    SCCACHE_REGION="${SYLPHX_SCCACHE_REGION:-us-east-1}" \
    SCCACHE_S3_KEY_PREFIX="$SYLPHX_SCCACHE_KEY_PREFIX/$KEY_PREFIX" \
    AWS_ACCESS_KEY_ID="$SYLPHX_SCCACHE_ACCESS_KEY_ID" AWS_SECRET_ACCESS_KEY="$SYLPHX_SCCACHE_SECRET_ACCESS_KEY" \
    AWS_SESSION_TOKEN="$SYLPHX_SCCACHE_SESSION_TOKEN"
  backend_label="S3 $SCCACHE_ENDPOINT/$SCCACHE_BUCKET/$SCCACHE_S3_KEY_PREFIX (org credential until ${SYLPHX_SCCACHE_EXPIRES:-?})"
elif [ -n "${S3_ACCESS_KEY:-}" ] && [ -n "${S3_SECRET_KEY:-}" ]; then
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
if [ "$backend" = none ] && [ "${ACTIONS_CACHE:-true}" != false ]; then
  backend=gha
  export SCCACHE_GHA_ENABLED=true SCCACHE_GHA_VERSION="$KEY_PREFIX"
  backend_label="GitHub Actions cache"
fi
