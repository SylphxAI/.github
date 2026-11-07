"""Installer entrypoints exercise real curl retries and cross-origin redirects."""
from contextlib import contextmanager, redirect_stderr
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.parse
import zipfile

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "synthetic-installer-token"
QUERY = "synthetic-signed-query"
PRIVATE_BODY = b"private-error-body"


def load(name, action, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / ".github/actions" / action / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


web = load("installer_web", "web-smoke", "web_smoke.py")
secret = load("installer_secret", "secret-scan", "secret_scan.py")
bridge = load("installer_bridge", "native-bridge-check", "bridge_contract.py")
net = web.curl_retry


class Endpoint:
    def __init__(self, bodies, replies=()):
        self.bodies, self.replies, self.requests = bodies, list(replies), []
        self.redirect_to = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                owner.requests.append((self.path, dict(self.headers)))
                reply = owner.replies.pop(0) if owner.replies else 200
                if reply == "timeout":
                    time.sleep(0.3)
                    reply = 200
                body = owner.bodies.get(urllib.parse.urlsplit(self.path).path, PRIVATE_BODY)
                if reply >= 400:
                    body = PRIVATE_BODY
                self.send_response(reply)
                if reply == 302:
                    self.send_header("Location", owner.redirect_to)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def gitleaks_archive():
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        body = b"synthetic executable bytes"
        member = tarfile.TarInfo("gitleaks")
        member.size = len(body)
        archive.addfile(member, io.BytesIO(body))
    return output.getvalue(), body


class InstallerRetryTests(unittest.TestCase):
    def endpoint(self, bodies, replies=()):
        endpoint = Endpoint(bodies, replies)
        self.addCleanup(endpoint.close)
        return endpoint

    @contextmanager
    def transport(self, endpoint, *, budget=2, attempt_time=0.06):
        # Rewrite only the provider origin, not the transport or its response
        # policy. Real curl still observes timing, credentials and redirects.
        request, urls = net.request, []

        def routed(url, destination, **kwargs):
            urls.append(url)
            path = urllib.parse.urlsplit(url).path
            kwargs["policy"] = net.Policy(budget=budget, attempt_time=attempt_time, base_delay=0.005)
            return request(endpoint.url + path + "?signature=" + QUERY, destination, **kwargs)

        with patch.object(net, "request", side_effect=routed), redirect_stderr(io.StringIO()) as log:
            yield log, urls

    def assert_redacted(self, log):
        text = log.getvalue()
        for value in (TOKEN, QUERY, PRIVATE_BODY.decode(), "http://", "https://", "Authorization"):
            self.assertNotIn(value, text)
        for field in net.FIELDS:
            self.assertIn(field + "=", text)

    def test_chrome_timeout_recovery_preserves_both_installed_binaries(self):
        bodies = {}
        for name, binary in (("chromedriver-linux64", "chromedriver"),
                             ("chrome-headless-shell-linux64", "chrome-headless-shell")):
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w") as archive:
                archive.writestr(name + "/" + binary, name.encode())
            bodies[f"/chrome-for-testing-public/1.2.3.4/linux64/{name}.zip"] = output.getvalue()
        endpoint = self.endpoint(bodies, ["timeout", "timeout"])
        with tempfile.TemporaryDirectory() as directory, self.transport(endpoint) as (log, urls), \
                patch.object(web, "libs_missing", return_value=False), \
                patch.object(web, "run", return_value=SimpleNamespace(stdout="")):
            root = web.install_chrome("1.2.3.4", directory)
            self.assertEqual([Path(path).read_bytes() for path in web.chrome_binaries(root)],
                             [b"chromedriver-linux64", b"chrome-headless-shell-linux64"])
        self.assertEqual(len(endpoint.requests), 4)
        self.assertEqual(len(urls), 2)
        self.assertTrue(all(url.startswith("https://storage.googleapis.com/") for url in urls))
        self.assertEqual(log.getvalue().count("curl_exit=28"), 2)
        self.assert_redacted(log)

    def test_chrome_budget_exhaustion_stops_before_extracting(self):
        endpoint = self.endpoint({}, ["timeout"])
        with tempfile.TemporaryDirectory() as directory, \
                self.transport(endpoint, budget=0.12, attempt_time=10) as (log, _):
            started = time.monotonic()
            with self.assertRaisesRegex(OSError, "curl exit 28"):
                web.install_chrome("1.2.3.4", directory)
            self.assertLess(time.monotonic() - started, 1)
            self.assertFalse(any(Path(path).exists() for path in web.chrome_binaries(
                str(Path(directory) / "chrome-for-testing-1.2.3.4"))))
        self.assertEqual(len(endpoint.requests), 1)
        self.assertIn("stop=budget-exhausted", log.getvalue())
        self.assert_redacted(log)

    def test_gitleaks_retries_and_still_checks_the_archive_digest(self):
        archive, binary = gitleaks_archive()
        path = f"/gitleaks/gitleaks/releases/download/v{secret.VERSION}/gitleaks_{secret.VERSION}_linux_x64.tar.gz"
        endpoint = self.endpoint({path: archive}, [503, 429])
        with tempfile.TemporaryDirectory() as directory, self.transport(endpoint) as (log, urls), \
                patch.object(secret.platform, "system", return_value="Linux"), \
                patch.object(secret.platform, "machine", return_value="x86_64"), \
                patch.dict(secret.CHECKSUMS, x86_64=("x64", hashlib.sha256(archive).hexdigest())):
            installed = secret.install(Path(directory), secret.VERSION)
            self.assertEqual(Path(installed).read_bytes(), binary)
            self.assertEqual(Path(installed).stat().st_mode & 0o777, 0o700)
            with patch.dict(secret.CHECKSUMS, x86_64=("x64", "0" * 64)):
                with self.assertRaisesRegex(secret.ScanError, "checksum mismatch"):
                    secret.install(Path(directory), secret.VERSION)
            self.assertEqual(Path(installed).read_bytes(), binary)
        self.assertEqual(len(endpoint.requests), 4)
        self.assertTrue(all(url.startswith("https://github.com/gitleaks/") for url in urls))
        self.assertEqual(log.getvalue().count("stop=retry"), 2)
        self.assert_redacted(log)

    def test_gitleaks_permanent_refusal_is_not_retried_or_installed(self):
        for code in (401, 403, 404):
            with self.subTest(code=code):
                endpoint = self.endpoint({}, [code, 200])
                with tempfile.TemporaryDirectory() as directory, self.transport(endpoint) as (log, _), \
                        patch.object(secret.platform, "system", return_value="Linux"), \
                        patch.object(secret.platform, "machine", return_value="x86_64"):
                    with self.assertRaises(secret.ScanError):
                        secret.install(Path(directory), secret.VERSION)
                    self.assertFalse((Path(directory) / "gitleaks").exists())
                self.assertEqual(len(endpoint.requests), 1)
                self.assertIn(f"http_code={code}", log.getvalue())
                self.assertIn("stop=permanent", log.getvalue())
                self.assert_redacted(log)

    def test_native_binary_retries_and_strips_auth_before_cross_origin_redirect(self):
        body = b"verified asset payload"
        target = self.endpoint({"/asset": body})
        origin = self.endpoint({}, ["timeout", "timeout", 302])
        origin.redirect_to = target.url + "/asset?signature=" + QUERY
        with self.transport(origin) as (log, urls), patch.dict(bridge.os.environ, GH_TOKEN=TOKEN):
            self.assertEqual(bridge.api("releases/assets/123", binary=True), body)
        self.assertEqual(urls, ["https://api.github.com/repos/Cubeage/cubeage-native-sdk/releases/assets/123"])
        self.assertEqual(len(origin.requests), 3)
        self.assertEqual(len(target.requests), 1)
        for _, headers in origin.requests:
            self.assertEqual(headers.get("Authorization"), "Bearer " + TOKEN)
            self.assertEqual(headers.get("Accept"), "application/octet-stream")
            self.assertEqual(headers.get("X-GitHub-Api-Version"), "2022-11-28")
        self.assertNotIn("Authorization", target.requests[0][1])
        self.assertEqual(log.getvalue().count("curl_exit=28"), 2)
        self.assert_redacted(log)

    def test_native_binary_refusal_is_not_retried(self):
        endpoint = self.endpoint({}, [403, 200])
        with self.transport(endpoint) as (log, _), patch.dict(bridge.os.environ, GH_TOKEN=TOKEN):
            with self.assertRaisesRegex(OSError, "HTTP 403"):
                bridge.api("releases/assets/123", binary=True)
        self.assertEqual(len(endpoint.requests), 1)
        self.assertIn("stop=permanent", log.getvalue())
        self.assert_redacted(log)


if __name__ == "__main__":
    unittest.main()
