#!/usr/bin/env bash
# Download a pinned release; the API is a single authenticated fallback, not a retry loop.
set -euo pipefail
# Never trace credentials, even if the caller enabled xtrace.
set +x
repo=$1 tag=$2 asset=$3 asset_id=$4 sha256=$5 destination=$6
[[ "$repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]
[[ "$asset_id" =~ ^[1-9][0-9]*$ ]]
[[ "$sha256" =~ ^[a-f0-9]{64}$ ]]
part="$(mktemp "${destination}.part.XXXXXX")"
trap 'rm -f "$part"' EXIT
if ! curl --retry 3 --retry-max-time 180 --connect-timeout 20 --max-time 120 -fsSL \
  -o "$part" "https://github.com/$repo/releases/download/$tag/$asset"; then
  if [[ -z "${GITHUB_TOKEN:-}" ]]; then
    echo 'Release download failed; authenticated API fallback requires GITHUB_TOKEN' >&2
    exit 1
  fi
  # curl config is delivered on stdin, never argv or a persistent file. Reject
  # config/header delimiters rather than interpolating an arbitrary credential.
  if [[ "$GITHUB_TOKEN" == *[$'\r\n'\"\\]* ]]; then
    echo 'Invalid GITHUB_TOKEN for release API fallback' >&2
    exit 1
  fi
  printf 'header = "Authorization: Bearer %s"\n' "$GITHUB_TOKEN" |
    curl --config - --connect-timeout 20 --max-time 120 -fsSL \
      -H 'Accept: application/octet-stream' -o "$part" \
      "https://api.github.com/repos/$repo/releases/assets/$asset_id"
fi
printf '%s  %s\n' "$sha256" "$part" | sha256sum -c --quiet -
mv "$part" "$destination"
