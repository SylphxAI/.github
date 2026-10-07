#!/usr/bin/env python3
"""run-store: a job hands a file or directory to another job of the same run
through the BuildCache gateway's Turbo door, never GitHub artifact storage.
Every test uses fake values and one local server on 127.0.0.1 that plays the
GitHub OIDC issuer and the gateway."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / ".github" / "actions" / "run-store" / "run-store.sh"

OIDC_JWT = "fake-oidc-jwt.aaa.bbb"
OIDC_REQUEST_TOKEN = "fake-oidc-request-token"
MINTED = "fake-minted.run.token"
SECRETS = (OIDC_JWT, OIDC_REQUEST_TOKEN, MINTED)


class Fake:
    """The OIDC issuer (GET /oidc), the token exchange (POST /v1/tokens/github)
    and the Turbo door (/v8/artifacts/<key>) over an in-memory store."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.requests: list[tuple[str, str, str | None]] = []
        self.put_mode = "ok"  # ok | dropped | exists | full
        self.read_misses = 0  # answer this many GETs 404 before serving
        self.minted = MINTED  # the run token the exchange answers with
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def _reply(self, code: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _key(self) -> str:
                return self.path.split("/v8/artifacts/", 1)[1].split("?", 1)[0]

            def do_GET(self) -> None:
                owner.requests.append(("GET", self.path, self.headers.get("Authorization")))
                if self.path.startswith("/oidc"):
                    assert "audience=sylphx-build-cache" in self.path
                    self._reply(200, json.dumps({"value": OIDC_JWT}).encode())
                    return
                if self.headers.get("Authorization") != f"Bearer {MINTED}":
                    self._reply(401)
                    return
                key = self._key()
                if owner.read_misses > 0:
                    owner.read_misses -= 1
                    self._reply(404)
                elif key in owner.store:
                    self._reply(200, owner.store[key])
                else:
                    self._reply(404)

            def do_HEAD(self) -> None:
                owner.requests.append(("HEAD", self.path, self.headers.get("Authorization")))
                self._reply(200 if self._key() in owner.store else 404)

            def do_POST(self) -> None:
                n = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(n)
                owner.requests.append(("POST", self.path, self.headers.get("Authorization")))
                assert self.headers.get("Authorization") == f"Bearer {OIDC_JWT}"
                base = f"http://127.0.0.1:{owner.port}"
                self._reply(
                    200,
                    json.dumps(
                        {
                            "token": owner.minted,
                            "env": {"TURBO_API": base, "TURBO_TOKEN": owner.minted, "TURBO_TEAM": "ci"},
                        }
                    ).encode(),
                )

            def do_PUT(self) -> None:
                owner.requests.append(("PUT", self.path, self.headers.get("Authorization")))
                n = int(self.headers["Content-Length"])
                body = self.rfile.read(n)
                key = self._key()
                if owner.put_mode == "dropped":
                    self._reply(202, b'{"urls":[]}', {"x-build-cache": "dropped"})
                elif owner.put_mode == "exists":
                    owner.store.setdefault(key, b"")
                    self._reply(409, b'{"error":{}}')
                elif owner.put_mode == "full":
                    self._reply(507, b'{"error":{}}')
                else:
                    owner.store[key] = body
                    self._reply(202, b'{"urls":[]}')

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class RunStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = Fake()
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)

    def tearDown(self) -> None:
        self.fake.close()
        self.tmp.cleanup()

    def run_store(self, mode: str, name: str, path: pathlib.Path, **extra: str) -> tuple[int, str, dict[str, str]]:
        output = self.dir / f"out-{len(self.fake.requests)}-{mode}"
        output.write_text("")
        env = {
            "PATH": os.environ["PATH"],
            "MODE": mode,
            "NAME": name,
            "STORE_PATH": str(path),
            "STORE_RUN_ID": "4242",
            "REPO_ID": "77",
            "REQUIRED": "true",
            "BUILD_CACHE_URL": f"http://127.0.0.1:{self.fake.port}",
            "BUILD_CACHE_NETWORK": "public",
            "ACTIONS_ID_TOKEN_REQUEST_URL": f"http://127.0.0.1:{self.fake.port}/oidc?x=1",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": OIDC_REQUEST_TOKEN,
            "GITHUB_OUTPUT": str(output),
            "RUNNER_TEMP": str(self.dir),
            "RUN_STORE_RETRY_DELAY": "0",
        }
        env.update(extra)
        p = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
        log = p.stdout + p.stderr
        for secret in SECRETS:
            # Only the mask commands may carry a secret.
            for line in log.splitlines():
                if secret in line:
                    self.assertTrue(line.startswith("::add-mask::"), line)
        outputs = dict(line.split("=", 1) for line in output.read_text().splitlines() if "=" in line)
        return p.returncode, log, outputs

    def test_directory_round_trip(self) -> None:
        src = self.dir / "venue"
        (src / "sub").mkdir(parents=True)
        (src / "room.glb").write_bytes(os.urandom(4096))
        (src / "sub" / "lightmap.ktx2").write_text("lm")
        rc, log, out = self.run_store("put", "venue-assets", src)
        self.assertEqual(rc, 0, log)
        self.assertEqual(out["key"], "run-store.v1.77.4242.venue-assets")
        self.assertIn("run-store.v1.77.4242.venue-assets", self.fake.store)
        self.assertIn(("HEAD", "/v8/artifacts/run-store.v1.77.4242.venue-assets", f"Bearer {MINTED}"), self.fake.requests)

        dest = self.dir / "dest"
        rc, log, out = self.run_store("get", "venue-assets", dest)
        self.assertEqual(rc, 0, log)
        self.assertEqual(out["found"], "true")
        self.assertEqual((dest / "room.glb").read_bytes(), (src / "room.glb").read_bytes())
        self.assertEqual((dest / "sub" / "lightmap.ktx2").read_text(), "lm")

    def test_file_lands_under_its_name(self) -> None:
        src = self.dir / "app.apk"
        src.write_bytes(b"apk")
        self.assertEqual(self.run_store("put", "apk", src)[0], 0)
        dest = self.dir / "dl"
        rc, log, _ = self.run_store("get", "apk", dest)
        self.assertEqual(rc, 0, log)
        self.assertEqual((dest / "app.apk").read_bytes(), b"apk")

    def test_get_from_another_run(self) -> None:
        src = self.dir / "web"
        src.mkdir()
        (src / "index.html").write_text("hi")
        self.assertEqual(self.run_store("put", "web-dist", src, STORE_RUN_ID="99")[0], 0)
        rc, log, out = self.run_store("get", "web-dist", self.dir / "d", STORE_RUN_ID="99")
        self.assertEqual(rc, 0, log)
        self.assertEqual(out["key"], "run-store.v1.77.99.web-dist")

    def test_required_miss_fails(self) -> None:
        rc, log, out = self.run_store("get", "nothing", self.dir / "d")
        self.assertEqual(rc, 1)
        self.assertIn("::error::run-store get nothing: no entry run-store.v1.77.4242.nothing", log)
        self.assertEqual(out["found"], "false")

    def test_optional_miss_reports_not_found(self) -> None:
        rc, log, out = self.run_store("get", "nothing", self.dir / "d", REQUIRED="false")
        self.assertEqual(rc, 0, log)
        self.assertEqual(out["found"], "false")
        self.assertIn("::warning::", log)

    def test_transient_miss_is_retried(self) -> None:
        src = self.dir / "f.txt"
        src.write_text("x")
        self.assertEqual(self.run_store("put", "f", src)[0], 0)
        self.fake.read_misses = 2
        rc, log, out = self.run_store("get", "f", self.dir / "d")
        self.assertEqual(rc, 0, log)
        self.assertEqual(out["found"], "true")

    def test_dropped_write_fails_the_put(self) -> None:
        src = self.dir / "f.txt"
        src.write_text("x")
        self.fake.put_mode = "dropped"
        rc, log, _ = self.run_store("put", "f", src)
        self.assertEqual(rc, 1)
        self.assertIn("dropped the write", log)
        self.assertEqual(sum(1 for r in self.fake.requests if r[0] == "PUT"), 3)

    def test_best_effort_put_only_warns(self) -> None:
        src = self.dir / "f.txt"
        src.write_text("x")
        self.fake.put_mode = "full"
        rc, log, _ = self.run_store("put", "f", src, REQUIRED="false")
        self.assertEqual(rc, 0, log)
        self.assertIn("::warning::run-store put f: could not store the bundle (HTTP 507)", log)
        self.assertEqual(sum(1 for r in self.fake.requests if r[0] == "PUT"), 1)

    def test_protected_rerun_keeps_the_first_entry(self) -> None:
        src = self.dir / "f.txt"
        src.write_text("x")
        self.fake.put_mode = "exists"
        rc, log, _ = self.run_store("put", "f", src)
        self.assertEqual(rc, 0, log)
        self.assertIn("::notice::", log)

    def test_no_oidc_fails_without_retry(self) -> None:
        src = self.dir / "f.txt"
        src.write_text("x")
        rc, log, _ = self.run_store("put", "f", src, ACTIONS_ID_TOKEN_REQUEST_URL="")
        self.assertEqual(rc, 1)
        self.assertIn("id-token: write", log)
        self.assertEqual(self.fake.requests, [])

    def test_oversize_bundle_is_refused_before_upload(self) -> None:
        src = self.dir / "big.bin"
        src.write_bytes(os.urandom(8192))
        rc, log, _ = self.run_store("put", "big", src, RUN_STORE_MAX_BYTES="1000")
        self.assertEqual(rc, 1)
        self.assertIn("over the gateway's 1000", log)
        self.assertFalse(any(r[0] == "PUT" for r in self.fake.requests))

    def test_bad_name_is_refused(self) -> None:
        rc, log, _ = self.run_store("put", "a/b", self.dir)
        self.assertEqual(rc, 1)
        self.assertIn("name must be", log)

    def test_minted_token_over_8192_is_refused(self) -> None:
        self.fake.minted = "t" * 8193
        src = self.dir / "f.txt"
        src.write_text("x")
        rc, log, _ = self.run_store("put", "f", src)
        self.assertEqual(rc, 1)
        self.assertIn("token exchange: invalid response", log)


if __name__ == "__main__":
    unittest.main()
