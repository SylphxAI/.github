#!/usr/bin/env python3
"""Publish an already-proven static release on a mounted filesystem.

The authenticated deployment adapter supplies the expected package/proof hashes.
This mechanism neither grants workload authority nor accepts a caller's bare
`passed` flag as proof. Product manifests and load semantics stay with callers.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile
import re


class Refused(ValueError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def relative(value):
    path = PurePosixPath(value)
    if not value or path.is_absolute() or any(part in ("", ".", "..") for part in value.split("/")) or "\\" in value:
        raise Refused("Unsafe relative path")
    return path


def within(root, name):
    parts = relative(name).parts
    path = root.joinpath(*parts)
    if any(parent.is_symlink() for parent in [path, *path.parents] if parent != root.parent):
        raise Refused("Symlink in publication path")
    if not path.resolve().is_relative_to(root.resolve()):
        raise Refused("Path escapes publication root")
    return path


def durable_write(path, data, mode=0o600, owner=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, filename = tempfile.mkstemp(prefix=".publication-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            if owner is not None:
                current = os.fstat(stream.fileno())
                if (current.st_uid, current.st_gid) != tuple(owner):
                    os.fchown(stream.fileno(), *owner)
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(filename, path)
        sync_dir(path.parent)
    finally:
        if os.path.exists(filename):
            os.unlink(filename)


def sync_dir(path):
    descriptor = os.open(path, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def public_directories(path, root):
    """Give only newly created serving directories ordinary web read access."""
    missing = []
    while path != root:
        if not path.exists():
            missing.append(path)
        path = path.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o755)
        except FileExistsError:
            continue
        directory.chmod(0o755)  # A private executor umask must not hide assets.


@contextmanager
def transaction(state_root, pointer_name):
    # A short transaction on one publication pointer. This does not reserve any
    # repository or prevent independent source writers or independent targets.
    state_root.mkdir(parents=True, exist_ok=True)
    with (state_root / (digest(pointer_name.encode()) + ".transaction")).open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def validate_package(package, expected_package_sha, required_proofs):
    raw = (package / "release.json").read_bytes()
    if digest(raw) != expected_package_sha:
        raise Refused("Package identity mismatch")
    release = json.loads(raw)
    if release.get("schemaVersion") != 1:
        raise Refused("Unsupported package schema")
    source = release["source"]
    if (not isinstance(source, dict) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", source.get("repository", ""))
            or not re.fullmatch(r"[0-9a-f]{40}", source.get("sha", ""))
            or not re.fullmatch(r"[1-9][0-9]*", source.get("runId", ""))
            or type(source.get("runAttempt")) is not int or source["runAttempt"] < 1):
        raise Refused("Missing source identity")
    files = release["files"]
    if not files or len({item["path"] for item in files}) != len(files):
        raise Refused("Empty or ambiguous artifact population")
    names = {item["path"] for item in files}
    if any(any(other.startswith(name + "/") for other in names) for name in names):
        raise Refused("Artifact file/directory collision")
    for item in files:
        path = within(package / "files", item["path"])
        if not path.is_file() or path.stat().st_size != item["bytes"] or digest(path.read_bytes()) != item["sha256"]:
            raise Refused("Artifact bytes differ from package")
    subject = digest(canonical({"source": source, "files": files}))
    if subject != release["subjectSha256"]:
        raise Refused("Artifact subject mismatch")
    if not required_proofs:
        raise Refused("No required runtime proof configured")
    for kind, expected_sha in required_proofs.items():
        record = release["proofs"].get(kind)
        if record is None:
            raise Refused("Missing required load proof")
        proof_raw = within(package, record["path"]).read_bytes()
        if record["sha256"] != expected_sha or digest(proof_raw) != expected_sha:
            raise Refused("Load proof identity mismatch")
        proof = json.loads(proof_raw)
        if (proof.get("kind"), proof.get("verdict"), proof.get("subjectSha256"), proof.get("source")) != (kind, "pass", subject, source):
            raise Refused("Load proof refused or belongs to another artifact/source")
    pointer = within(package, release["pointerFile"]).read_bytes()
    if digest(pointer) != release["pointerSha256"]:
        raise Refused("Publication pointer bytes differ from package")
    relative(release["targetPointer"])
    relative(release["releaseDirectory"])
    if (release["targetPointer"] == release["releaseDirectory"]
            or release["targetPointer"].startswith(release["releaseDirectory"] + "/")
            or release["releaseDirectory"].startswith(release["targetPointer"] + "/")):
        raise Refused("Mutable pointer overlaps immutable release")
    return release, pointer


def install_directory(package, root, release):
    destination = within(root, release["releaseDirectory"])
    if destination.exists():
        actual = {str(path.relative_to(destination)) for path in destination.rglob("*") if path.is_file()}
        expected = {item["path"] for item in release["files"]}
        if actual != expected:
            raise Refused("Existing immutable directory population differs")
        for item in release["files"]:
            path = within(destination, item["path"])
            if path.stat().st_size != item["bytes"] or digest(path.read_bytes()) != item["sha256"]:
                raise Refused("Existing immutable release differs; never overwrite")
        return destination
    public_directories(destination.parent, root)
    temporary = Path(tempfile.mkdtemp(prefix=".static-release-", dir=destination.parent))
    try:
        for item in release["files"]:
            source = within(package / "files", item["path"])
            target = within(temporary, item["path"])
            public_directories(target.parent, temporary)
            durable_write(target, source.read_bytes(), mode=0o644)
        temporary.chmod(0o755)
        # The pointer transaction serializes attempts on this same target, so
        # the directory rename can never replace another accepted release.
        os.rename(temporary, destination)
        sync_dir(destination.parent)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return destination


def publish(package, root, state_root, expected_package_sha, required_proofs, verify_candidate, verify_serving,
            checkpoint=lambda _: None, verify_rollback=None):
    """Callbacks raise on failed HTTP/load proof. Retrying reconciles journal + pointer.

    Publication state belongs on a durable volume outside the served asset root.
    A failure after pointer activation restores only that exact pointer. A newer
    publication wins; this attempt cannot roll it back.
    """
    package, root, state_root = map(lambda path: Path(path).resolve(), (package, root, state_root))
    if verify_rollback is None:
        raise Refused("Rollback readback callback is required before publication")
    if state_root.is_relative_to(root) or root.is_relative_to(state_root):
        raise Refused("Durable publication state must be outside the served root")
    release, pointer_bytes = validate_package(package, expected_package_sha, required_proofs)
    target = within(root, release["targetPointer"])
    key = expected_package_sha
    journal_path = state_root / "receipts" / (key + ".json")
    backup_path = state_root / "previous" / key

    def save(journal, state):
        journal["state"] = state
        durable_write(journal_path, canonical(journal))
        checkpoint(state)

    def confirm_rollback(journal):
        if verify_rollback is None:
            raise Refused("Rollback readback is required")
        verify_rollback(release, backup_path.read_bytes())
        with transaction(state_root, release["targetPointer"]):
            if digest(target.read_bytes()) == release["parentPointerSha256"]:
                save(journal, "ROLLED_BACK")
            else:
                save(journal, "SUPERSEDED")

    with transaction(state_root, release["targetPointer"]):
        current = target.read_bytes()
        journal = json.loads(journal_path.read_bytes()) if journal_path.exists() else None
        if journal is not None:
            if journal["packageSha256"] != key or journal["pointerSha256"] != digest(pointer_bytes):
                raise Refused("Journal does not identify this publication")
            if journal["state"] in ("ROLLED_BACK", "SUPERSEDED", "REFUSED"):
                raise Refused("Publication is terminal: " + journal["state"])
            if digest(current) == digest(pointer_bytes):
                pass  # SWITCHING/SWITCHED/ACCEPTED: reconcile by reading serving.
            elif digest(current) == release["parentPointerSha256"] and journal["state"] not in ("ACCEPTED", "SWITCHED"):
                pass
            else:
                save(journal, "SUPERSEDED")
                raise Refused("A newer publication owns the pointer")
        else:
            if digest(current) != release["parentPointerSha256"]:
                raise Refused("Stale publication parent")
            if digest(current) == digest(pointer_bytes):
                raise Refused("No-op pointer cannot identify a new publication")
            durable_write(backup_path, current)
            metadata = target.stat()
            journal = {"schemaVersion": 1, "packageSha256": key, "source": release["source"],
                       "pointerSha256": digest(pointer_bytes), "targetPointer": release["targetPointer"],
                       "releaseDirectory": release["releaseDirectory"],
                       "pointerMode": metadata.st_mode & 0o777,
                       "pointerOwner": [metadata.st_uid, metadata.st_gid]}
            save(journal, "PREPARED")
        install_directory(package, root, release)

    if journal["state"] == "ROLLBACK_SWITCHED":
        confirm_rollback(journal)
        raise Refused("Prior attempt failed; rollback reconciled")

    try:
        verify_candidate(release)
        with transaction(state_root, release["targetPointer"]):
            current = target.read_bytes()
            if digest(current) == release["parentPointerSha256"]:
                save(journal, "SWITCHING")
                durable_write(target, pointer_bytes, journal["pointerMode"], journal["pointerOwner"])
                checkpoint("POINTER_REPLACED")
                save(journal, "SWITCHED")
            elif digest(current) != digest(pointer_bytes):
                save(journal, "SUPERSEDED")
                raise Refused("Pointer changed while candidate was verified")
        verify_serving(release)
        with transaction(state_root, release["targetPointer"]):
            if target.read_bytes() != pointer_bytes:
                save(journal, "SUPERSEDED")
                raise Refused("Pointer changed before serving acceptance")
            save(journal, "ACCEPTED")
        return journal
    except Exception:
        rolled_back = False
        with transaction(state_root, release["targetPointer"]):
            current = target.read_bytes()
            if current == pointer_bytes:
                previous = backup_path.read_bytes()
                if digest(previous) != release["parentPointerSha256"]:
                    raise Refused("Rollback bytes differ from captured parent")
                durable_write(target, previous, journal["pointerMode"], journal["pointerOwner"])
                save(journal, "ROLLBACK_SWITCHED")
                rolled_back = True
            elif digest(current) == release["parentPointerSha256"]:
                save(journal, "REFUSED")
            else:
                save(journal, "SUPERSEDED")
        if rolled_back:
            confirm_rollback(journal)
        raise
