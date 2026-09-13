#!/usr/bin/env python3
"""Write the default evidence document for the image lane.

The document binds the exact locally built OCI index to the source revision,
the workflow attempt, and the build host. It is deliberately data-only: it
claims no signature, no attestation, and no builder trust. The caller's
promotion boundary owns any stronger verification.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
from pathlib import Path

SCHEMA = "sylphx.image-lane.evidence/v1"
REQUIRED_ENV = ("IMAGE", "IMAGE_TAG", "PLATFORMS", "SOURCE_SHA", "OCI_DIR", "BUILD_EVIDENCE_DIR")


def index_digest(oci_dir: Path) -> str:
    try:
        body = (oci_dir / "index.json").read_bytes()
    except OSError as error:
        raise SystemExit(f"evidence requires the built OCI layout: {error}") from error
    return "sha256:" + hashlib.sha256(body).hexdigest()


def build_document(environ: dict[str, str]) -> dict[str, object]:
    oci_dir = Path(environ["OCI_DIR"])
    return {
        "schema": SCHEMA,
        "image": environ["IMAGE"],
        "tag": environ["IMAGE_TAG"],
        "platforms": [item for item in environ["PLATFORMS"].split(",") if item],
        "sourceSha": environ["SOURCE_SHA"],
        "sourceRepository": environ.get("GITHUB_REPOSITORY", ""),
        "workflowRunId": environ.get("GITHUB_RUN_ID", ""),
        "workflowRunAttempt": environ.get("GITHUB_RUN_ATTEMPT", ""),
        "ociIndexDigest": index_digest(oci_dir),
        "builder": {
            "host": platform.platform(),
            "machine": platform.machine(),
        },
    }


def main() -> int:
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        raise SystemExit(f"evidence environment incomplete: {', '.join(missing)}")
    document = build_document(dict(os.environ))
    target = Path(os.environ["BUILD_EVIDENCE_DIR"]) / "evidence.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"evidence_pack_ok file={target} index_digest={document['ociIndexDigest']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
