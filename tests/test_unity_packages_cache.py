import hashlib
from email.message import Message
import http.server
import importlib.util
import io
import json
import os
import subprocess
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
                with patch.object(upm, "fetch") as fetch, self.assertRaisesRegex(ValueError, "manifest/lock differs"):
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
