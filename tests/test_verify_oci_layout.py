"""verify-oci-layout.py: a truncated or partial image transfer fails loudly."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("verify_oci", ROOT / "scripts/verify-oci-layout.py")
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


def write_blob(root: Path, data: bytes) -> dict:
    digest = hashlib.sha256(data).hexdigest()
    (root / "blobs" / "sha256").mkdir(parents=True, exist_ok=True)
    (root / "blobs" / "sha256" / digest).write_bytes(data)
    return {"digest": f"sha256:{digest}", "size": len(data)}


def layout(root: Path) -> tuple[dict, dict]:
    layer = write_blob(root, b"layer" * 1000)
    config = write_blob(root, b'{"config":1}')
    manifest = json.dumps({
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {**config, "mediaType": "application/vnd.oci.image.config.v1+json"},
        "layers": [{**layer, "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip"}],
    }).encode()
    m = write_blob(root, manifest)
    (root / "index.json").write_text(json.dumps({
        "schemaVersion": 2,
        "manifests": [{**m, "mediaType": "application/vnd.oci.image.manifest.v1+json"}],
    }))
    (root / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}')
    return layer, m


class VerifyOciLayoutTests(unittest.TestCase):
    def run_on(self, root: Path) -> int:
        import sys
        argv = sys.argv
        sys.argv = ["verify-oci-layout.py", str(root)]
        try:
            return verify.main()
        finally:
            sys.argv = argv

    def test_a_complete_layout_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout(Path(tmp))
            self.assertEqual(self.run_on(Path(tmp)), 0)

    def test_truncated_missing_or_empty_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            layer, _ = layout(root)
            blob = root / "blobs" / "sha256" / layer["digest"].split(":")[1]
            blob.write_bytes(blob.read_bytes()[:10])
            self.assertEqual(self.run_on(root), 1)
            blob.unlink()
            self.assertEqual(self.run_on(root), 1)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.run_on(Path(tmp)), 1)


if __name__ == "__main__":
    unittest.main()
