# shellcheck shell=bash disable=SC2154  # sourced: backend and backend_label come from backend.sh
# Sourced by the rust-sccache action after sccache is installed: pick the
# backend (backend.sh), start the sccache server on it, and when the server will
# not start on that backend, try the next one (buildcache, static, gha, none).
#
# The settings go to the sccache server through this shell's environment. Only
# what a later step needs ($GITHUB_ENV: the wrapper and the static and Actions
# cache settings) is written after the server is up, and BuildCache's run token
# never is: it lives in the server and nowhere else.
#
# In: backend.sh's inputs, GITHUB_ACTION_PATH, GITHUB_ENV, GITHUB_OUTPUT,
#     GITHUB_STEP_SUMMARY, VERSION.

skip=""
while :; do
  export SKIP_BACKENDS="$skip"
  # shellcheck source=backend.sh
  . "${GITHUB_ACTION_PATH:?}/backend.sh"
  if [ "$backend" = none ]; then
    echo "backend=none" >> "$GITHUB_OUTPUT"
    echo "No sccache backend; compiling without it"
    echo "Rust compile cache backend: none" >> "${GITHUB_STEP_SUMMARY:-/dev/null}"
    break
  fi
  if [ "$backend" = static ]; then
    bucket_url="${SCCACHE_ENDPOINT%/}/$SCCACHE_BUCKET"
    sign=(--aws-sigv4 "aws:amz:us-east-1:s3" --user "$AWS_ACCESS_KEY_ID:$AWS_SECRET_ACCESS_KEY")
    if ! curl -sS -o /dev/null --max-time 10 -w '%{http_code}' "${sign[@]}" -I "$bucket_url" | grep -q '^200$'; then
      curl -sS --max-time 10 "${sign[@]}" -X PUT "$bucket_url" -o /dev/null || true
    fi
    # Expiry, applied on every run so a bucket made earlier gets it too (a
    # bucket that was made before the one-namespace prefix kept objects under
    # `<owner>/<repo>/` that no run reads again, filled the user's quota, and
    # RGW then refused every cache write with 403 QuotaExceeded): the cache
    # namespace expires after 14 days, the retired `<owner>/` namespace after 1.
    lifecycle="<LifecycleConfiguration><Rule><ID>expire</ID><Filter><Prefix>${SCCACHE_S3_KEY_PREFIX}/</Prefix></Filter><Status>Enabled</Status><Expiration><Days>14</Days></Expiration></Rule>"
    if [ -n "${GITHUB_REPOSITORY_OWNER:-}" ] && [ "${GITHUB_REPOSITORY_OWNER}" != "$SCCACHE_S3_KEY_PREFIX" ]; then
      lifecycle="$lifecycle<Rule><ID>expire-retired-repo-prefix</ID><Filter><Prefix>${GITHUB_REPOSITORY_OWNER}/</Prefix></Filter><Status>Enabled</Status><Expiration><Days>1</Days></Expiration></Rule>"
    fi
    lifecycle="$lifecycle</LifecycleConfiguration>"
    md5="$(printf '%s' "$lifecycle" | openssl dgst -md5 -binary | base64)"
    curl -sS --max-time 10 "${sign[@]}" -X PUT -H "Content-MD5: $md5" --data "$lifecycle" "$bucket_url?lifecycle" -o /dev/null || true
  fi
  export SCCACHE_IDLE_TIMEOUT=0
  if SCCACHE_LOG=warn sccache --start-server; then
    case "$backend" in
      static)
        {
          echo "SCCACHE_BUCKET=$SCCACHE_BUCKET"
          echo "SCCACHE_ENDPOINT=$SCCACHE_ENDPOINT"
          echo "SCCACHE_REGION=us-east-1"
          echo "SCCACHE_S3_KEY_PREFIX=$SCCACHE_S3_KEY_PREFIX"
          echo "AWS_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID"
          echo "AWS_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY"
        } >> "$GITHUB_ENV"
        ;;
      gha)
        echo "SCCACHE_GHA_ENABLED=true" >> "$GITHUB_ENV"
        echo "SCCACHE_GHA_VERSION=$KEY_PREFIX" >> "$GITHUB_ENV"
        ;;
    esac
    echo "RUSTC_WRAPPER=sccache" >> "$GITHUB_ENV"
    echo "backend=$backend" >> "$GITHUB_OUTPUT"
    echo "Rust compile cache backend: $backend" >> "${GITHUB_STEP_SUMMARY:-/dev/null}"
    echo "sccache ${VERSION:-} on $backend_label"
    break
  fi
  echo "::warning::sccache could not start on $backend_label; trying the next cache backend"
  sccache --stop-server > /dev/null 2>&1 || true
  skip="$skip $backend"
  # The failed backend's settings go with it; AWS_* are the static backend's.
  [ "$backend" != static ] || unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
done
