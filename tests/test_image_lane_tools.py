#!/usr/bin/env python3
"""Unit tests for the image lane's credential and evidence tooling.

These are pure-policy tests: no cluster, no registry, no network. They pin the
exact-grant contract (one repository, pull+push, bounded lifetime) that makes
the lane's credential a repository-scoped identity rather than a broad one.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LANE_IMAGE = "registry.sylphx.com/library/sylphx-hands"


def load(module_name: str, relative: str):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


mint = load("image_lane_mint", "scripts/mint-registry-auth.py")
pack = load("image_lane_pack", "scripts/pack-image-evidence.py")


def jwt(claims: dict) -> str:
    def segment(value: dict) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{segment({'alg': 'ES256'})}.{segment(claims)}.signature"


class SplitImageReferenceTests(unittest.TestCase):
    def test_splits_host_and_repository(self) -> None:
        self.assertEqual(
            mint.split_image_reference(LANE_IMAGE),
            ("registry.sylphx.com", "library/sylphx-hands"),
        )

    def test_rejects_non_canonical_references(self) -> None:
        for bad in [
            "library/sylphx-hands",
            "registry.sylphx.com/",
            "registry.sylphx.com//x",
            "registry.sylphx.com/../x",
            "registry.sylphx.com/x/../y",
            "https://registry.sylphx.com/library/x",
            "registry.sylphx.com/library/x@sha256:" + "0" * 64,
        ]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                mint.split_image_reference(bad)


class RegistryTokenValidationTests(unittest.TestCase):
    def claims(self, grants, iat=None, exp=None, aud=None):
        now = int(time.time())
        return {
            "aud": aud or [mint.REGISTRY_TOKEN_SERVICE],
            "iat": now if iat is None else iat,
            "exp": now + 300 if exp is None else exp,
            "access": grants,
        }

    def test_accepts_exactly_one_repository_grant(self) -> None:
        token = jwt(self.claims([{"type": "repository", "name": "library/sylphx-hands", "actions": ["pull", "push"]}]))
        mint._validate_registry_token(token, int(time.time()), "library/sylphx-hands")

    def test_rejects_a_grant_for_another_repository(self) -> None:
        token = jwt(self.claims([{"type": "repository", "name": "library/other", "actions": ["pull", "push"]}]))
        with self.assertRaises(ValueError):
            mint._validate_registry_token(token, int(time.time()), "library/sylphx-hands")

    def test_rejects_authority_outside_the_requested_repository(self) -> None:
        token = jwt(
            self.claims(
                [
                    {"type": "repository", "name": "library/sylphx-hands", "actions": ["pull", "push"]},
                    {"type": "repository", "name": "library/other", "actions": ["pull", "push"]},
                ]
            )
        )
        with self.assertRaises(ValueError):
            mint._validate_registry_token(token, int(time.time()), "library/sylphx-hands")

    def test_rejects_wide_actions(self) -> None:
        token = jwt(self.claims([{"type": "repository", "name": "library/sylphx-hands", "actions": ["pull", "push", "delete"]}]))
        with self.assertRaises(ValueError):
            mint._validate_registry_token(token, int(time.time()), "library/sylphx-hands")

    def test_rejects_an_expired_token(self) -> None:
        now = int(time.time())
        token = jwt(self.claims([{"type": "repository", "name": "library/sylphx-hands", "actions": ["pull", "push"]}], iat=now - 1200, exp=now - 300))
        with self.assertRaises(ValueError):
            mint._validate_registry_token(token, now, "library/sylphx-hands")

    def test_rejects_an_overlong_token_lifetime(self) -> None:
        now = int(time.time())
        token = jwt(self.claims([{"type": "repository", "name": "library/sylphx-hands", "actions": ["pull", "push"]}], iat=now, exp=now + 4000))
        with self.assertRaises(ValueError):
            mint._validate_registry_token(token, now, "library/sylphx-hands")

    def test_rejects_a_foreign_audience(self) -> None:
        token = jwt(self.claims([{"type": "repository", "name": "library/sylphx-hands", "actions": ["pull", "push"]}], aud=["something-else"]))
        with self.assertRaises(ValueError):
            mint._validate_registry_token(token, int(time.time()), "library/sylphx-hands")


class SvidValidationTests(unittest.TestCase):
    def test_accepts_the_publisher_subject_and_audience(self) -> None:
        now = int(time.time())
        token = jwt({"sub": mint.SPIFFE_ID, "aud": [mint.SPIFFE_AUDIENCE], "iat": now, "exp": now + 300})
        mint._validate_svid(token, now)

    def test_rejects_another_subject(self) -> None:
        now = int(time.time())
        token = jwt({"sub": "spiffe://sylphx.local/role/other", "aud": [mint.SPIFFE_AUDIENCE], "iat": now, "exp": now + 300})
        with self.assertRaises(ValueError):
            mint._validate_svid(token, now)


class EvidenceDocumentTests(unittest.TestCase):
    def build(self, tmp: Path) -> dict:
        oci = tmp / "image-oci"
        oci.mkdir(parents=True, exist_ok=True)
        (oci / "index.json").write_bytes(b'{"schemaVersion":2,"manifests":[]}\n')
        evidence = tmp / "evidence"
        evidence.mkdir(parents=True, exist_ok=True)
        environ = {
            "IMAGE": LANE_IMAGE,
            "IMAGE_TAG": "abcdef12",
            "PLATFORMS": "linux/amd64",
            "SOURCE_SHA": "a" * 40,
            "OCI_DIR": str(oci),
            "BUILD_EVIDENCE_DIR": str(evidence),
            "GITHUB_REPOSITORY": "SylphxAI/hands",
            "GITHUB_RUN_ID": "123",
            "GITHUB_RUN_ATTEMPT": "2",
        }
        return pack.build_document(environ)

    def test_binds_the_local_index_digest_and_source_revision(self) -> None:
        import hashlib
        import tempfile

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document = self.build(root)
            expected = "sha256:" + hashlib.sha256(b'{"schemaVersion":2,"manifests":[]}\n').hexdigest()
            self.assertEqual(document["ociIndexDigest"], expected)
            self.assertEqual(document["schema"], pack.SCHEMA)
            self.assertEqual(document["sourceSha"], "a" * 40)
            self.assertEqual(document["sourceRepository"], "SylphxAI/hands")
            self.assertEqual(document["platforms"], ["linux/amd64"])

    def test_missing_oci_layout_fails_closed(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            oci = root / "image-oci"
            oci.mkdir()
            with self.assertRaises(SystemExit):
                pack.index_digest(oci)


class PublishShapeTests(unittest.TestCase):
    """The lane must publish an INDEX, and a flattened push must fail closed.

    `containers/image` resolves `oci:DIR` to the entry the layout root
    `index.json` names, so a root that names the platform MANIFEST resolves to
    that bare manifest and no flag can preserve an index
    (`oci/layout/oci_transport.go::getManifestDescriptor`). Landing a bare
    manifest is invisible to index-resolving consumers: the Hands snapshot-key
    probe reads an index only when the runtime reported a per-platform child
    digest (so only if the registry served an index), its receipt then carries
    no `indexManifest`, and `apps_delivery.resolve_bootstrap_artifact` returns
    NULL on its first line -- refusing every environment's first Knative Service
    create (measured 2026-09-18: the Cubeage promote could not settle).

    So the lane wraps a one-platform layout in an index that names the platform
    manifest as its child, and the readback refuses a flattened push.
    """

    def setUp(self) -> None:
        self.workflow = (ROOT / ".github/workflows/image-lane.yml").read_text()

    def _run_block(self, marker: str) -> str:
        start = self.workflow.index(marker)
        end = self.workflow.index("\n          PY\n", start)
        body = self.workflow[start:end]
        return "\n".join(
            line[10:] if line.startswith(" " * 10) else line
            for line in body.splitlines()
        )

    def test_the_push_wraps_a_one_platform_layout_in_an_index(self) -> None:
        block = self._run_block("python3 - \"${OCI_DIR}\" <<'PY'")
        self.assertIn("application/vnd.oci.image.index.v1+json", block)
        self.assertIn("index_wrapper_added", block)
        # A genuine multi-platform layout must be left alone.
        self.assertIn("index_wrapper_skipped", block)
        self.assertIn("len(images) == 1", block)

    def test_the_readback_refuses_a_flattened_push(self) -> None:
        block = self._run_block(
            "python3 - \"${READBACK_DIR}/image.json\" \"${OCI_DIR}\" <<'PY'"
        )
        self.assertIn("registry served a single manifest where the lane pushed an index", block)
        # The old tolerated branch must be gone.
        self.assertNotIn("readback_ok single-manifest", self.workflow)

    def test_the_readback_follows_the_wrapper_to_the_platform_manifests(self) -> None:
        # After wrapping, the layout root names the wrapper, not the platform
        # manifests, so the local side must resolve one level or a correct push
        # would FATAL on a digest mismatch.
        block = self._run_block(
            "python3 - \"${READBACK_DIR}/image.json\" \"${OCI_DIR}\" <<'PY'"
        )
        self.assertIn("def resolve_entry", block)
        self.assertIn("local_entry = resolve_entry(local_index, root_dir)", block)


if __name__ == "__main__":
    unittest.main()
