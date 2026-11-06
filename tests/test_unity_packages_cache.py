import hashlib
import http.server
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import patch

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
                     "packages": {name: {"version": "1.2.3", "sha256": hashlib.sha256(data).hexdigest()}
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
        def local_fetch(url, path, expected=None):
            self.assertTrue(url.startswith(upm.MIRROR + "/upm/"), url)
            return self.real_fetch(url.replace(upm.MIRROR, self.origin), path, expected)
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
        for change in ("manifest", "lock", "missing transitive", "version", "digest"):
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
                self.write_json("manifest.json", manifest)
                self.write_json("packages-lock.json", {"dependencies": registry})
                self.write_json("unity-packages.json", pins)
                with self.assertRaises(ValueError):
                    self.hydrate()
        self.assertEqual(self.requests, [])

    def test_missing_lock_and_incomplete_graph_fail_closed(self):
        target = self.project / "Packages/packages-lock.json"
        target.unlink()
        with self.assertRaises(FileNotFoundError):
            self.hydrate()
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

    def test_maintenance_redirect_allows_only_the_observed_https_unity_cdn(self):
        handler = upm.UnityCDNRedirect()
        request = upm.urllib.request.Request("https://download.packages.unity.com/com.unity.burst/-/com.unity.burst-1.4.8.tgz")
        valid = "https://cdn.packages.unity.com/tarballs/com.unity.burst/com.unity.burst-1.4.8/abc.tgz"
        self.assertEqual(handler.redirect_request(request, None, 302, "Found", {}, valid).full_url, valid)
        for url in (valid.replace("https:", "http:"), valid.replace("com/tarballs/", "com/other/"),
                    valid.replace("cdn.packages.unity.com", "evil.example"),
                    valid.replace("cdn.packages.unity.com", "cdn.packages.unity.com:8443")):
            with self.assertRaises(ValueError):
                handler.redirect_request(request, None, 302, "Found", {}, url)

    def test_size_bound_is_enforced_while_streaming(self):
        self.responses["/oversize"] = b"x" * 100
        with patch.object(upm, "MAX_ARCHIVE", 50), self.assertRaisesRegex(ValueError, "size limit"):
            self.real_fetch(self.origin + "/oversize", Path(self.tmp.name) / "download")

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

    def test_action_only_hydrates_and_does_not_contact_registry(self):
        source = (ACTION / "action.yml").read_text()
        self.assertIn('packages.py" hydrate', source)
        self.assertNotIn('packages.py" pin', source)
        self.assertNotIn("packages.unity.com", source)
        self.assertIn('"$GITHUB_WORKSPACE/$PROJECT_PATH"', source)


if __name__ == "__main__":
    unittest.main()
