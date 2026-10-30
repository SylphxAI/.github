import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[1] / "tools/static-publication/publish.py"
SPEC = importlib.util.spec_from_file_location("static_publication", MODULE)
p = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(p)


class Crash(BaseException):
    pass


class StaticPublication(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.root = self.directory / "served"
        self.root.mkdir()
        self.state = self.directory / "state"
        (self.root / "Version.json").write_bytes(b'old manifest\n')
        (self.root / "unrelated-session-state").write_bytes(b'keep exactly')
        self.previous = (self.root / "Version.json").read_bytes()

    def package(self, name="one", parent=None):
        package = self.directory / name
        (package / "files").mkdir(parents=True)
        data = b'exact verified artifact ' + name.encode()
        (package / "files" / "asset.bundle").write_bytes(data)
        source = {"repository": "example/game", "sha": "a" * 40, "runId": "1", "runAttempt": 1}
        files = [{"path": "asset.bundle", "bytes": len(data), "sha256": p.digest(data)}]
        subject = p.digest(p.canonical({"source": source, "files": files}))
        proof = {"kind": "actual-runtime-load", "verdict": "pass", "source": source, "subjectSha256": subject}
        raw_proof = p.canonical(proof)
        (package / "proof.json").write_bytes(raw_proof)
        pointer = ("new manifest " + name).encode()
        (package / "pointer.json").write_bytes(pointer)
        release = {"schemaVersion": 1, "source": source, "files": files, "subjectSha256": subject,
                   "proofs": {"actual-runtime-load": {"path": "proof.json", "sha256": p.digest(raw_proof)}},
                   "pointerFile": "pointer.json", "pointerSha256": p.digest(pointer),
                   "targetPointer": "Version.json", "releaseDirectory": "releases/" + name,
                   "parentPointerSha256": p.digest(parent if parent is not None else self.previous)}
        (package / "release.json").write_bytes(p.canonical(release))
        return package

    def run_publish(self, package, candidate=lambda _: None, serving=lambda _: None, checkpoint=lambda _: None):
        release = json.loads((package / "release.json").read_bytes())
        return p.publish(package, self.root, self.state, p.digest((package / "release.json").read_bytes()),
                         {"actual-runtime-load": release["proofs"]["actual-runtime-load"]["sha256"]},
                         candidate, serving, checkpoint,
                         verify_rollback=lambda r, previous: self.assertEqual((self.root / r["targetPointer"]).read_bytes(), previous))

    def test_acceptance_binds_exact_artifact_and_pointer_without_touching_other_state(self):
        package = self.package()
        calls = []
        result = self.run_publish(package, lambda r: calls.append(("candidate", (self.root / "Version.json").read_bytes())),
                                  lambda r: calls.append(("serving", (self.root / "Version.json").read_bytes())))
        self.assertEqual(result["state"], "ACCEPTED")
        self.assertEqual(calls, [("candidate", self.previous), ("serving", b"new manifest one")])
        self.assertEqual((self.root / "unrelated-session-state").read_bytes(), b"keep exactly")
        self.assertEqual((self.root / "releases/one/asset.bundle").read_bytes(), (package / "files/asset.bundle").read_bytes())

    def test_corrupt_artifact_cannot_change_pointer(self):
        package = self.package()
        (package / "files/asset.bundle").write_bytes(b'corrupt')
        with self.assertRaisesRegex(p.Refused, "Artifact bytes"):
            self.run_publish(package)
        self.assertEqual((self.root / "Version.json").read_bytes(), self.previous)
        self.assertFalse((self.root / "releases").exists())

    def test_serving_files_and_directories_are_readable_under_a_private_umask(self):
        package = self.package()
        original_umask = os.umask(0o077)
        try:
            self.run_publish(package)
        finally:
            os.umask(original_umask)
        for directory in [self.root / "releases", self.root / "releases/one"]:
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((self.root / "releases/one/asset.bundle").stat().st_mode), 0o644)
        for receipt in self.state.joinpath("receipts").glob("*"):
            self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o600)

    def test_pointer_permissions_survive_activation_and_rollback(self):
        target = self.root / "Version.json"
        target.chmod(0o640)
        def serving(_):
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
            raise RuntimeError("serving failure")
        with self.assertRaisesRegex(RuntimeError, "serving failure"):
            self.run_publish(self.package(), serving=serving)
        self.assertEqual(target.read_bytes(), self.previous)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)

    def test_missing_or_refused_load_proof_cannot_change_pointer(self):
        for verdict in ("fail", "missing"):
            package = self.package(verdict)
            if verdict == "missing":
                (package / "proof.json").unlink()
            else:
                (package / "proof.json").write_bytes(b'failed proof')
            with self.assertRaises((p.Refused, FileNotFoundError)):
                self.run_publish(package)
        self.assertEqual((self.root / "Version.json").read_bytes(), self.previous)

    def test_failed_candidate_proof_leaves_pointer_unchanged(self):
        def failure(_):
            raise RuntimeError("candidate load refused")
        with self.assertRaisesRegex(RuntimeError, "refused"):
            self.run_publish(self.package(), candidate=failure)
        self.assertEqual((self.root / "Version.json").read_bytes(), self.previous)

    def test_failed_serving_proof_restores_exact_previous_manifest(self):
        def failure(_):
            raise RuntimeError("public bytes differ")
        package = self.package()
        with self.assertRaisesRegex(RuntimeError, "differ"):
            self.run_publish(package, serving=failure)
        self.assertEqual((self.root / "Version.json").read_bytes(), self.previous)
        with self.assertRaisesRegex(p.Refused, "terminal"):
            self.run_publish(package)

    def test_stale_candidate_refuses_without_overwriting_newer_publication(self):
        first, stale = self.package("first"), self.package("stale")
        self.run_publish(first)
        with self.assertRaisesRegex(p.Refused, "Stale publication"):
            self.run_publish(stale)
        self.assertEqual((self.root / "Version.json").read_bytes(), b"new manifest first")

    def test_overtaken_candidate_refuses_after_prepublication_check(self):
        first, second = self.package("first"), self.package("second")
        with self.assertRaisesRegex(p.Refused, "changed while"):
            self.run_publish(first, candidate=lambda _: self.run_publish(second))
        self.assertEqual((self.root / "Version.json").read_bytes(), b"new manifest second")

    def test_old_failed_verification_cannot_rollback_newer_publication(self):
        first = self.package("first")
        def overtaken(_):
            second = self.package("second", parent=b"new manifest first")
            self.run_publish(second)
            raise RuntimeError("old check failed")
        with self.assertRaisesRegex(RuntimeError, "old check failed"):
            self.run_publish(first, serving=overtaken)
        self.assertEqual((self.root / "Version.json").read_bytes(), b"new manifest second")

    def test_crash_after_pointer_switch_reconciles_same_operation(self):
        package = self.package()
        def crash(state):
            if state == "POINTER_REPLACED":
                raise Crash()
        with self.assertRaises(Crash):
            self.run_publish(package, checkpoint=crash)
        self.assertEqual((self.root / "Version.json").read_bytes(), b"new manifest one")
        result = self.run_publish(package)
        self.assertEqual(result["state"], "ACCEPTED")
        self.assertEqual(len(list((self.state / "receipts").glob("*"))), 1)

    def test_crash_before_pointer_switch_resumes_without_duplicate_release(self):
        package = self.package()
        def crash(state):
            if state == "SWITCHING":
                raise Crash()
        with self.assertRaises(Crash):
            self.run_publish(package, checkpoint=crash)
        self.assertEqual((self.root / "Version.json").read_bytes(), self.previous)
        self.assertEqual(self.run_publish(package)["state"], "ACCEPTED")

    def test_changed_immutable_release_is_never_overwritten(self):
        package = self.package()
        self.run_publish(package)
        (self.root / "releases/one/asset.bundle").write_bytes(b"changed")
        with self.assertRaisesRegex(p.Refused, "immutable release differs"):
            self.run_publish(package)
        self.assertEqual((self.root / "releases/one/asset.bundle").read_bytes(), b"changed")

    def test_path_traversal_and_symlink_are_rejected(self):
        for name in ("../outside", "/absolute", "./relative", "a//b", "a\\b"):
            with self.assertRaises(p.Refused):
                p.relative(name)
        package = self.package()
        file = package / "files/asset.bundle"
        file.unlink()
        file.symlink_to(self.root / "unrelated-session-state")
        with self.assertRaisesRegex(p.Refused, "Symlink"):
            self.run_publish(package)

    def test_crash_after_rollback_switch_reconciles_rollback_without_republishing(self):
        package = self.package()
        def failure(_):
            raise RuntimeError("serving refused")
        def crash(state):
            if state == "ROLLBACK_SWITCHED":
                raise Crash()
        with self.assertRaises(Crash):
            self.run_publish(package, serving=failure, checkpoint=crash)
        self.assertEqual((self.root / "Version.json").read_bytes(), self.previous)
        with self.assertRaisesRegex(p.Refused, "rollback reconciled"):
            self.run_publish(package)
        self.assertEqual((self.root / "Version.json").read_bytes(), self.previous)


if __name__ == "__main__":
    unittest.main()
