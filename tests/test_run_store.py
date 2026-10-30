#!/usr/bin/env python3
"""run-store: a job hands a file or directory to another job of the same run
through the BuildCache gateway's Turbo door, never GitHub artifact storage.
Every test uses fake values and one local server on 127.0.0.1 that plays the
GitHub OIDC issuer and the gateway."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import pathlib
import subprocess
import tarfile
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / ".github" / "actions" / "run-store" / "run-store.sh"

OIDC_JWT = "fake-oidc-jwt.aaa.bbb"
OIDC_REQUEST_TOKEN = "fake-oidc-request-token"
MINTED = "fake-minted.run.token"
SECRETS = (OIDC_JWT, OIDC_REQUEST_TOKEN, MINTED)


def bundle(members: list[tuple[tarfile.TarInfo, bytes | None]]) -> bytes:
    """A gzip tar bundle, as run-store put stores it, from explicit members so a
    test can name one a put would never produce (traversal, link, special)."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for member, data in members:
            tar.addfile(member, io.BytesIO(data) if data is not None else None)
    return gzip.compress(raw.getvalue())


def regular(name: str, data: bytes) -> tuple[tarfile.TarInfo, bytes]:
    member = tarfile.TarInfo(name)
    member.size = len(data)
    member.mode = 0o644
    return member, data


def special(name: str, kind: bytes, linkname: str = "") -> tuple[tarfile.TarInfo, None]:
    member = tarfile.TarInfo(name)
    member.type = kind
    member.linkname = linkname
    member.mode = 0o755 if kind == tarfile.DIRTYPE else 0o644
    return member, None


class Fake:
    """The OIDC issuer (GET /oidc), the token exchange (POST /v1/tokens/github)
    and the Turbo door (/v8/artifacts/<key>) over an in-memory store."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.requests: list[tuple[str, str, str | None]] = []
        self.put_mode = "ok"  # ok | dropped | exists | full
        self.failure_headers: dict[str, str] = {}
        self.failed_part: str | None = None
        self.protected = False
        self.put_sizes: list[int] = []
        self.read_misses = 0  # answer this many GETs 404 before serving
        self.head_statuses: list[int] = []
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
                self._reply(owner.head_statuses.pop(0) if owner.head_statuses else 200 if self._key() in owner.store else 404, headers={"x-build-cache-scope": "protected" if owner.protected else "dev"})

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
                owner.put_sizes.append(n)
                key = self._key()
                if owner.failed_part and key.endswith(owner.failed_part):
                    self._reply(413, b"body rejected", owner.failure_headers)
                    return
                if owner.protected and key in owner.store:
                    self._reply(409)
                    return
                if owner.put_mode == "dropped":
                    self._reply(202, b'{"urls":[]}', {"x-build-cache": "dropped"})
                elif owner.put_mode == "exists":
                    owner.store.setdefault(key, b"")
                    self._reply(409, b'{"error":{}}')
                elif owner.put_mode == "full":
                    self._reply(507, b'{"error":{}}')
                elif owner.put_mode == "too_large":
                    self._reply(413, b"private response body: " + MINTED.encode(), owner.failure_headers)
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
            # setup-python's shared-library build needs its loader path on
            # self-hosted runners too; keep the fixture's credentials isolated.
            "LD_LIBRARY_PATH": os.environ.get("LD_LIBRARY_PATH", ""),
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

    def test_refused_upload_records_bytes_and_only_safe_response_id(self) -> None:
        src = self.dir / "upload.bin"
        src.write_bytes(os.urandom(512))
        self.fake.put_mode = "too_large"
        self.fake.failure_headers = {"cf-ray": "a5275677b84d2bcd-FRA"}
        rc, log, out = self.run_store("put", "upload", src)
        self.assertNotEqual(rc, 0)
        self.assertIn("packed_bytes=" + out["bytes"], log)
        self.assertIn("HTTP 413", log)
        self.assertIn("response_id=cf-ray:a5275677b84d2bcd-FRA", log)
        self.assertNotIn("private response body", log)
        self.assertEqual(sum(method == "PUT" for method, _, _ in self.fake.requests), 1)

    def test_response_diagnostics_refuse_arbitrary_header_content(self) -> None:
        src = self.dir / "upload.bin"
        src.write_bytes(b"fixture")
        self.fake.put_mode = "too_large"
        self.fake.failure_headers = {"x-request-id": MINTED, "cf-ray": "Bearer " + MINTED}
        rc, log, _ = self.run_store("put", "upload", src)
        self.assertNotEqual(rc, 0)
        self.assertIn("response_id=unavailable", log)
        self.assertNotIn("private response body", log)

    def test_parts_manifest_last_roundtrip_below_each_request_limit(self) -> None:
        src = self.dir / "parts.bin"
        src.write_bytes(os.urandom(4096))
        rc, log, _ = self.run_store("put", "parts", src, RUN_STORE_PART_BYTES="1024")
        self.assertEqual(rc, 0, log)
        key = "run-store.v1.77.4242.parts"
        self.assertTrue(self.fake.store[key].startswith(b"ci-run-store-parts-v1\n"))
        self.assertTrue(all(size <= 1024 for size in self.fake.put_sizes))
        puts = [path for method, path, _ in self.fake.requests if method == "PUT"]
        self.assertEqual(puts[-1], "/v8/artifacts/" + key)
        dest = self.dir / "parts-out"
        rc, log, out = self.run_store("get", "parts", dest, RUN_STORE_PART_BYTES="1024")
        self.assertEqual(rc, 0, log)
        self.assertEqual(out["found"], "true")
        self.assertEqual((dest / src.name).read_bytes(), src.read_bytes())

    def test_large_repeats_replace_dev_and_preserve_protected_root(self) -> None:
        src = self.dir / "parts.bin"
        src.write_bytes(os.urandom(4096))
        rc, log, _ = self.run_store("put", "parts", src, RUN_STORE_PART_BYTES="1024")
        self.assertEqual(rc, 0, log)
        src.write_bytes(os.urandom(4096))
        rc, log, _ = self.run_store("put", "parts", src, RUN_STORE_PART_BYTES="1024")
        self.assertEqual(rc, 0, log)
        dest = self.dir / "dev-repeat-out"
        rc, log, _ = self.run_store("get", "parts", dest, RUN_STORE_PART_BYTES="1024")
        self.assertEqual(rc, 0, log)
        self.assertEqual((dest / src.name).read_bytes(), src.read_bytes())
        self.fake.protected = True
        retained = dict(self.fake.store)
        puts = len(self.fake.put_sizes)
        src.write_bytes(os.urandom(4096))
        rc, log, _ = self.run_store("put", "parts", src, RUN_STORE_PART_BYTES="1024")
        self.assertEqual(rc, 0, log)
        self.assertEqual(self.fake.store, retained)
        self.assertEqual(len(self.fake.put_sizes), puts)

    def test_missing_or_corrupt_part_never_unpacks(self) -> None:
        src = self.dir / "parts.bin"
        src.write_bytes(os.urandom(4096))
        rc, log, _ = self.run_store("put", "parts", src, RUN_STORE_PART_BYTES="1024")
        self.assertEqual(rc, 0, log)
        key = "run-store.v1.77.4242.parts.part-1"
        original = self.fake.store.pop(key)
        dest = self.dir / "missing-out"
        rc, log, out = self.run_store("get", "parts", dest, RUN_STORE_PART_BYTES="1024")
        self.assertNotEqual(rc, 0)
        self.assertEqual(out["found"], "false")
        self.assertFalse(dest.exists())
        self.fake.store[key] = b"x" * len(original)
        dest = self.dir / "corrupt-out"
        rc, log, out = self.run_store("get", "parts", dest, RUN_STORE_PART_BYTES="1024")
        self.assertNotEqual(rc, 0)
        self.assertIn("corrupt parts bundle", log)
        self.assertFalse(dest.exists())

    def test_failed_part_never_commits_root_manifest(self) -> None:
        src = self.dir / "parts.bin"
        src.write_bytes(os.urandom(4096))
        self.fake.failed_part = ".part-1"
        rc, log, _ = self.run_store("put", "parts", src, RUN_STORE_PART_BYTES="1024")
        self.assertNotEqual(rc, 0)
        self.assertNotIn("run-store.v1.77.4242.parts", self.fake.store)
        self.assertIn("request_bytes=1024", log)

    def test_incomplete_immutable_part_collision_fails_before_commit(self) -> None:
        src = self.dir / "parts.bin"
        src.write_bytes(os.urandom(4096))
        self.fake.protected = True
        self.fake.store["run-store.v1.77.4242.parts.part-0"] = b"prior incomplete producer"
        rc, log, _ = self.run_store("put", "parts", src, RUN_STORE_PART_BYTES="1024")
        self.assertNotEqual(rc, 0)
        self.assertIn("immutable part differs", log)
        self.assertNotIn("run-store.v1.77.4242.parts", self.fake.store)

    def test_manifest_bounds_refuse_unbounded_part_fetches(self) -> None:
        key = "run-store.v1.77.4242.invalid"
        self.fake.store[key] = b"ci-run-store-parts-v1\n" + json.dumps({
            "parts": 10001, "size": 1, "sha256": "0" * 64}).encode()
        rc, log, _ = self.run_store("get", "invalid", self.dir / "dest")
        self.assertNotEqual(rc, 0)
        self.assertIn("invalid parts manifest", log)
        self.assertFalse(any(".part-" in path for _, path, _ in self.fake.requests))

    def test_existing_cloud_parts_wire_format_is_readable(self) -> None:
        key = "run-store.v1.77.4242.cloud"
        data = bundle([regular("fixture.txt", b"existing client bytes")])
        self.fake.store[key] = b"ci-run-store-parts-v1\n" + json.dumps({
            "parts": 2, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}).encode() + b"\n"
        self.fake.store[key + ".part-0"] = data[:50]
        self.fake.store[key + ".part-1"] = data[50:]
        dest = self.dir / "cloud-out"
        rc, log, _ = self.run_store("get", "cloud", dest)
        self.assertEqual(rc, 0, log)
        self.assertEqual((dest / "fixture.txt").read_bytes(), b"existing client bytes")

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

    def test_accepted_put_survives_transient_head_503_without_resending_upload(self) -> None:
        src = self.dir / "f.txt"
        src.write_text("x")
        self.fake.head_statuses = [503, 200]
        rc, log, _ = self.run_store("put", "f", src)
        self.assertEqual(rc, 0, log)
        self.assertEqual(sum(r[0] == "PUT" for r in self.fake.requests), 1)
        self.assertEqual(sum(r[0] == "HEAD" for r in self.fake.requests), 2)

    def test_persistent_head_503_fails_after_bounded_reads(self) -> None:
        src = self.dir / "f.txt"
        src.write_text("x")
        self.fake.head_statuses = [503, 503, 503]
        rc, log, out = self.run_store("put", "f", src)
        self.assertEqual(rc, 1, log)
        self.assertIn("store unavailable", log)
        self.assertEqual(out["unavailable"], "true")
        self.assertEqual(sum(r[0] == "PUT" for r in self.fake.requests), 1)
        self.assertEqual(sum(r[0] == "HEAD" for r in self.fake.requests), 3)

    def test_head_auth_failure_is_not_retried(self) -> None:
        src = self.dir / "f.txt"
        src.write_text("x")
        self.fake.head_statuses = [403, 200]
        rc, log, _ = self.run_store("put", "f", src)
        self.assertEqual(rc, 1, log)
        self.assertIn("HTTP 403", log)
        self.assertEqual(sum(r[0] == "PUT" for r in self.fake.requests), 1)
        self.assertEqual(sum(r[0] == "HEAD" for r in self.fake.requests), 1)

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
        self.assertIn("::warning::run-store put f: could not store the bundle (HTTP 507 ", log)
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

    def stage_bundle(self, name: str, blob: bytes) -> None:
        self.fake.store[f"run-store.v1.77.4242.{name}"] = blob

    def test_path_traversal_is_refused_before_writing(self) -> None:
        self.stage_bundle("evil", bundle([regular("../escaped.txt", b"pwn"), regular("bad/r00t.txt", b"pwn")]))
        dest = self.dir / "d"
        rc, log, out = self.run_store("get", "evil", dest)
        self.assertEqual(rc, 1)
        self.assertIn("refusing to unpack run-store.v1.77.4242.evil: unsafe archive member", log)
        self.assertFalse((self.dir / "escaped.txt").exists())
        self.assertFalse(dest.exists())
        self.assertEqual(out["found"], "false")

    def test_symlink_member_is_refused_before_writing(self) -> None:
        self.stage_bundle(
            "link",
            bundle([special("evil", tarfile.SYMTYPE, "/etc"), regular("evil/pwned.txt", b"pwn")]),
        )
        dest = self.dir / "d"
        rc, log, _ = self.run_store("get", "link", dest)
        self.assertEqual(rc, 1)
        self.assertIn("unsafe archive member: symlink 'evil' -> '/etc'", log)
        self.assertFalse(dest.exists())

    def test_hardlink_member_is_refused_before_writing(self) -> None:
        self.stage_bundle(
            "hard",
            bundle([regular("target", b"safe"), special("hard", tarfile.LNKTYPE, "/etc/passwd")]),
        )
        rc, log, _ = self.run_store("get", "hard", self.dir / "d")
        self.assertEqual(rc, 1)
        self.assertIn("unsafe archive member: hardlink 'hard' -> '/etc/passwd'", log)

    def test_absolute_path_is_refused_before_writing(self) -> None:
        self.stage_bundle("abs", bundle([regular("/etc/evil", b"pwn")]))
        rc, log, _ = self.run_store("get", "abs", self.dir / "d")
        self.assertEqual(rc, 1)
        self.assertIn("unsafe archive member: absolute path '/etc/evil'", log)

    def test_special_file_is_refused_before_writing(self) -> None:
        self.stage_bundle("spec", bundle([special("pipe", tarfile.FIFOTYPE)]))
        rc, log, _ = self.run_store("get", "spec", self.dir / "d")
        self.assertEqual(rc, 1)
        self.assertIn("unsafe archive member: special file 'pipe'", log)

    def test_safe_junit_bundle_still_fetches(self) -> None:
        self.stage_bundle(
            "junit",
            bundle([special("results/", tarfile.DIRTYPE), regular("results/junit.xml", b"<testsuite/>")]),
        )
        dest = self.dir / "d"
        rc, log, out = self.run_store("get", "junit", dest)
        self.assertEqual(rc, 0, log)
        self.assertEqual(out["found"], "true")
        self.assertEqual((dest / "results" / "junit.xml").read_bytes(), b"<testsuite/>")


if __name__ == "__main__":
    unittest.main()
