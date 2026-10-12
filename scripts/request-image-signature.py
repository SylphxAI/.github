#!/usr/bin/env python3
"""Ask the platform signer to sign the digest this lane just published.

The lane's publish job pushes `IMAGE:IMAGE_TAG`, reads back its authoritative
digest, then runs this. It sends the job's GitHub Actions OIDC token (the same
registry audience the push used) and `IMAGE@REGISTRY_DIGEST` to the Sylphx
api's publisher door (`POST /v1/registry/signatures`, SylphxAI/cloud
`artifact_signing.rs`). The api signs only when the registry token service
admits this job as a publisher of the image's repository and the digest is in
that repository; the signature lands in the cosign layout
(`<repo>:sha256-<hex>.sig`) under the `artifact-signing` key that Kyverno's
verify-on-deploy policy checks.

Never prints the token. Transient failures (connect, timeout, HTTP 5xx) are
retried; a 4xx is the door's verdict and fails at once.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SIGNATURE_URL = "https://api.sylphx.com/v1/registry/signatures"
DEFAULT_TIMEOUT_SECONDS = 30
ATTEMPTS = 5
BACKOFF_SECONDS = (5.0, 10.0, 20.0, 30.0)
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def _mint_module():
    path = Path(__file__).resolve().parent / "mint-registry-auth.py"
    spec = importlib.util.spec_from_file_location("image_lane_mint", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def signature_request(image: str, digest: str, token: str) -> urllib.request.Request:
    if _DIGEST.fullmatch(digest) is None:
        raise ValueError("digest must be sha256:<64 lowercase hex>")
    if "@" in image or ":" in image.rsplit("/", 1)[-1]:
        raise ValueError("image must be host/repository with no tag or digest")
    body = json.dumps({"image": f"{image}@{digest}"}).encode()
    return urllib.request.Request(
        SIGNATURE_URL,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )


def _verdict(error: urllib.error.HTTPError) -> str:
    try:
        payload = json.loads(error.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, OSError):
        return f"HTTP {error.code}"
    if not isinstance(payload, dict):
        return f"HTTP {error.code}"
    return f"HTTP {error.code} {payload.get('error', '')}: {payload.get('detail', '')}".strip()


def request_signature(request: urllib.request.Request, timeout_seconds: float) -> dict:
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                if response.geturl() != SIGNATURE_URL:
                    raise RuntimeError("the signer redirected away from its fixed authority")
                payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict) or not isinstance(payload.get("signature"), str):
                raise RuntimeError("the signer's response names no signature")
            return payload
        except urllib.error.HTTPError as error:
            if error.code < 500:
                raise RuntimeError(f"the signer refused: {_verdict(error)}") from error
            reason = _verdict(error)
            failure: Exception = error
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            reason = type(getattr(error, "reason", error)).__name__
            failure = error
        if attempt == ATTEMPTS:
            raise RuntimeError(f"the signer is unavailable after {ATTEMPTS} attempts ({reason})") from failure
        delay = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS)) - 1]
        print(f"signature attempt {attempt}/{ATTEMPTS} failed ({reason}); retrying in {delay:.0f}s", file=sys.stderr)
        time.sleep(delay)
    raise AssertionError("unreachable")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True, help="registry host + repository, e.g. registry.sylphx.com/library/name")
    parser.add_argument("--digest", required=True, help="the published digest the readback returned")
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args()
    token = _mint_module()._fetch_github_oidc_token(args.timeout_seconds)
    payload = request_signature(signature_request(args.image, args.digest, token), args.timeout_seconds)
    print(f"signed image={args.image}@{args.digest} signature={payload['signature']} key={payload.get('signing_key', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
