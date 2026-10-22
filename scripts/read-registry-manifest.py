#!/usr/bin/env python3
"""Read the registry-selected manifest identity with the publisher's scoped JWT.

HEAD uses the OCI/Docker index and manifest types containerd accepts. Never
promote a hash of skopeo's separately negotiated representation instead.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "registry_manifest_mint", Path(__file__).with_name("mint-registry-auth.py")
)
_mint = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_mint)

MANIFEST_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
)
ACCEPT = ",".join(MANIFEST_TYPES)
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
REFERENCE = re.compile(r"(?:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}|sha256:[0-9a-f]{64})\Z")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def publisher_token(auth_file: Path, host: str) -> str:
    record = json.loads(auth_file.read_text())["auths"][host]
    # mint-registry-auth.py writes this exact skopeo form; no static credential
    # fallback or ambient Docker credential lookup is part of this readback.
    user, separator, token = base64.b64decode(record["auth"], validate=True).decode().partition(":")
    if user != "oauth2accesstoken" or not separator or not token:
        raise ValueError("publisher auth file does not contain its scoped registry JWT")
    return token


def read_manifest(image: str, reference: str, auth_file: Path, output: Path) -> str:
    host, repository = _mint.split_image_reference(image)
    if not REFERENCE.fullmatch(reference):
        raise ValueError("manifest reference is not a tag or SHA-256 digest")
    token = publisher_token(auth_file, host)
    base = f"https://{host}/v2/{urllib.parse.quote(repository, safe='/')}/manifests/"
    headers = {"Authorization": f"Bearer {token}", "Accept": ACCEPT}
    opener = urllib.request.build_opener(NoRedirect())
    tag_url = base + urllib.parse.quote(reference, safe=":")
    with opener.open(urllib.request.Request(tag_url, headers=headers, method="HEAD"), timeout=20) as response:
        digest = response.headers.get("Docker-Content-Digest", "")
        media_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip()
        if response.geturl() != tag_url or not DIGEST.fullmatch(digest):
            raise ValueError("manifest HEAD omitted a canonical registry digest or changed authority")
        if media_type not in MANIFEST_TYPES:
            raise ValueError("manifest HEAD selected an unsupported media type")
    digest_url = base + digest
    with opener.open(urllib.request.Request(digest_url, headers=headers), timeout=20) as response:
        if response.geturl() != digest_url or response.headers.get("Docker-Content-Digest") != digest:
            raise ValueError("digest GET did not return the HEAD-selected manifest identity")
        if response.headers.get("Content-Type", "").split(";", 1)[0].strip() != media_type:
            raise ValueError("digest GET changed the HEAD-selected media type")
        body = response.read()
    if "sha256:" + hashlib.sha256(body).hexdigest() != digest:
        raise ValueError("registry manifest bytes do not match its advertised digest")
    json.loads(body)
    output.write_bytes(body)
    return digest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--auth-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        digest = read_manifest(args.image, args.reference, args.auth_file, args.output)
    except urllib.error.HTTPError as error:
        print(f"registry manifest readback refused: HTTP {error.code}", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError):
        # Neither an unexpected response body nor credential contents belong
        # in logs, including on auth/transport/parse failure.
        print("registry manifest readback failed identity, content or transport validation", file=sys.stderr)
        return 1
    print(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
