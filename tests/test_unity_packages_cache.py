import hashlib
from email.message import Message
import http.server
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / ".github/actions/unity-packages-cache"
spec = importlib.util.spec_from_file_location("unity_packages_cache", ACTION / "packages.py")
upm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upm)


def archive(name, version="1.2.3", dependencies=None, extra=()):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as tar:
        content = json.dumps({"name": name, "version": version, "dependencies": dependencies or {}}).encode()
        entry = tarfile.TarInfo("package/package.json")
        entry.size = len(content)
        tar.addfile(entry, io.BytesIO(content))
        for entry, data in extra:
            tar.addfile(entry, io.BytesIO(data))
    return stream.getvalue()


class UnityPackagesCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / "project with spaces"
        for directory in ("Assets", "ProjectSettings", "Packages"):
            (self.project / directory).mkdir(parents=True)
        self.registry = {
            "com.unity.first": {"version": "1.2.3", "source": "registry", "depth": 0,
                                "url": upm.REGISTRY, "dependencies": {"com.unity.transitive": "1.2.3"}},
            "com.unity.transitive": {"version": "1.2.3", "source": "registry", "depth": 1,
                                     "url": upm.REGISTRY, "dependencies": {}},
        }
        self.manifest = {"dependencies": {"com.unity.first": "1.2.3", "com.other.local": "file:../local"}}
        self.write_json("manifest.json", self.manifest)
        self.write_json("packages-lock.json", {"dependencies": self.registry})
        self.archives = {name: archive(name, dependencies=node["dependencies"]) for name, node in self.registry.items()}
        self.pins = {"schemaVersion": 1, "lockSha256": upm.canonical(self.registry),
                     "manifestSha256": upm.canonical({"com.unity.first": "1.2.3"}),
                     "packages": {name: {"version": "1.2.3", "sha256": hashlib.sha256(data).hexdigest(),
                                         "archiveBytes": len(data)}
                                  for name, data in self.archives.items()}}
        self.write_json("unity-packages.json", self.pins)
        self.requests = []
        self.responses = {}
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests.append(self.path)
                value = outer.responses.get(self.path)
                if value == "redirect":
                    self.send_response(302)
                    self.send_header("Location", "/must-not-follow")
                    self.end_headers()
                elif value is None:
                    self.send_error(503)
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(value)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.origin = f"http://127.0.0.1:{server.server_port}"
        self.real_fetch = upm.fetch
        for name, entry in self.pins["packages"].items():
            self.responses[f"/upm/{entry['sha256']}/{name}/{entry['version']}"] = self.archives[name]

    def write_json(self, name, data):
        (self.project / "Packages" / name).write_text(json.dumps(data))

    def hydrate(self):
        def local_fetch(url, path, expected=None, *, expected_size=None):
            self.assertTrue(url.startswith(upm.MIRROR + "/upm/"), url)
            return self.real_fetch(url.replace(upm.MIRROR, self.origin), path, expected, expected_size=expected_size)
        with patch.object(upm, "fetch", side_effect=local_fetch):
            return upm.hydrate(self.project)

    def test_embeds_full_transitive_closure_and_receipt_without_registry_requests(self):
        receipt = json.loads(self.hydrate().read_text())
        self.assertEqual(receipt["packageCount"], 2)
        self.assertEqual(receipt["packages"], self.pins["packages"])
        self.assertEqual(len(self.requests), 2)
        for name in self.registry:
            self.assertEqual(json.loads((self.project / "Packages" / name / "package.json").read_text())["name"], name)
        self.assertEqual(upm.load(self.project / "Packages/packages-lock.json")["dependencies"], self.registry)
        self.assertFalse(list((self.project / "Packages").glob(".unity-upm-*")))

    def test_manifest_lock_and_pin_drift_fail_before_network(self):
        for change in ("manifest", "lock", "missing transitive", "version", "digest", "missing size",
                       "oversize", "zero size", "boolean size", "noninteger size"):
            with self.subTest(change=change):
                manifest = json.loads(json.dumps(self.manifest))
                registry = json.loads(json.dumps(self.registry))
                pins = json.loads(json.dumps(self.pins))
                if change == "manifest":
                    manifest["dependencies"]["com.unity.first"] = "2.0.0"
                if change == "lock":
                    registry["com.unity.transitive"]["depth"] = 2
                if change == "missing transitive":
                    del pins["packages"]["com.unity.transitive"]
                if change == "version":
                    pins["packages"]["com.unity.first"]["version"] = "9.0.0"
                if change == "digest":
                    pins["packages"]["com.unity.first"]["sha256"] = "A" * 64
                if change == "missing size":
                    del pins["packages"]["com.unity.first"]["archiveBytes"]
                if change == "oversize":
                    pins["packages"]["com.unity.first"]["archiveBytes"] = upm.MAX_ARCHIVE + 1
                if change == "zero size":
                    pins["packages"]["com.unity.first"]["archiveBytes"] = 0
                if change == "boolean size":
                    pins["packages"]["com.unity.first"]["archiveBytes"] = True
                if change == "noninteger size":
                    pins["packages"]["com.unity.first"]["archiveBytes"] = 12.5
                self.write_json("manifest.json", manifest)
                self.write_json("packages-lock.json", {"dependencies": registry})
                self.write_json("unity-packages.json", pins)
                with self.assertRaises(ValueError):
                    self.hydrate()
        self.assertEqual(self.requests, [])

    def test_missing_direct_registry_requests_fail_before_network_or_mutation(self):
        upm.validate(self.project)  # Positive control: the existing pins are valid.
        receipt = self.project / ".unity-packages-cache-receipt.json"
        receipt.write_text("existing receipt must not be removed")
        for name, request in (("com.autodesk.fbx", "5.1.3"), ("1.2.3", "4.5.6"),
                              ("net.example.tool_kit", "1.0.0"), ("com.unity.prepared", "file:../prepared")):
            with self.subTest(name=name, request=request):
                manifest = json.loads(json.dumps(self.manifest))
                manifest["dependencies"][name] = request
                self.write_json("manifest.json", manifest)
                before = {p.relative_to(self.project): p.read_bytes() for p in self.project.rglob("*") if p.is_file()}
                with patch.object(upm, "fetch") as fetch, patch.object(upm.urllib.request, "urlopen") as metadata, patch.object(
                        upm.tempfile, "TemporaryDirectory") as staging:
                    for operation in (upm.validate, upm.hydrate, upm.pin):
                        with self.assertRaisesRegex(ValueError, f"manifest package missing from lock: {name}"):
                            operation(self.project)
                    fetch.assert_not_called()
                    metadata.assert_not_called()
                    staging.assert_not_called()
                after = {p.relative_to(self.project): p.read_bytes() for p in self.project.rglob("*") if p.is_file()}
                self.assertEqual(after, before)
        self.assertEqual(self.requests, [])

    def test_unselected_local_git_and_locked_foreign_registry_requests_preserve_pins(self):
        upm.validate(self.project)
        for request in ("file:../prepared", "file:../prepared.tgz", "http://example.com/repo.git",
                        "https://example.com/repo.git#v1.2.3", "ssh://git@example.com/repo.git",
                        "git://example.com/repo", "git+https://example.com/repo", "git+ssh://git@example.com/repo",
                        "git+file:///tmp/repo", "git@example.com:owner/repo.git"):
            with self.subTest(request=request):
                manifest = json.loads(json.dumps(self.manifest))
                manifest["dependencies"]["net.example.prepared"] = request
                self.write_json("manifest.json", manifest)
                pins, selected = upm.validate(self.project)
                self.assertEqual(pins, self.pins)
                self.assertEqual(selected, self.registry)
                self.assertEqual(upm.graph(self.project)[1:], (self.pins["lockSha256"], self.pins["manifestSha256"]))
        self.registry["net.other.registry"] = {"version": "1.0.0", "source": "registry", "url": "https://other.example"}
        self.write_json("packages-lock.json", {"dependencies": self.registry})
        for request in ("1.0.0", "2.0.0"):
            with self.subTest(foreign_request=request):
                manifest = json.loads(json.dumps(self.manifest))
                manifest["dependencies"]["net.other.registry"] = request
                self.write_json("manifest.json", manifest)
                pins, selected = upm.validate(self.project)
                self.assertEqual(pins, self.pins)
                self.assertNotIn("net.other.registry", selected)
        builtin = "com.unity.modules.audio"
        self.registry[builtin] = {"version": "1.0.0", "source": "builtin", "dependencies": {}}
        manifest["dependencies"][builtin] = "1.0.0"
        self.pins["manifestSha256"] = upm.canonical({"com.unity.first": "1.2.3", builtin: "1.0.0"})
        self.write_json("manifest.json", manifest)
        self.write_json("packages-lock.json", {"dependencies": self.registry})
        self.write_json("unity-packages.json", self.pins)
        pins, selected = upm.validate(self.project)
        self.assertEqual(pins, self.pins)
        self.assertNotIn(builtin, selected)
        self.assertEqual(upm.graph(self.project)[1:], (self.pins["lockSha256"], self.pins["manifestSha256"]))
        self.assertEqual(self.requests, [])

    def test_uri_requests_cannot_select_stale_official_registry_locks(self):
        receipt = self.project / ".unity-packages-cache-receipt.json"
        receipt.write_text("previous receipt must survive")
        for name in ("com.unity.first", "com.autodesk.fbx"):
            for request in ("file:../prepared", "file:../prepared.tgz", "https://example.com/repo.git#v1.2.3",
                            "ssh://git@example.com/repo.git", "git+https://example.com/repo", "git@example.com:owner/repo.git"):
                registry = json.loads(json.dumps(self.registry))
                if name not in registry:
                    registry[name] = {"version": "5.1.3", "source": "registry", "url": upm.REGISTRY, "dependencies": {}}
                manifest = json.loads(json.dumps(self.manifest))
                manifest["dependencies"][name] = registry[name]["version"]
                pins = json.loads(json.dumps(self.pins))
                pins["lockSha256"] = upm.canonical(registry)
                pins["packages"][name] = {"version": registry[name]["version"], "sha256": "a" * 64, "archiveBytes": 1}
                pins["manifestSha256"] = upm.canonical({n: v for n, v in manifest["dependencies"].items()
                                                       if n.startswith("com.unity.") or n in registry})
                self.write_json("manifest.json", manifest)
                self.write_json("packages-lock.json", {"dependencies": registry})
                self.write_json("unity-packages.json", pins)
                self.assertEqual(upm.validate(self.project)[0], pins)  # Registry-version positive control.
                manifest["dependencies"][name] = request
                # Matching hashes cannot authorize a conflicting source choice.
                pins["manifestSha256"] = upm.canonical({n: v for n, v in manifest["dependencies"].items()
                                                       if n.startswith("com.unity.") or n in registry})
                self.write_json("manifest.json", manifest)
                self.write_json("unity-packages.json", pins)
                before = {p.relative_to(self.project): p.read_bytes() for p in self.project.rglob("*") if p.is_file()}
                for operation in (upm.graph, upm.validate, upm.hydrate, upm.pin):
                    with self.subTest(name=name, request=request, operation=operation.__name__):
                        refusal = None
                        with patch.object(upm, "fetch", side_effect=AssertionError("fetch reached")) as fetch, patch.object(
                                upm.urllib.request, "urlopen", side_effect=AssertionError("metadata reached")) as metadata:
                            try:
                                operation(self.project)
                            except Exception as error:
                                refusal = error
                            self.assertIsInstance(refusal, ValueError)
                            self.assertIn("URI manifest request has an official registry lock source", str(refusal))
                            fetch.assert_not_called()
                            metadata.assert_not_called()
                        self.assertEqual({p.relative_to(self.project): p.read_bytes()
                                          for p in self.project.rglob("*") if p.is_file()}, before)
                        self.assertFalse(list((self.project / "Packages").glob(".unity-upm-*")))
        self.assertEqual(self.requests, [])

    def test_stale_uri_lock_sources_cannot_satisfy_direct_registry_requests(self):
        for name in ("com.autodesk.fbx", "1.2.3", "net.example.tool_kit"):
            for source in ("git", "local", "local-tarball", "tarball", "unknown"):
                with self.subTest(name=name, source=source):
                    registry = json.loads(json.dumps(self.registry))
                    registry[name] = {"version": "file:../prepared", "source": source, "dependencies": {}}
                    manifest = json.loads(json.dumps(self.manifest))
                    manifest["dependencies"][name] = "file:../prepared" if source != "git" else "https://example.com/repo.git"
                    self.write_json("packages-lock.json", {"dependencies": registry})
                    self.write_json("manifest.json", manifest)
                    self.assertEqual(upm.validate(self.project)[0], self.pins)  # URI preparation is a valid control.
                    manifest["dependencies"][name] = "5.1.3"
                    self.write_json("manifest.json", manifest)
                    before = {p.relative_to(self.project): p.read_bytes() for p in self.project.rglob("*") if p.is_file()}
                    with patch.object(upm, "fetch") as fetch, patch.object(upm.urllib.request, "urlopen") as metadata, patch.object(
                            upm.tempfile, "TemporaryDirectory") as staging:
                        for operation in (upm.validate, upm.hydrate, upm.pin):
                            with self.assertRaisesRegex(ValueError, "registry manifest request has a non-registry lock source"):
                                operation(self.project)
                        fetch.assert_not_called()
                        metadata.assert_not_called()
                        staging.assert_not_called()
                    after = {p.relative_to(self.project): p.read_bytes() for p in self.project.rglob("*") if p.is_file()}
                    self.assertEqual(after, before)
        self.assertEqual(self.requests, [])

    def test_embedded_version_overrides_require_genuine_contained_package_identity(self):
        name, folder = "net.example.embedded", "prepared-embedded"
        packages = self.project / "Packages"
        embedded = packages / folder
        embedded.mkdir()
        good = json.dumps({"name": name, "version": "1.0.0"})
        (embedded / "package.json").write_text(good)
        self.manifest["dependencies"][name] = "9.9.9"  # Embedded identity overrides the requested version.
        self.registry[name] = {"version": f"file:{folder}", "source": "embedded", "dependencies": {}}
        self.write_json("manifest.json", self.manifest)
        self.write_json("packages-lock.json", {"dependencies": self.registry})
        self.assertEqual(upm.validate(self.project)[0], self.pins)
        receipt = self.project / ".unity-packages-cache-receipt.json"
        receipt.write_text("previous receipt must survive admission failures")
        outside = self.project / "outside-embedded"
        outside.mkdir()
        (outside / "package.json").write_text(good)
        (packages / "linked-embedded").symlink_to(outside, target_is_directory=True)
        for case in ("missing directory", "missing package.json", "wrong name", "traversal", "absolute path",
                     "non-file location", "nonstring location", "outside directory symlink", "outside metadata symlink"):
            with self.subTest(case=case):
                embedded.mkdir(exist_ok=True)
                metadata = embedded / "package.json"
                metadata.unlink(missing_ok=True)
                metadata.write_text(good)
                self.registry[name]["version"] = f"file:{folder}"
                if case in ("missing directory", "missing package.json"):
                    metadata.unlink()
                if case == "missing directory":
                    embedded.rmdir()
                if case == "wrong name":
                    metadata.write_text(json.dumps({"name": "net.other.package", "version": "1.0.0"}))
                if case == "traversal":
                    self.registry[name]["version"] = "file:../outside-embedded"
                if case == "absolute path":
                    self.registry[name]["version"] = f"file:{outside}"
                if case == "non-file location":
                    self.registry[name]["version"] = "1.0.0"
                if case == "nonstring location":
                    self.registry[name]["version"] = None
                if case == "outside directory symlink":
                    self.registry[name]["version"] = "file:linked-embedded"
                if case == "outside metadata symlink":
                    metadata.unlink()
                    metadata.symlink_to(outside / "package.json")
                self.write_json("packages-lock.json", {"dependencies": self.registry})
                with patch.object(upm, "fetch") as fetch, patch.object(upm.urllib.request, "urlopen") as request, patch.object(
                        upm.tempfile, "TemporaryDirectory") as staging:
                    for operation in (upm.validate, upm.hydrate, upm.pin):
                        with self.assertRaisesRegex(ValueError, "invalid embedded package override"):
                            operation(self.project)
                    fetch.assert_not_called()
                    request.assert_not_called()
                    staging.assert_not_called()
                self.assertEqual(receipt.read_text(), "previous receipt must survive admission failures")
        (embedded / "package.json").unlink()
        (embedded / "package.json").write_text(good)
        self.registry[name]["version"] = f"file:{folder}"
        self.write_json("packages-lock.json", {"dependencies": self.registry})
        (packages / "linked-embedded").unlink()
        receipt = json.loads(self.hydrate().read_text())
        self.assertEqual(receipt["packages"], self.pins["packages"])
        self.assertEqual(receipt["packageCount"], 2)
        self.assertEqual((embedded / "package.json").read_text(), good)
        self.assertEqual(len(self.requests), 2)

    def test_embedded_overrides_reject_contained_symlink_components_before_metadata_read(self):
        name = "net.example.embedded"
        for case in ("metadata", "package directory", "ancestor directory"):
            with self.subTest(case=case), tempfile.TemporaryDirectory(dir=self.tmp.name) as temporary:
                project = Path(temporary)
                packages = project / "Packages"
                pkg = packages / "pkg"
                pkg.mkdir(parents=True)
                (pkg / "package.json").write_text(json.dumps({"name": name, "version": "1.0.0"}))
                registry = json.loads(json.dumps(self.registry))
                registry[name] = {"version": "file:pkg", "source": "embedded", "dependencies": {}}
                manifest = json.loads(json.dumps(self.manifest))
                manifest["dependencies"][name] = "9.9.9"
                (packages / "manifest.json").write_text(json.dumps(manifest))
                (packages / "packages-lock.json").write_text(json.dumps({"dependencies": registry}))
                pins = packages / "unity-packages.json"
                pins.write_text(json.dumps(self.pins))
                self.assertEqual(upm.validate(project)[0], self.pins)  # Genuine directory/metadata positive control.
                if case == "metadata":
                    target = packages / "real-package.json"
                    (pkg / "package.json").rename(target)
                    (pkg / "package.json").symlink_to(target)
                elif case == "package directory":
                    target = packages / "real-package"
                    pkg.rename(target)
                    pkg.symlink_to(target, target_is_directory=True)
                else:
                    parent = packages / "real-parent"
                    parent.mkdir()
                    pkg.rename(parent / "nested")
                    registry[name]["version"] = "file:real-parent/nested"
                    (packages / "packages-lock.json").write_text(json.dumps({"dependencies": registry}))
                    self.assertEqual(upm.validate(project)[0], self.pins)  # Genuine nested-path positive control.
                    (packages / "parent-link").symlink_to(parent, target_is_directory=True)
                    registry[name]["version"] = "file:parent-link/nested"
                    (packages / "packages-lock.json").write_text(json.dumps({"dependencies": registry}))
                receipt = project / ".unity-packages-cache-receipt.json"
                receipt.write_text("unchanged receipt")
                before_pins = pins.read_bytes()
                reads = []
                real_load = upm.json.load

                def record_load(stream):
                    reads.append(os.fstat(stream.fileno()).st_ino)
                    return real_load(stream)

                with patch.object(upm.json, "load", side_effect=record_load), patch.object(upm, "fetch") as fetch, patch.object(
                        upm.urllib.request, "urlopen") as request, patch.object(upm.tempfile, "TemporaryDirectory") as staging:
                    for operation in (upm.validate, upm.hydrate, upm.pin):
                        with self.assertRaisesRegex(ValueError, "invalid embedded package override"):
                            operation(project)
                    self.assertEqual(reads, [(packages / "manifest.json").stat().st_ino,
                                             (packages / "packages-lock.json").stat().st_ino] * 3)
                    fetch.assert_not_called()
                    request.assert_not_called()
                    staging.assert_not_called()
                self.assertEqual(receipt.read_text(), "unchanged receipt")
                self.assertEqual(pins.read_bytes(), before_pins)
        self.assertEqual(self.requests, [])

    def package_input_fixture(self, temporary):
        project = Path(temporary) / "project"
        packages = project / "Packages"
        pkg = packages / "ancestor" / "pkg"
        pkg.mkdir(parents=True)
        name = "net.example.embedded"
        (pkg / "package.json").write_text(json.dumps({"name": name, "version": "1.0.0"}))
        registry = json.loads(json.dumps(self.registry))
        registry[name] = {"version": "file:ancestor/pkg", "source": "embedded", "dependencies": {}}
        manifest = json.loads(json.dumps(self.manifest))
        manifest["dependencies"][name] = "9.9.9"
        (packages / "manifest.json").write_text(json.dumps(manifest))
        (packages / "packages-lock.json").write_text(json.dumps({"dependencies": registry}))
        (packages / "unity-packages.json").write_text(json.dumps(self.pins))
        self.assertEqual(upm.validate(project)[0], self.pins)  # Genuine nested-path positive control.
        outside = Path(temporary) / "outside"
        shutil.copytree(packages, outside)
        return project, packages, outside

    def test_packages_admission_rejects_symlinks_at_every_input_component(self):
        for relative in (".", "manifest.json", "packages-lock.json", "unity-packages.json",
                         "ancestor", "ancestor/pkg", "ancestor/pkg/package.json"):
            with self.subTest(component=relative), tempfile.TemporaryDirectory(dir=self.tmp.name) as temporary:
                project, packages, outside = self.package_input_fixture(temporary)
                target = packages / relative
                target.rename(Path(temporary) / "original")
                replacement = outside / relative
                target.symlink_to(replacement, target_is_directory=replacement.is_dir())
                receipt = project / ".unity-packages-cache-receipt.json"
                receipt.write_text("unchanged receipt")
                outside_before = {p.relative_to(outside): p.read_bytes() for p in outside.rglob("*") if p.is_file()}
                reads = []
                real_load = upm.json.load

                def record_load(stream):
                    reads.append(os.fstat(stream.fileno()).st_ino)
                    return real_load(stream)

                with patch.object(upm.json, "load", side_effect=record_load), patch.object(upm, "fetch") as fetch, patch.object(
                        upm.urllib.request, "urlopen") as request, patch.object(upm.tempfile, "TemporaryDirectory") as staging:
                    operations = (upm.validate, upm.hydrate) if relative == "unity-packages.json" else (
                        upm.graph, upm.validate, upm.hydrate, upm.pin)
                    for operation in operations:
                        with self.assertRaises(ValueError):
                            operation(project)
                    fetch.assert_not_called()
                    request.assert_not_called()
                    staging.assert_not_called()
                self.assertTrue(set(reads).isdisjoint(p.stat().st_ino for p in outside.rglob("*") if p.is_file()))
                self.assertEqual(receipt.read_text(), "unchanged receipt")
                self.assertEqual({p.relative_to(outside): p.read_bytes() for p in outside.rglob("*") if p.is_file()}, outside_before)
        self.assertEqual(self.requests, [])

    def test_packages_input_swaps_never_read_replacement_paths(self):
        for timing in ("before open", "after open"):
            for relative in (".", "manifest.json", "packages-lock.json", "unity-packages.json",
                             "ancestor", "ancestor/pkg", "ancestor/pkg/package.json"):
                with self.subTest(timing=timing, component=relative), tempfile.TemporaryDirectory(dir=self.tmp.name) as temporary:
                    project, packages, outside = self.package_input_fixture(temporary)
                    target = packages / relative
                    replacement = outside / relative
                    outside_inodes = {p.stat().st_ino for p in outside.rglob("*") if p.is_file()}
                    outside_reads, swaps = [], []
                    real_open, real_path_open, real_load = os.open, Path.open, upm.json.load

                    def swap():
                        target.rename(Path(temporary) / "original")
                        target.symlink_to(replacement, target_is_directory=replacement.is_dir())
                        swaps.append(relative)

                    def open_fd(path, flags, *args, **kwargs):
                        selected = not swaps and kwargs.get("dir_fd") is not None and os.fspath(path) == target.name
                        if selected and timing == "before open":
                            swap()
                        fd = real_open(path, flags, *args, **kwargs)
                        if selected and timing == "after open":
                            swap()
                        return fd

                    def open_path(path, *args, **kwargs):
                        # The old pathname reader uses this boundary; the fixed
                        # reader instead exercises the real dir_fd os.open above.
                        selected = not swaps and (path == target or target in path.parents)
                        if selected and timing == "before open":
                            swap()
                        stream = real_path_open(path, *args, **kwargs)
                        if selected and timing == "after open":
                            swap()
                        return stream

                    def record_load(stream):
                        if os.fstat(stream.fileno()).st_ino in outside_inodes:
                            outside_reads.append(relative)
                        return real_load(stream)

                    with patch.object(upm.os, "open", side_effect=open_fd), patch.object(Path, "open", open_path), patch.object(
                            upm.json, "load", side_effect=record_load), patch.object(upm, "fetch") as fetch:
                        if timing == "before open":
                            with self.assertRaises(ValueError):
                                upm.validate(project)
                        else:
                            self.assertEqual(upm.validate(project)[0], self.pins)
                        fetch.assert_not_called()
                    self.assertEqual(swaps, [relative])  # Prove that the intended race actually occurred.
                    self.assertEqual(outside_reads, [])  # An opened original inode is allowed; replacements are not.
        self.assertEqual(self.requests, [])

    def test_cli_rejects_packages_root_symlink_without_deleting_receipt_or_writing_outside(self):
        with tempfile.TemporaryDirectory(dir=self.tmp.name) as temporary:
            project, packages, outside = self.package_input_fixture(temporary)
            for name in ("Assets", "ProjectSettings"):
                (project / name).mkdir()
            receipt = project / ".unity-packages-cache-receipt.json"
            receipt.write_text("previous receipt")
            packages.rename(Path(temporary) / "original")
            packages.symlink_to(outside, target_is_directory=True)
            before = {p.relative_to(outside): p.read_bytes() for p in outside.rglob("*") if p.is_file()}
            result = subprocess.run([sys.executable, upm.__file__, "hydrate", "--project", str(project),
                                     "--mirror", "https://127.0.0.1:9"], capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("invalid Unity package inputs", result.stderr)
            self.assertEqual(receipt.read_text(), "previous receipt")
            self.assertEqual({p.relative_to(outside): p.read_bytes() for p in outside.rglob("*") if p.is_file()}, before)
            self.assertFalse(list(outside.glob(".unity-upm-*")))
        self.assertEqual(self.requests, [])

    def test_hydration_writes_stay_on_opened_directories_after_path_replacement(self):
        for component in ("Packages", "project"):
            with self.subTest(component=component), tempfile.TemporaryDirectory(dir=self.tmp.name) as temporary:
                project, packages, outside = self.package_input_fixture(temporary)
                receipt = project / ".unity-packages-cache-receipt.json"
                receipt.write_text("original receipt")
                if component == "project":
                    outside_project = Path(temporary) / "outside-project"
                    outside_project.mkdir()
                    outside.rename(outside_project / "Packages")
                    outside = outside_project
                    (outside / receipt.name).write_text("outside receipt must survive")
                before = {p.relative_to(outside): p.read_bytes() for p in outside.rglob("*") if p.is_file()}
                pins_inode = (packages / "unity-packages.json").stat().st_ino
                real_load, swaps = upm.json.load, []
                saved = Path(temporary) / "original"

                def load_and_swap(stream):
                    value = real_load(stream)
                    if os.fstat(stream.fileno()).st_ino == pins_inode:
                        target = packages if component == "Packages" else project
                        target.rename(saved)
                        target.symlink_to(outside, target_is_directory=True)
                        swaps.append(component)
                    return value

                def local_fetch(url, destination, expected=None, *, expected_size=None):
                    return self.real_fetch(url.replace(upm.MIRROR, self.origin), destination, expected, expected_size=expected_size)

                with patch.object(upm.json, "load", side_effect=load_and_swap), patch.object(upm, "fetch", side_effect=local_fetch):
                    upm.hydrate(project)
                self.assertEqual(swaps, [component])
                self.assertEqual({p.relative_to(outside): p.read_bytes() for p in outside.rglob("*") if p.is_file()}, before)
                installed = saved if component == "Packages" else saved / "Packages"
                for name in self.registry:
                    metadata = installed / name / "package.json"
                    self.assertTrue(metadata.is_file(), f"package must be installed in original directory: {name}")
                    self.assertEqual(json.loads(metadata.read_text())["name"], name)
                original_receipt = receipt if component == "Packages" else saved / receipt.name
                self.assertEqual(json.loads(original_receipt.read_text())["packages"], self.pins["packages"])
                self.assertEqual({p.relative_to(outside): p.read_bytes() for p in outside.rglob("*") if p.is_file()}, before)
                self.assertFalse(list(installed.glob(".unity-upm-*")))
        self.assertEqual(len(self.requests), 4)

    def test_pin_output_uses_opened_packages_and_never_follows_output_symlink(self):
        for component in ("Packages", "pins"):
            with self.subTest(component=component), tempfile.TemporaryDirectory(dir=self.tmp.name) as temporary:
                project, packages, outside = self.package_input_fixture(temporary)
                (packages / "unity-packages.json").write_text('{"old": true}')
                (outside / "unity-packages.json").write_text("outside pins must not be overwritten")
                before = {p.relative_to(outside): p.read_bytes() for p in outside.rglob("*") if p.is_file()}
                saved, swaps = Path(temporary) / "original", []

                def metadata(url, **kwargs):
                    if not swaps:
                        target = packages if component == "Packages" else packages / "unity-packages.json"
                        target.rename(saved)
                        target.symlink_to(outside if component == "Packages" else outside / "unity-packages.json",
                                          target_is_directory=component == "Packages")
                        swaps.append(component)
                    name = url.rsplit("/", 1)[-1]
                    data = self.archives[name]
                    dist = {"tarball": f"https://download.packages.unity.com/{name}/-/{name}-1.2.3.tgz",
                            "shasum": hashlib.sha1(data).hexdigest()}
                    return io.BytesIO(json.dumps({"versions": {"1.2.3": {"dist": dist}}}).encode())

                def download(url, destination, **kwargs):
                    name = url.split("/")[3]
                    data = self.archives[name]
                    destination.write_bytes(data)
                    return hashlib.sha256(data).hexdigest(), hashlib.sha1(data).hexdigest(), len(data)

                with patch.object(upm.urllib.request, "urlopen", side_effect=metadata), patch.object(upm, "fetch", side_effect=download):
                    if component == "Packages":
                        upm.pin(project)
                        self.assertEqual(json.loads((saved / "unity-packages.json").read_text()), self.pins)
                    else:
                        with self.assertRaises(ValueError):
                            upm.pin(project)
                self.assertEqual(swaps, [component])
                self.assertEqual({p.relative_to(outside): p.read_bytes() for p in outside.rglob("*") if p.is_file()}, before)
        self.assertEqual(self.requests, [])

    def test_staging_replacement_before_extract_or_install_aborts_without_outside_changes(self):
        for boundary in ("extract", "install"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory(dir=self.tmp.name) as temporary:
                project, packages, outside = self.package_input_fixture(temporary)
                for name, data in self.archives.items():
                    (outside / f"{name}.tgz").write_bytes(data)
                    (outside / name).mkdir()
                    (outside / name / "keep.txt").write_text("unrelated outside file")
                before = {p.relative_to(outside): p.read_bytes() for p in outside.rglob("*") if p.is_file()}
                original = Path(temporary) / "original-stage"
                real_unlink, swaps = Path.unlink, []

                def swap(stage):
                    stage.rename(original)
                    stage.symlink_to(outside, target_is_directory=True)
                    swaps.append(boundary)

                def local_fetch(url, destination, expected=None, *, expected_size=None):
                    result = self.real_fetch(url.replace(upm.MIRROR, self.origin), destination, expected, expected_size=expected_size)
                    if boundary == "extract" and not swaps:
                        swap(destination.parent)
                    return result

                def unlink_and_swap(path, *args, **kwargs):
                    real_unlink(path, *args, **kwargs)
                    if boundary == "install" and path.name == "com.unity.transitive.tgz":
                        swap(path.parent)

                refusal = None
                with patch.object(upm, "fetch", side_effect=local_fetch), patch.object(Path, "unlink", unlink_and_swap):
                    try:
                        upm.hydrate(project)
                    except Exception as error:
                        refusal = error
                self.assertEqual(swaps, [boundary])
                self.assertEqual({p.relative_to(outside): p.read_bytes() for p in outside.rglob("*") if p.is_file()}, before)
                for name in self.registry:
                    self.assertFalse(os.path.lexists(packages / name), "no registry package may be installed after a stage swap")
                self.assertIsInstance(refusal, ValueError)
                self.assertIn("staging directory changed", str(refusal))
                self.assertFalse((project / ".unity-packages-cache-receipt.json").exists())

    def test_nonstring_manifest_requests_fail_closed(self):
        upm.validate(self.project)
        for name in ("com.autodesk.fbx", "com.unity.first", "com.other.local"):
            for request in (None, False, 7, 1.2, [], {}):
                with self.subTest(name=name, request=request):
                    manifest = json.loads(json.dumps(self.manifest))
                    manifest["dependencies"][name] = request
                    self.write_json("manifest.json", manifest)
                    with patch.object(upm, "fetch") as fetch, patch.object(upm.urllib.request, "urlopen") as metadata, patch.object(
                            upm.tempfile, "TemporaryDirectory") as staging:
                        for operation in (upm.validate, upm.hydrate, upm.pin):
                            with self.assertRaisesRegex(ValueError, f"invalid Unity manifest request: {name}"):
                                operation(self.project)
                        fetch.assert_not_called()
                        metadata.assert_not_called()
                        staging.assert_not_called()
        self.assertEqual(self.requests, [])

    def test_missing_lock_and_incomplete_graph_fail_closed(self):
        target = self.project / "Packages/packages-lock.json"
        target.unlink()
        with self.assertRaisesRegex(ValueError, "invalid Unity package input path") as refusal:
            self.hydrate()
        self.assertIsInstance(refusal.exception.__cause__, FileNotFoundError)
        del self.registry["com.unity.transitive"]
        self.write_json("packages-lock.json", {"dependencies": self.registry})
        with self.assertRaisesRegex(ValueError, "incomplete lock closure"):
            self.hydrate()
        self.assertEqual(self.requests, [])

    def test_non_unity_preparation_does_not_invalidate_unity_pins(self):
        self.manifest["dependencies"]["com.other.local"] = "file:../prepared.tgz"
        self.write_json("manifest.json", self.manifest)
        upm.validate(self.project)

    def test_hash_mismatch_and_missing_mirror_never_fallback_or_partially_embed(self):
        for response in (b"corrupt", None):
            with self.subTest(response=response):
                last = sorted(self.responses)[-1]
                self.responses[last] = response
                with self.assertRaises((ValueError, OSError)):
                    self.hydrate()
                for name in self.registry:
                    self.assertFalse((self.project / "Packages" / name).exists())
                self.assertFalse((self.project / ".unity-packages-cache-receipt.json").exists())
        self.assertTrue(all(path.startswith("/upm/") for path in self.requests))

    def test_redirect_is_not_followed(self):
        self.responses["/redirect"] = "redirect"
        with self.assertRaisesRegex(ValueError, "redirects are forbidden"):
            self.real_fetch(self.origin + "/redirect", Path(self.tmp.name) / "download")
        self.assertEqual(self.requests, ["/redirect"])

    def add_registry_package(self, name="com.autodesk.fbx", version="5.1.3"):
        self.registry[name] = {"version": version, "source": "registry", "depth": 0,
                               "url": upm.REGISTRY, "dependencies": {"com.unity.transitive": "1.2.3"}}
        self.manifest["dependencies"][name] = version
        data = archive(name, version, self.registry[name]["dependencies"])
        self.archives[name] = data
        entry = {"version": version, "sha256": hashlib.sha256(data).hexdigest(), "archiveBytes": len(data)}
        self.pins["packages"][name] = entry
        self.pins["lockSha256"] = upm.canonical(self.registry)
        self.pins["manifestSha256"] = upm.canonical({n: v for n, v in self.manifest["dependencies"].items() if n in self.archives})
        self.write_json("manifest.json", self.manifest)
        self.write_json("packages-lock.json", {"dependencies": self.registry})
        self.write_json("unity-packages.json", self.pins)
        self.responses[f"/upm/{entry['sha256']}/{name}/{version}"] = data

    def test_outside_publisher_pin_then_hydrate_through_real_transport_and_redirect_handlers(self):
        self.add_registry_package()
        requests, routes = [], {}
        for name, node in self.registry.items():
            version, data = node["version"], self.archives[name]
            download = f"https://download.packages.unity.com/{name}/-/{name}-{version}.tgz"
            # Both layouts are observed upstream; neither is a publisher privilege.
            directory = version if name == "com.autodesk.fbx" else f"{name}-{version}"
            cdn = f"https://cdn.packages.unity.com/tarballs/{name}/{directory}/{hashlib.sha1(data).hexdigest()}.tgz"
            metadata = {"versions": {version: {"dist": {"tarball": download, "shasum": hashlib.sha1(data).hexdigest()}}}}
            routes[f"{upm.REGISTRY}/{name}"] = (200, {}, json.dumps(metadata).encode())
            routes[download] = (302, {"Location": cdn}, b"")
            routes[cdn] = (200, {}, data)
            routes[f"{upm.MIRROR}/upm/{hashlib.sha256(data).hexdigest()}/{name}/{version}"] = (200, {}, data)

        class HTTPSFixture(upm.urllib.request.HTTPSHandler):
            def https_open(self, request):
                requests.append(request.full_url)
                code, headers, body = routes[request.full_url]
                message = Message()
                for key, value in headers.items():
                    message[key] = value
                response = upm.urllib.response.addinfourl(io.BytesIO(body), message, request.full_url, code)
                response.msg = "Fixture response"
                return response

        build_opener = upm.urllib.request.build_opener
        metadata_opener = build_opener(HTTPSFixture)
        def opener(*handlers):
            return build_opener(*handlers, HTTPSFixture)
        with patch.object(upm.urllib.request, "build_opener", side_effect=opener), patch.object(
                upm.urllib.request, "urlopen", side_effect=metadata_opener.open):
            generated = json.loads(upm.pin(self.project).read_text())
            self.assertEqual(generated, self.pins)
            self.assertEqual(len(requests), 9)  # Metadata, download redirect and CDN body for each package.
            requests.clear()
            receipt = json.loads(upm.hydrate(self.project).read_text())
        self.assertEqual(receipt["packages"], self.pins["packages"])
        self.assertEqual(receipt["packageCount"], 3)
        self.assertEqual(len(requests), 3)
        self.assertTrue(all(url.startswith(upm.MIRROR + "/upm/") for url in requests))
        for name, node in self.registry.items():
            self.assertEqual(upm.load(self.project / "Packages" / name / "package.json"),
                             {"name": name, "version": node["version"], "dependencies": node["dependencies"]})
        self.assertEqual(upm.load(self.project / "Packages/packages-lock.json")["dependencies"], self.registry)
        self.assertFalse(list((self.project / "Packages").glob(".unity-upm-*")))

    def test_ambiguous_cdn_directory_binds_both_sources_and_pin_verifies_content(self):
        name, directory = "1.2.3", "1.2.3-4.5.6"
        metadata_url = f"{upm.REGISTRY}/{name}"
        cdn = f"https://cdn.packages.unity.com/tarballs/{name}/{directory}/" + "a" * 40 + ".tgz"
        build_opener = upm.urllib.request.build_opener
        for version in ("4.5.6", "1.2.3-4.5.6"):
            source = f"https://download.packages.unity.com/{name}/-/{name}-{version}.tgz"
            for case in ("valid", "wrong version", "SHA1 mismatch", "wrong name"):
                with self.subTest(version=version, case=case), tempfile.TemporaryDirectory(dir=self.tmp.name) as temporary:
                    project = Path(temporary)
                    packages = project / "Packages"
                    packages.mkdir()
                    node = {"version": version, "source": "registry", "url": upm.REGISTRY, "dependencies": {}}
                    (packages / "manifest.json").write_text(json.dumps({"dependencies": {name: version}}))
                    (packages / "packages-lock.json").write_text(json.dumps({"dependencies": {name: node}}))
                    pins = packages / "unity-packages.json"
                    self.assertFalse(pins.exists())
                    body = archive("com.other.package" if case == "wrong name" else name,
                                   "9.9.9" if case == "wrong version" else version)
                    shasum = "0" * 40 if case == "SHA1 mismatch" else hashlib.sha1(body).hexdigest()
                    self.assertEqual(shasum == hashlib.sha1(body).hexdigest(), case != "SHA1 mismatch")
                    metadata = {"versions": {version: {"dist": {"tarball": source, "shasum": shasum}}}}
                    # The same ambiguous URL has isolated response state in every
                    # case: neither a previous body nor a previous pin can mask a guard.
                    routes = {metadata_url: (200, {}, json.dumps(metadata).encode()),
                              source: (302, {"Location": cdn}, b""), cdn: (200, {}, body)}
                    requests = []

                    class HTTPSFixture(upm.urllib.request.HTTPSHandler):
                        def https_open(self, request):
                            requests.append(request.full_url)
                            code, headers, data = routes[request.full_url]
                            message = Message()
                            for key, value in headers.items():
                                message[key] = value
                            response = upm.urllib.response.addinfourl(io.BytesIO(data), message, request.full_url, code)
                            response.msg = "Fixture response"
                            return response

                    metadata_opener = build_opener(HTTPSFixture)
                    def opener(*handlers):
                        return build_opener(*handlers, HTTPSFixture)
                    with patch.object(upm.urllib.request, "build_opener", side_effect=opener), patch.object(
                            upm.urllib.request, "urlopen", side_effect=metadata_opener.open), patch.object(
                            upm, "unpack", wraps=upm.unpack) as unpack:
                        if case == "valid":
                            generated = json.loads(upm.pin(project).read_text())
                            self.assertEqual(generated["packages"], {name: {"version": version,
                                             "sha256": hashlib.sha256(body).hexdigest(), "archiveBytes": len(body)}})
                        else:
                            error = "SHA1 mismatch" if case == "SHA1 mismatch" else "archive identity differs"
                            with self.assertRaisesRegex(ValueError, error):
                                upm.pin(project)
                            self.assertFalse(pins.exists())
                        if case == "SHA1 mismatch":
                            unpack.assert_not_called()
                        else:
                            unpack.assert_called_once()
                            self.assertEqual(unpack.call_args.args[2:], (name, node))
                    self.assertEqual(requests, [metadata_url, source, cdn])

    def test_outside_publisher_manifest_drift_refused_before_network(self):
        self.add_registry_package()
        upm.validate(self.project)  # Positive control: a malformed plan cannot satisfy this negative.
        for request in ("5.1.2", "file:../fbx.tgz", "https://example.com/fbx.git", None):
            with self.subTest(request=request):
                manifest = json.loads(json.dumps(self.manifest))
                if request is None:
                    del manifest["dependencies"]["com.autodesk.fbx"]
                else:
                    manifest["dependencies"]["com.autodesk.fbx"] = request
                self.write_json("manifest.json", manifest)
                reason = ("URI manifest request has an official registry lock source"
                          if request is not None and ":" in request else "manifest/lock differs")
                with patch.object(upm, "fetch") as fetch, self.assertRaisesRegex(ValueError, reason):
                    upm.hydrate(self.project)
                fetch.assert_not_called()
        self.assertEqual(self.requests, [])

    def test_package_name_grammar_is_publisher_independent_and_path_safe(self):
        for name in ("com.autodesk.fbx", "net.3company.tool_name-v2", "x." + "a" * 212):
            with self.subTest(name=name):
                self.add_registry_package(name)
                upm.validate(self.project)
        for name in ("com.Example.tool", "com.example/escape", "com.example\\escape", "com.example%2ftool",
                     "com..example", ".com.example", "com.example.", "single", "com.example?tool",
                     "com.example#tool", "com.example@tool", "com.example:tool", "com.example.é", "x." + "a" * 213):
            with self.subTest(name=name):
                registry = self.registry | {name: {"version": "1.2.3", "source": "registry", "url": upm.REGISTRY}}
                self.write_json("packages-lock.json", {"dependencies": registry})
                with patch.object(upm.urllib.request, "urlopen") as request, self.assertRaisesRegex(ValueError, "invalid exact"):
                    upm.pin(self.project)
                request.assert_not_called()
        self.assertEqual(self.requests, [])

    def test_official_origin_not_publisher_selects_closure_and_preserves_other_preparation(self):
        self.add_registry_package()
        for name, source, url in (("net.other.git", "git", "https://example.com/repo.git"),
                                  ("net.other.local", "local", ""),
                                  ("net.other.registry", "registry", "https://other.example")):
            self.registry[name] = {"version": "1.0.0", "source": source, "url": url}
            self.manifest["dependencies"][name] = "file:../prepared.tgz"
        self.write_json("packages-lock.json", {"dependencies": self.registry})
        self.write_json("manifest.json", self.manifest)
        pins, selected = upm.validate(self.project)
        self.assertEqual(set(selected), {"com.unity.first", "com.unity.transitive", "com.autodesk.fbx"})
        self.assertEqual(pins, self.pins)
        self.registry["com.autodesk.fbx"]["url"] = "https://other.example"
        self.write_json("packages-lock.json", {"dependencies": self.registry})
        with self.assertRaisesRegex(ValueError, "manifest/lock differs"):
            upm.validate(self.project)
        self.registry["com.unity.first"]["url"] = "https://other.example"
        self.write_json("packages-lock.json", {"dependencies": self.registry})
        with self.assertRaisesRegex(ValueError, "unexpected registry"):
            upm.validate(self.project)

    def test_maintenance_redirect_allows_only_canonical_identity_bound_unity_cdn_paths(self):
        handler = upm.UnityCDNRedirect()
        for name, version in (("com.autodesk.fbx", "5.1.3"), ("com.unity.burst", "5.1.3"),
                              ("net.3company.tool_name-v2", "5.1.3"), ("x." + "a" * 212, "5.1.3"),
                              ("1.2.3", "1.2.3-rc.1"), ("1.2.3", "1.2.3-4.5")):
            for directory in (version, f"{name}-{version}"):
                with self.subTest(name=name, version=version, directory=directory):
                    request = upm.urllib.request.Request(f"https://download.packages.unity.com/{name}/-/{name}-{version}.tgz")
                    valid = f"https://cdn.packages.unity.com/tarballs/{name}/{directory}/" + "a" * 40 + ".tgz"
                    self.assertEqual(handler.redirect_request(request, None, 302, "Found", {}, valid).full_url, valid)
                    cdn_request = upm.urllib.request.Request(valid)
                    self.assertEqual(handler.redirect_request(cdn_request, None, 302, "Found", {}, valid).full_url, valid)
        request = upm.urllib.request.Request("https://download.packages.unity.com/com.unity.burst/-/com.unity.burst-5.1.3.tgz")
        valid = "https://cdn.packages.unity.com/tarballs/com.unity.burst/5.1.3/" + "a" * 40 + ".tgz"
        invalid = (valid.replace("https:", "http:"), valid.replace("com/tarballs/", "com/other/"),
                   valid.replace("cdn.packages.unity.com", "evil.example"),
                   valid.replace("cdn.packages.unity.com", "cdn.packages.unity.com:8443"),
                   valid.replace("cdn.packages.unity.com", "user@cdn.packages.unity.com"),
                   valid + "?download=1", valid + "#fragment", valid.replace("5.1.3", "5.1.2"),
                   valid.replace("com.unity.burst", "com.unity.wrong"),
                   valid.replace("/5.1.3/", "/com.unity.wrong-5.1.3/"),
                   valid.replace("/5.1.3/", "/../5.1.3/"), valid.replace("/5.1.3/", "/%35.1.3/"),
                   valid.replace("com.unity.burst", "com.autodesk%2ffbx"),
                   valid.replace("a" * 40, "a" * 39), valid.replace("a" * 40, "A" * 40),
                   valid.replace(".tgz", ".tgz/extra"), valid.replace(".tgz", ".zip"))
        for url in invalid:
            with self.subTest(target=url), self.assertRaisesRegex(ValueError, "unexpected Unity archive redirect"):
                handler.redirect_request(request, None, 302, "Found", {}, url)
        for source in (request.full_url.replace("https:", "http:"),
                       request.full_url.replace("download.packages.unity.com", "evil.example"),
                       request.full_url.replace("download.packages.unity.com", "user@download.packages.unity.com"),
                       request.full_url.replace("download.packages.unity.com", "download.packages.unity.com:443"),
                       request.full_url + "?q=1", request.full_url + "#fragment",
                       request.full_url.replace("com.unity.burst-5.1.3", "com.unity.wrong-5.1.3"),
                       valid.replace("5.1.3", "5.1.2")):
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, "unexpected Unity archive redirect"):
                handler.redirect_request(upm.urllib.request.Request(source), None, 302, "Found", {}, valid)

    def test_size_bound_is_enforced_while_streaming(self):
        self.responses["/oversize"] = b"x" * 100
        with patch.object(upm, "MAX_ARCHIVE", 50), self.assertRaisesRegex(ValueError, "size limit"):
            self.real_fetch(self.origin + "/oversize", Path(self.tmp.name) / "download")

    def test_measured_archive_ceiling_accepts_exactly_cap_and_refuses_cap_plus_one(self):
        self.assertEqual(upm.MAX_ARCHIVE, (603_699_285 * 5 + 3) // 4)
        chunk = b"x" * (1024 * 1024)

        class Stream:
            def __init__(self, size):
                self.remaining = size
                self.received = 0

            def read(self, size):
                data = chunk[:min(size, self.remaining)]
                self.remaining -= len(data)
                self.received += len(data)
                return data

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        class Sink:
            def __init__(self):
                self.written = 0

            def open(self, mode):
                assert mode == "xb"
                return self

            def write(self, data):
                self.written += len(data)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        for size in (upm.MAX_ARCHIVE, upm.MAX_ARCHIVE + 1):
            with self.subTest(archiveBytes=size):
                response, sink = Stream(size), Sink()
                opener = Mock()
                opener.open.return_value = response
                with patch.object(upm.urllib.request, "build_opener", return_value=opener):
                    if size == upm.MAX_ARCHIVE:
                        self.assertEqual(self.real_fetch(upm.MIRROR, sink)[2], size)
                        self.assertEqual(sink.written, size)
                    else:
                        with self.assertRaisesRegex(ValueError, "size limit"):
                            self.real_fetch(upm.MIRROR, sink)
                        self.assertLessEqual(sink.written, upm.MAX_ARCHIVE)
                self.assertEqual(response.received, size)

    def test_pinned_byte_count_is_checked_even_when_sha256_matches(self):
        data = b"verified archive bytes"
        self.responses["/size-mismatch"] = data
        with self.assertRaisesRegex(ValueError, "byte count differs"):
            self.real_fetch(self.origin + "/size-mismatch", Path(self.tmp.name) / "download",
                            hashlib.sha256(data).hexdigest(), expected_size=len(data) + 1)

    def test_existing_embedded_package_is_never_overwritten(self):
        existing = self.project / "Packages/com.unity.first"
        existing.mkdir()
        (existing / "user-work").write_text("preserve")
        with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
            self.hydrate()
        self.assertEqual((existing / "user-work").read_text(), "preserve")
        self.assertEqual(self.requests, [])

    def test_tar_traversal_absolute_links_and_special_files_are_rejected(self):
        for path, kind in (("package/../../escape", tarfile.REGTYPE), ("/absolute", tarfile.REGTYPE),
                           ("other/file", tarfile.REGTYPE), ("package/link", tarfile.SYMTYPE),
                           ("package/hard", tarfile.LNKTYPE), ("package/device", tarfile.CHRTYPE)):
            with self.subTest(path=path, kind=kind):
                entry = tarfile.TarInfo(path)
                entry.type = kind
                entry.linkname = "../../escape"
                data = archive("com.unity.first", dependencies=self.registry["com.unity.first"]["dependencies"], extra=((entry, b""),))
                target = Path(self.tmp.name) / "bad.tgz"
                target.write_bytes(data)
                with tempfile.TemporaryDirectory(dir=self.tmp.name) as output, self.assertRaises(ValueError):
                    upm.unpack(target, Path(output), "com.unity.first", self.registry["com.unity.first"])

    def test_archive_identity_dependencies_and_executable_mode_are_checked(self):
        node = self.registry["com.unity.transitive"]
        target = Path(self.tmp.name) / "test.tgz"
        for name, deps in (("com.unity.wrong", {}), ("com.unity.transitive", {"com.unity.unlocked": "1.0.0"})):
            target.write_bytes(archive(name, dependencies=deps))
            with tempfile.TemporaryDirectory(dir=self.tmp.name) as output, self.assertRaises(ValueError):
                upm.unpack(target, Path(output), "com.unity.transitive", node)
        executable = tarfile.TarInfo("package/Tools/compiler")
        executable.mode = 0o755
        executable.size = 4
        target.write_bytes(archive("com.unity.transitive", extra=((executable, b"tool"),)))
        with tempfile.TemporaryDirectory(dir=self.tmp.name) as output:
            upm.unpack(target, Path(output), "com.unity.transitive", node)
            self.assertEqual((Path(output) / "Tools/compiler").stat().st_mode & 0o777, 0o755)

    def test_pin_maintenance_verifies_registry_sha1_and_commits_sha256(self):
        def metadata(url, timeout):
            name = url.removeprefix(upm.REGISTRY + "/")
            self.assertIn(name, self.archives)
            version = self.registry[name]["version"]
            result = {"versions": {version: {"dist": {"tarball": f"https://download.packages.unity.com/{name}/-/{name}-{version}.tgz",
                                                      "shasum": hashlib.sha1(self.archives[name]).hexdigest()}}}}
            return io.BytesIO(json.dumps(result).encode())
        def fetch(url, destination, *, allow_unity_cdn=False):
            self.assertTrue(allow_unity_cdn)
            name = url.split("/")[-3]
            destination.write_bytes(self.archives[name])
            return hashlib.sha256(self.archives[name]).hexdigest(), hashlib.sha1(self.archives[name]).hexdigest(), len(self.archives[name])
        with patch.object(upm.urllib.request, "urlopen", side_effect=metadata), patch.object(upm, "fetch", side_effect=fetch):
            result = upm.pin(self.project)
        self.assertEqual(json.loads(result.read_text()), self.pins)

    def test_action_resolves_relative_and_absolute_project_paths_without_shell_interpolation(self):
        source = (ACTION / "action.yml").read_text()
        body = source.split("      run: |\n", 1)[1]
        script = "\n".join(line.removeprefix("        ") for line in body.splitlines())
        with tempfile.TemporaryDirectory() as temporary:
            action = Path(temporary) / "action"
            action.mkdir()
            (action / "packages.py").write_text("import json,sys; print(json.dumps(sys.argv[1:]))")
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            for project, expected in (("project with spaces", workspace / "project with spaces"),
                                      (str(Path(temporary) / "isolated project"), Path(temporary) / "isolated project")):
                with self.subTest(project=project):
                    result = subprocess.run(["bash", "-euc", script], text=True, capture_output=True,
                        env=os.environ | {"GITHUB_WORKSPACE": str(workspace), "GITHUB_ACTION_PATH": str(action),
                                          "PROJECT_PATH": project, "MIRROR": upm.MIRROR})
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(result.stdout), ["hydrate", "--project", str(expected), "--mirror", upm.MIRROR])

    def test_action_only_hydrates_and_does_not_contact_registry(self):
        source = (ACTION / "action.yml").read_text()
        self.assertIn('packages.py" hydrate', source)
        self.assertNotIn('packages.py" pin', source)
        self.assertNotIn("packages.unity.com", source)
        self.assertIn('project="$GITHUB_WORKSPACE/$project"', source)


if __name__ == "__main__":
    unittest.main()
