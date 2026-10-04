#!/usr/bin/env bash
# Creates the static backend's bucket (14-day expiry) when it is missing, and
# says plainly what the object store answered when it cannot. Only HTTP status
# codes and the S3 error code are ever printed, never a credential.
#
# In: SCCACHE_ENDPOINT, SCCACHE_BUCKET, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY.
# Exit 0: the bucket exists (or already existed). Exit 1: it cannot be created.
set -uo pipefail

bucket_url="${SCCACHE_ENDPOINT%/}/$SCCACHE_BUCKET"
sign=(--aws-sigv4 "aws:amz:us-east-1:s3" --user "$AWS_ACCESS_KEY_ID:$AWS_SECRET_ACCESS_KEY")
body="$(mktemp)"
trap 'rm -f "$body"' EXIT

# s3 <curl args...>: the HTTP status code (000 when the request itself failed);
# the response body lands in $body.
s3() {
  curl -sS --max-time 10 -o "$body" -w '%{http_code}' "${sign[@]}" "$@" 2>/dev/null || true
}
s3_error() { grep -oE '<Code>[^<]*</Code>' "$body" 2>/dev/null | head -n1 | sed -E 's#</?Code>##g'; }

code="$(s3 -I "$bucket_url")"
[ "$code" = 200 ] && exit 0

code="$(s3 -X PUT "$bucket_url")"
case "$code" in
  2??) ;;
  409) echo "::notice::bucket $SCCACHE_BUCKET already exists (HTTP 409 $(s3_error))" ;;
  *)
    err="$(s3_error)"
    echo "::error::cannot create the compile cache bucket $SCCACHE_BUCKET: HTTP ${code:-000} ${err:-no S3 error code}. Every cache write will fail until the bucket exists and its credential may write to it."
    exit 1
    ;;
esac

lifecycle='<LifecycleConfiguration><Rule><ID>expire</ID><Filter><Prefix></Prefix></Filter><Status>Enabled</Status><Expiration><Days>14</Days></Expiration></Rule></LifecycleConfiguration>'
md5="$(printf '%s' "$lifecycle" | openssl dgst -md5 -binary | base64)"
code="$(s3 -X PUT -H "Content-MD5: $md5" --data "$lifecycle" "$bucket_url?lifecycle")"
case "$code" in
  2??) ;;
  *) echo "::warning::bucket $SCCACHE_BUCKET has no 14-day expiry (lifecycle PUT: HTTP ${code:-000} $(s3_error)); the cache will grow unbounded" ;;
esac
