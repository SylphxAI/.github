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
      lifecycle='<LifecycleConfiguration><Rule><ID>expire</ID><Filter><Prefix></Prefix></Filter><Status>Enabled</Status><Expiration><Days>14</Days></Expiration></Rule></LifecycleConfiguration>'
      md5="$(printf '%s' "$lifecycle" | openssl dgst -md5 -binary | base64)"
      curl -sS --max-time 10 "${sign[@]}" -X PUT -H "Content-MD5: $md5" --data "$lifecycle" "$bucket_url?lifecycle" -o /dev/null || true
    fi
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
