#!/usr/bin/env python3
"""Pin once from Unity's registry; hydrate only SHA256-pinned Build Cache archives."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile
import tempfile
import urllib.parse
import urllib.request

REGISTRY = "https://packages.unity.com"
MIRROR = "https://build-cache.sylphx.net"
# 2026-11-06: 71 measured consumer archives, max 603,699,285 bytes, plus 25%.
MAX_ARCHIVE = 754_624_107
MAX_UNPACKED = 2 * 1024 * 1024 * 1024
MAX_FILES = 100_000
# Unity 2022.3 package naming: reverse domain, documented ASCII, at most 214 chars.
NAME = re.compile(r"(?=.{1,214}\Z)[a-z0-9_-]+(?:\.[a-z0-9_-]+)+\Z")
VERSION = re.compile(r"[0-9][A-Za-z0-9.-]{0,79}\Z")
DIGEST = re.compile(r"[a-f0-9]{64}\Z")


def canonical(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def load(path):
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def graph(project):
    packages = project / "Packages"
    manifest = load(packages / "manifest.json")["dependencies"]
    locked = load(packages / "packages-lock.json")["dependencies"]
    direct = {name: version for name, version in manifest.items() if name.startswith("com.unity.")}
    for name, request in manifest.items():
        if not isinstance(request, str):
            raise ValueError(f"invalid Unity manifest request: {name}")
        # Registry version requests need a lock entry regardless of publisher.
        # Unselected file/Git preparation uses a URI or SCP-style colon.
        if name not in locked and (name in direct or ":" not in request):
            raise ValueError(f"manifest package missing from lock: {name}")
        if ":" not in request:
            node = locked[name]
            if node.get("source") == "embedded":
                # Embedded packages override version requests. The lock's file:
                # path is relative to Packages; the folder need not match name.
                location = node.get("version", "")
                if not isinstance(location, str) or not location.startswith("file:"):
                    raise ValueError(f"invalid embedded package override: {name}")
                relative = PurePosixPath(location[5:])
                embedded = packages.joinpath(*relative.parts)
                metadata_path = embedded / "package.json"
                if (relative.is_absolute() or not relative.parts or ".." in relative.parts or "\\" in location or
                        not embedded.resolve().is_relative_to(packages.resolve()) or not embedded.is_dir() or
                        not metadata_path.resolve().is_relative_to(packages.resolve()) or not metadata_path.is_file()):
                    raise ValueError(f"invalid embedded package override: {name}")
                metadata = load(metadata_path)
                if not isinstance(metadata, dict) or metadata.get("name") != name:
                    raise ValueError(f"invalid embedded package override: {name}")
            elif node.get("source") not in ("registry", "builtin"):
                raise ValueError(f"registry manifest request has a non-registry lock source: {name}")
    registry = {}
    for name, node in locked.items():
        if node.get("source") != "registry":
            continue
        if node.get("url", "").rstrip("/") != REGISTRY:
            if name.startswith("com.unity."):
                raise ValueError(f"Unity package uses an unexpected registry: {name}")
            continue
        version = node["version"]
        if not NAME.fullmatch(name) or not VERSION.fullmatch(version):
            raise ValueError(f"invalid exact Unity package identity: {name}@{version}")
        for dependency in node.get("dependencies", {}):
            if dependency not in locked:
                raise ValueError(f"incomplete lock closure: {name} requires {dependency}")
        registry[name] = node
    if not registry:
        raise ValueError("lock has no Unity registry packages; refusing an empty cache plan")
    # Include every direct official-registry request, not just Unity's publisher.
    # Keep the historical com.unity projection so existing pins remain valid;
    # unrelated local/Git preparation must not change this hash.
    direct.update({name: version for name, version in manifest.items() if name in registry})
    return registry, canonical(registry), canonical(direct)


def validate(project):
    registry, lock_hash, manifest_hash = graph(project)
    pins = load(project / "Packages/unity-packages.json")
    if pins.get("schemaVersion") != 1 or pins.get("lockSha256") != lock_hash or pins.get("manifestSha256") != manifest_hash:
        raise ValueError("Unity manifest/lock differs from committed pins; resolve and regenerate pins explicitly")
    entries = pins["packages"]
    if entries.keys() != registry.keys():
        raise ValueError("pins must cover the entire Unity registry lock closure, including transitive packages")
    for name, node in registry.items():
        pin = entries[name]
        if pin.get("version") != node["version"] or not DIGEST.fullmatch(pin.get("sha256", "")):
            raise ValueError(f"invalid or mismatched pin: {name}")
        size = pin.get("archiveBytes")
        if type(size) is not int or not 0 < size <= MAX_ARCHIVE:
            raise ValueError(f"pinned archiveBytes must be positive and at most {MAX_ARCHIVE}: {name}")
    return pins, registry


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("archive redirects are forbidden; no registry fallback")


class UnityCDNRedirect(urllib.request.HTTPRedirectHandler):
    """Only the explicit pin-maintenance operation may follow Unity's download CDN."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlsplit(newurl)
        parts = target.path.split("/")
        if (target.scheme != "https" or target.netloc != "cdn.packages.unity.com" or
                newurl != f"https://cdn.packages.unity.com{target.path}" or
                len(parts) != 5 or parts[:2] != ["", "tarballs"]):
            raise ValueError("unexpected Unity archive redirect")
        name, directory, filename = parts[2:]
        if not NAME.fullmatch(name) or not re.fullmatch(r"[a-f0-9]{40}\.tgz", filename):
            raise ValueError("unexpected Unity archive redirect")
        # A version may itself start with <name>-; the exact download source
        # selects the requested identity, not this ambiguous CDN directory.
        # pin() then verifies metadata SHA1 and archive name/version/dependencies.
        versions = (directory, directory.removeprefix(name + "-"))
        if not any(VERSION.fullmatch(version) and req.full_url in (
                f"https://download.packages.unity.com/{name}/-/{name}-{version}.tgz", newurl)
                for version in versions):
            raise ValueError("unexpected Unity archive redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url, destination, expected=None, *, expected_size=None, allow_unity_cdn=False):
    """Bounded disk streaming; transport and integrity errors never select another host."""
    sha256, sha1, size = hashlib.sha256(), hashlib.sha1(), 0
    opener = urllib.request.build_opener(UnityCDNRedirect if allow_unity_cdn else NoRedirect)
    with opener.open(url, timeout=120) as response, destination.open("xb") as stream:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_ARCHIVE:
                raise ValueError("Unity package archive exceeds size limit")
            stream.write(chunk)
            sha256.update(chunk)
            sha1.update(chunk)
    if expected is not None and sha256.hexdigest() != expected:
        raise ValueError("Unity package SHA256 mismatch; refusing archive")
    if expected_size is not None and size != expected_size:
        raise ValueError("Unity package archive byte count differs from pins; refusing archive")
    return sha256.hexdigest(), sha1.hexdigest(), size


def unpack(archive, destination, name, node):
    total, count = 0, 0
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar:
            count += 1
            path = PurePosixPath(member.name)
            if (count > MAX_FILES or "\\" in member.name or path.is_absolute() or
                    ".." in path.parts or not path.parts or path.parts[0] != "package" or
                    not (member.isdir() or member.isfile())):
                raise ValueError("unsafe Unity package archive entry")
            total += member.size
            if total > MAX_UNPACKED:
                raise ValueError("Unity package unpacked size exceeds limit")
            target = destination.joinpath(*path.parts[1:])
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(member) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            target.chmod(0o755 if member.mode & 0o111 else 0o644)
    metadata = load(destination / "package.json")
    if metadata.get("name") != name or metadata.get("version") != node["version"]:
        raise ValueError(f"archive identity differs from lock: {name}")
    if metadata.get("dependencies", {}) != node.get("dependencies", {}):
        raise ValueError(f"archive dependency graph differs from lock: {name}")


def hydrate(project, mirror=MIRROR):
    pins, registry = validate(project)  # Validate all drift before any network or mutation.
    url = urllib.parse.urlsplit(mirror)
    if url.scheme != "https" or not url.netloc or url.query or url.fragment or url.username or url.path not in ("", "/"):
        raise ValueError("mirror must be an HTTPS origin")
    packages = project / "Packages"
    receipt_path = project / ".unity-packages-cache-receipt.json"
    receipt_path.unlink(missing_ok=True)
    for name in registry:
        if os.path.lexists(packages / name):
            raise ValueError(f"refusing to overwrite an existing embedded package: {name}")
    moved = []
    with tempfile.TemporaryDirectory(prefix=".unity-upm-", dir=packages) as temporary:
        stage = Path(temporary)
        for name, node in sorted(registry.items()):
            pin = pins["packages"][name]
            archive = stage / f"{name}.tgz"
            fetch(f"{mirror.rstrip('/')}/upm/{pin['sha256']}/{name}/{pin['version']}", archive,
                  pin["sha256"], expected_size=pin["archiveBytes"])
            unpack(archive, stage / name, name, node)
            archive.unlink()
        try:
            for name in sorted(registry):
                (stage / name).rename(packages / name)
                moved.append(packages / name)
            receipt = {"schemaVersion": 1, "lockSha256": pins["lockSha256"],
                       "manifestSha256": pins["manifestSha256"], "mirror": mirror,
                       "packages": pins["packages"], "packageCount": len(registry)}
            receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        except BaseException:
            for path in moved:
                shutil.rmtree(path)
            raise
    print(f"Embedded {len(registry)} SHA256-verified Unity packages; registry fallback disabled")
    return receipt_path


def pin(project):
    """Explicit maintenance operation only, never called by the CI hydration action."""
    registry, lock_hash, manifest_hash = graph(project)
    entries = {}
    with tempfile.TemporaryDirectory(prefix="unity-upm-pin-") as temporary:
        for name, node in sorted(registry.items()):
            version = node["version"]
            with urllib.request.urlopen(f"{REGISTRY}/{name}", timeout=60) as response:
                metadata = json.load(response)["versions"][version]
            dist = metadata["dist"]
            expected_url = f"https://download.packages.unity.com/{name}/-/{name}-{version}.tgz"
            if dist["tarball"] != expected_url or not re.fullmatch(r"[a-f0-9]{40}", dist["shasum"]):
                raise ValueError(f"unexpected Unity distribution metadata: {name}")
            archive = Path(temporary) / f"{name}.tgz"
            sha256, sha1, size = fetch(expected_url, archive, allow_unity_cdn=True)
            if sha1 != dist["shasum"]:
                raise ValueError(f"Unity registry SHA1 mismatch: {name}")
            unpack(archive, Path(temporary) / name, name, node)
            entries[name] = {"version": version, "sha256": sha256, "archiveBytes": size}
    result = {"schemaVersion": 1, "lockSha256": lock_hash, "manifestSha256": manifest_hash, "packages": entries}
    target = project / "Packages/unity-packages.json"
    target.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Pinned {len(entries)} Unity packages in {target}")
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("hydrate", "pin"))
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--mirror", default=MIRROR)
    args = parser.parse_args()
    project = args.project.resolve(strict=True)
    if not (project / "Assets").is_dir() or not (project / "ProjectSettings").is_dir():
        parser.error("expected a prepared Unity project")
    result = pin(project) if args.operation == "pin" else hydrate(project, args.mirror)
    if args.operation == "hydrate" and os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write(f"receipt={result}\n")


if __name__ == "__main__":
    main()
