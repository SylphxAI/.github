#!/usr/bin/env python3
"""The rust-sccache backend order: BuildCache (a short-lived run token the
gateway mints for the job's GitHub OIDC identity), then the organization's own
cache user, then the GitHub Actions cache; never the platform's ci-sccache
bucket. Every test uses fake values and local servers on 127.0.0.1."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ACTION = pathlib.Path(__file__).resolve().parents[1] / ".github" / "actions" / "rust-sccache"
BACKEND = ACTION / "backend.sh"
START = ACTION / "start.sh"

OIDC_JWT = "fake-oidc-jwt.aaa.bbb"
OIDC_REQUEST_TOKEN = "fake-oidc-request-token"
MINTED = "fake-minted.run.token"
SECRETS = (OIDC_JWT, OIDC_REQUEST_TOKEN, MINTED)

STATIC = {"S3_ACCESS_KEY": "STATICKEY", "S3_SECRET_KEY": "static-secret", "S3_ENDPOINT": "http://127.0.0.1:1"}
FIELDS = [
    "backend",
    "SCCACHE_BUCKET",
    "SCCACHE_S3_KEY_PREFIX",
    "AWS_ACCESS_KEY_ID",
    "AWS_SESSION_TOKEN",
    "SCCACHE_GHA_ENABLED",
    "SCCACHE_WEBDAV_ENDPOINT",
    "SCCACHE_WEBDAV_TOKEN",
    "SCCACHE_WEBDAV_KEY_PREFIX",
    "SCCACHE_IGNORE_SERVER_IO_ERROR",
]


class Fake:
    """A local OIDC issuer and BuildCache gateway on one port."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.mode = "ok"
        self.endpoint = "http://127.0.0.1:9/webdav"
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def _reply(self, code: int, body: str) -> None:
                data = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except OSError:  # the client gave up (the timeout cases)
                    pass

            def _record(self) -> bytes:
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                owner.requests.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "auth": self.headers.get("Authorization"),
                        "ctype": self.headers.get("Content-Type"),
                        "body": body.decode(),
                    }
                )
                return body

            def do_GET(self) -> None:  # the OIDC issuer
                self._record()
                if owner.mode == "oidc-401":
                    self._reply(401, '{"message":"nope"}')
                else:
                    self._reply(200, json.dumps({"value": OIDC_JWT}))

            def do_POST(self) -> None:  # the gateway
                self._record()
                m = owner.mode
                if m == "ok":
                    self._reply(
                        200,
                        json.dumps(
                            {
                                "token": MINTED,
                                "env": {
                                    "SCCACHE_WEBDAV_ENDPOINT": owner.endpoint,
                                    "SCCACHE_WEBDAV_TOKEN": MINTED,
                                    "SCCACHE_IGNORE_SERVER_IO_ERROR": "1",
                                    "TURBO_TOKEN": MINTED,
                                },
                            }
                        ),
                    )
                elif m == "404":
                    self._reply(404, '{"code":"NOT_FOUND","message":"leaky-body-' + MINTED + '"}')
                elif m == "503":
                    self._reply(503, '{"code":"UNAVAILABLE","message":"leaky-body-' + MINTED + '"}')
                elif m == "bad-json":
                    self._reply(200, "<html>leaky-body-" + MINTED + "</html>")
                elif m == "no-token":
                    self._reply(200, '{"env":{"SCCACHE_WEBDAV_ENDPOINT":"' + owner.endpoint + '"}}')
                elif m == "timeout":
                    time.sleep(3)
                    self._reply(200, "{}")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def oidc_env(self) -> dict[str, str]:
        return {
            "ACTIONS_ID_TOKEN_REQUEST_URL": f"{self.base}/oidc?api-version=2.0",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": OIDC_REQUEST_TOKEN,
            "BUILD_CACHE_URL": self.base,
            "BUILD_CACHE_MAX_TIME": "1",
        }

    def gateway_calls(self) -> list[dict]:
        return [r for r in self.requests if r["method"] == "POST"]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def base_env() -> dict[str, str]:
    return {
        "PATH": os.environ["PATH"],
        "KEY_PREFIX": "org-a/repo-a",
        "GITHUB_REPOSITORY_OWNER": "Org-A",
        # A job's own AWS_* must not leak into the static backend's server.
        "AWS_SESSION_TOKEN": "",
    }


def choose(env: dict[str, str]) -> tuple[dict[str, str], str]:
    script = f'. "{BACKEND}"\n' + "".join(f'printf "%s=%s\\n" {f} "${{{f}:-}}"\n' for f in FIELDS)
    out = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        env={**base_env(), **env},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    fields = dict(line.split("=", 1) for line in out.splitlines() if "=" in line and not line.startswith("::"))
    return fields, out


class RustSccacheBackend(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = Fake()
        self.addCleanup(self.fake.close)

    def assertNoSecrets(self, text: str) -> None:
        for s in SECRETS:
            self.assertNotIn(s, text.replace(f"::add-mask::{s}", ""))
        self.assertNotIn("leaky-body", text)

    # --- BuildCache first ---------------------------------------------------

    def test_buildcache_comes_first(self) -> None:
        got, out = choose({**self.fake.oidc_env(), **STATIC})
        self.assertEqual(got["backend"], "buildcache")
        self.assertEqual(got["SCCACHE_WEBDAV_ENDPOINT"], self.fake.endpoint)
        self.assertEqual(got["SCCACHE_WEBDAV_TOKEN"], MINTED)
        self.assertEqual(got["SCCACHE_WEBDAV_KEY_PREFIX"], "org-a/repo-a")
        self.assertEqual(got["SCCACHE_IGNORE_SERVER_IO_ERROR"], "1")
        # No other backend's settings ride along.
        self.assertEqual(got["SCCACHE_BUCKET"], "")
        self.assertEqual(got["AWS_ACCESS_KEY_ID"], "")
        self.assertEqual(got["SCCACHE_GHA_ENABLED"], "")
        self.assertNoSecrets(out.split("backend=")[0])

    def test_exchange_follows_the_contract(self) -> None:
        choose(self.fake.oidc_env())
        oidc = next(r for r in self.fake.requests if r["method"] == "GET")
        self.assertEqual(oidc["auth"], f"Bearer {OIDC_REQUEST_TOKEN}")
        self.assertTrue(oidc["path"].startswith("/oidc?api-version=2.0&audience=sylphx-build-cache"), oidc["path"])
        (call,) = self.fake.gateway_calls()
        self.assertEqual(call["path"], "/v1/tokens/github")
        self.assertEqual(call["auth"], f"Bearer {OIDC_JWT}")
        self.assertEqual(call["ctype"], "application/json")
        self.assertEqual(json.loads(call["body"]), {"ttl_seconds": 22500, "network": "public"})

    def test_cluster_network_is_requested(self) -> None:
        choose({**self.fake.oidc_env(), "BUILD_CACHE_NETWORK": "cluster"})
        self.assertEqual(json.loads(self.fake.gateway_calls()[0]["body"])["network"], "cluster")

    def test_both_tokens_are_masked_before_any_other_output(self) -> None:
        _, out = choose(self.fake.oidc_env())
        lines = out.splitlines()
        for s in (OIDC_REQUEST_TOKEN, OIDC_JWT, MINTED):
            self.assertIn(f"::add-mask::{s}", lines)
        masks = [i for i, l in enumerate(lines) if l.startswith("::add-mask::")]
        others = [i for i, l in enumerate(lines) if not l.startswith("::add-mask::")]
        self.assertLess(max(masks), min(others))
        self.assertNoSecrets(out.split("backend=")[0])

    def test_nothing_is_written_to_github_env_or_output(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            files = {n: pathlib.Path(d, n) for n in ("env", "output", "summary")}
            for p in files.values():
                p.touch()
            choose(
                {
                    **self.fake.oidc_env(),
                    "GITHUB_ENV": str(files["env"]),
                    "GITHUB_OUTPUT": str(files["output"]),
                    "GITHUB_STEP_SUMMARY": str(files["summary"]),
                }
            )
            for p in files.values():
                self.assertEqual(p.read_text(), "", p.name)

    # --- fallbacks ---------------------------------------------------------

    def test_no_id_token_permission_falls_to_static(self) -> None:
        got, out = choose(STATIC)
        self.assertEqual(got["backend"], "static")
        self.assertEqual(got["SCCACHE_BUCKET"], "ci-sccache-org-a")
        self.assertEqual(got["AWS_ACCESS_KEY_ID"], "STATICKEY")
        self.assertEqual(got["SCCACHE_WEBDAV_TOKEN"], "")
        self.assertIn("::warning::", out)
        self.assertIn("id-token", out)
        self.assertEqual(self.fake.requests, [])

    def test_no_id_token_permission_and_no_pair_falls_to_gha(self) -> None:
        got, _ = choose({})
        self.assertEqual(got["backend"], "gha")
        self.assertEqual(got["SCCACHE_GHA_ENABLED"], "true")

    def test_only_the_request_url_is_not_enough(self) -> None:
        env = self.fake.oidc_env()
        del env["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]
        got, _ = choose({**env, **STATIC})
        self.assertEqual(got["backend"], "static")
        self.assertEqual(self.fake.requests, [])

    def _falls_back(self, mode: str, reason: str) -> None:
        self.fake.mode = mode
        got, out = choose({**self.fake.oidc_env(), **STATIC})
        self.assertEqual(got["backend"], "static", mode)
        self.assertEqual(got["SCCACHE_WEBDAV_TOKEN"], "", mode)
        self.assertEqual(got["SCCACHE_WEBDAV_ENDPOINT"], "", mode)
        warnings = [l for l in out.splitlines() if l.startswith("::warning::")]
        self.assertEqual(len(warnings), 1, (mode, warnings))
        self.assertIn(reason, warnings[0], mode)
        self.assertNoSecrets(out)
        # Without the static pair the Actions cache is next.
        got, _ = choose(self.fake.oidc_env())
        self.assertEqual(got["backend"], "gha", mode)

    def test_gateway_404_falls_back(self) -> None:
        self._falls_back("404", "HTTP 404")

    def test_gateway_503_falls_back(self) -> None:
        self._falls_back("503", "HTTP 503")

    def test_gateway_timeout_falls_back(self) -> None:
        self._falls_back("timeout", "curl exit 28")

    def test_gateway_invalid_json_falls_back(self) -> None:
        self._falls_back("bad-json", "invalid response")

    def test_gateway_reply_without_a_token_falls_back(self) -> None:
        self._falls_back("no-token", "invalid response")

    def test_oidc_refusal_falls_back_without_calling_the_gateway(self) -> None:
        self.fake.mode = "oidc-401"
        got, out = choose({**self.fake.oidc_env(), **STATIC})
        self.assertEqual(got["backend"], "static")
        self.assertIn("HTTP 401", out)
        self.assertEqual(self.fake.gateway_calls(), [])
        self.assertNoSecrets(out)

    def test_unreachable_gateway_falls_back(self) -> None:
        env = {**self.fake.oidc_env(), "BUILD_CACHE_URL": "http://127.0.0.1:1"}
        got, out = choose({**env, **STATIC})
        self.assertEqual(got["backend"], "static")
        self.assertIn("curl exit 7", out)
        self.assertNoSecrets(out)

    def test_build_cache_false_skips_it(self) -> None:
        got, out = choose({**self.fake.oidc_env(), **STATIC, "BUILD_CACHE": "false"})
        self.assertEqual(got["backend"], "static")
        self.assertEqual(self.fake.requests, [])
        self.assertNotIn("::warning::", out)

    def test_a_skipped_backend_is_not_tried_again(self) -> None:
        got, _ = choose({**self.fake.oidc_env(), **STATIC, "SKIP_BACKENDS": "buildcache"})
        self.assertEqual(got["backend"], "static")
        self.assertEqual(self.fake.requests, [])
        got, _ = choose({**STATIC, "SKIP_BACKENDS": "buildcache static"})
        self.assertEqual(got["backend"], "gha")
        got, _ = choose({"SKIP_BACKENDS": "buildcache static gha"})
        self.assertEqual(got["backend"], "none")

    # --- hostile inputs ----------------------------------------------------

    def test_shell_metacharacters_in_inputs_are_refused_or_inert(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            marker = pathlib.Path(d, "pwned")
            for field, bad in (
                ("BUILD_CACHE_URL", f"{self.fake.base}/$(touch {marker})"),
                ("BUILD_CACHE_URL", f"{self.fake.base};touch {marker}"),
                ("BUILD_CACHE_URL", f"{self.fake.base}/`touch {marker}`"),
                ("BUILD_CACHE_URL", f"{self.fake.base}/x y"),
                ("BUILD_CACHE_URL", f"{self.fake.base}/\n::add-mask::x"),
                ("BUILD_CACHE_URL", f"file:///etc/passwd"),
                ("BUILD_CACHE_NETWORK", f"public;touch {marker}"),
                ("BUILD_CACHE_NETWORK", 'public","ttl_seconds":1'),
                ("BUILD_CACHE_NETWORK", ""),
            ):
                self.fake.requests.clear()
                got, out = choose({**self.fake.oidc_env(), **STATIC, field: bad})
                self.assertEqual(got["backend"], "static", (field, bad))
                self.assertFalse(marker.exists(), (field, bad))
                self.assertEqual(self.fake.gateway_calls(), [], (field, bad))
                self.assertEqual(len([l for l in out.splitlines() if l.startswith("::warning::")]), 1, (field, bad))

    def test_key_prefix_is_inert(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            marker = pathlib.Path(d, "pwned")
            nasty = f"a$(touch {marker});`touch {marker}` b"
            got, _ = choose({**self.fake.oidc_env(), "KEY_PREFIX": nasty})
            self.assertEqual(got["backend"], "buildcache")
            self.assertEqual(got["SCCACHE_WEBDAV_KEY_PREFIX"], nasty)
            self.assertFalse(marker.exists())

    def test_the_gateway_cannot_inject_through_its_reply(self) -> None:
        self.fake.endpoint = "http://127.0.0.1:9/x\n::set-env name=A::b"
        got, out = choose({**self.fake.oidc_env(), **STATIC})
        self.assertEqual(got["backend"], "static")
        self.assertNotIn("::set-env", out)

    # --- the old slots -----------------------------------------------------

    def test_runner_carried_credential_is_gone(self) -> None:
        org = {
            "SYLPHX_SCCACHE_ACCESS_KEY_ID": "ORGKEY",
            "SYLPHX_SCCACHE_SECRET_ACCESS_KEY": "org-secret",
            "SYLPHX_SCCACHE_SESSION_TOKEN": "org-token",
            "SYLPHX_SCCACHE_BUCKET": "ci-org-cache",
            "SYLPHX_SCCACHE_ENDPOINT": "http://rgw.example:80",
            "SYLPHX_SCCACHE_KEY_PREFIX": "sccache/1001",
        }
        got, _ = choose({**org, **STATIC})
        self.assertEqual(got["backend"], "static")
        self.assertEqual(got["AWS_ACCESS_KEY_ID"], "STATICKEY")
        got, _ = choose(org)
        self.assertEqual(got["backend"], "gha")

    def test_actions_cache_can_be_left_to_the_caller(self) -> None:
        self.assertEqual(choose({"ACTIONS_CACHE": "false"})[0]["backend"], "none")

    def test_never_the_platform_ci_sccache_bucket(self) -> None:
        got, _ = choose({**STATIC, "S3_BUCKET": "ci-sccache"})
        self.assertNotEqual(got["SCCACHE_BUCKET"], "ci-sccache")
        self.assertEqual(got["backend"], "gha")

    def test_the_action_documents_the_new_backends_only(self) -> None:
        text = (ACTION / "action.yml").read_text()
        self.assertNotIn("SYLPHX_SCCACHE", text)
        self.assertNotIn("SYLPHX_SCCACHE", BACKEND.read_text())
        self.assertIn("buildcache|static|gha|none", text)


class RustSccacheStart(unittest.TestCase):
    """start.sh: pick a backend, start the server, fall through when it will
    not start, and keep the run token out of everything the job inherits."""

    def setUp(self) -> None:
        self.fake = Fake()
        self.addCleanup(self.fake.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = pathlib.Path(self.tmp.name)
        self.bin = d / "bin"
        self.bin.mkdir()
        self.log = d / "sccache.log"
        sccache = self.bin / "sccache"
        sccache.write_text(
            "#!/bin/sh\n"
            'case "$1" in\n'
            "  --start-server)\n"
            '    kind=gha; [ -n "$SCCACHE_BUCKET" ] && kind=static; [ -n "$SCCACHE_WEBDAV_ENDPOINT" ] && kind=buildcache\n'
            f'    echo "start $kind webdav_token=$([ -n "$SCCACHE_WEBDAV_TOKEN" ] && echo yes || echo no)'
            ' bucket=${SCCACHE_BUCKET:-} gha=${SCCACHE_GHA_ENABLED:-}" >> "$FAKE_LOG"\n'
            '    case " $FAKE_FAIL " in *" $kind "*) exit 1;; esac\n'
            "    exit 0;;\n"
            '  --stop-server) echo stop >> "$FAKE_LOG"; exit 0;;\n'
            "esac\n"
        )
        sccache.chmod(0o755)
        self.out = {n: d / n for n in ("env", "output", "summary")}
        for p in self.out.values():
            p.touch()

    def start(self, env: dict[str, str], fail: str = "") -> subprocess.CompletedProcess:
        full = {
            **base_env(),
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "GITHUB_ACTION_PATH": str(ACTION),
            "GITHUB_ENV": str(self.out["env"]),
            "GITHUB_OUTPUT": str(self.out["output"]),
            "GITHUB_STEP_SUMMARY": str(self.out["summary"]),
            "FAKE_LOG": str(self.log),
            "FAKE_FAIL": fail,
            **env,
        }
        return subprocess.run(
            ["bash", "-euo", "pipefail", "-c", f'. "{START}"'], env=full, capture_output=True, text=True, check=True
        )

    def read(self, name: str) -> str:
        return self.out[name].read_text()

    def test_buildcache_runs_and_the_token_stays_in_the_server(self) -> None:
        res = self.start(self.fake.oidc_env())
        self.assertIn("backend=buildcache", self.read("output"))
        self.assertIn("webdav_token=yes", self.log.read_text())
        self.assertIn("RUSTC_WRAPPER=sccache", self.read("env"))
        for name in ("env", "output", "summary"):
            for s in SECRETS:
                self.assertNotIn(s, self.read(name), name)
        self.assertNotIn("WEBDAV", self.read("env"))
        self.assertIn("buildcache", self.read("summary"))
        for s in SECRETS:
            self.assertNotIn(s, res.stderr)
            self.assertNotIn(s, res.stdout.replace(f"::add-mask::{s}", ""))

    def test_server_that_will_not_start_on_buildcache_falls_to_static(self) -> None:
        res = self.start({**self.fake.oidc_env(), **STATIC}, fail="buildcache")
        self.assertIn("backend=static", self.read("output"))
        log = self.log.read_text()
        self.assertIn("start buildcache webdav_token=yes", log)
        self.assertIn("start static webdav_token=no", log)
        self.assertIn("AWS_ACCESS_KEY_ID=STATICKEY", self.read("env"))
        self.assertNotIn(MINTED, self.read("env"))
        self.assertIn("::warning::", res.stdout)
        self.assertIn("static", self.read("summary"))

    def test_static_that_will_not_start_falls_to_gha_without_s3_settings(self) -> None:
        self.start({**STATIC}, fail="static")
        self.assertIn("backend=gha", self.read("output"))
        self.assertIn("start gha webdav_token=no bucket= gha=true", self.log.read_text())
        self.assertNotIn("AWS_ACCESS_KEY_ID", self.read("env"))
        self.assertIn("SCCACHE_GHA_ENABLED=true", self.read("env"))

    def test_nothing_starts_ends_on_none_without_a_wrapper(self) -> None:
        self.start({**self.fake.oidc_env(), **STATIC}, fail="buildcache static gha")
        self.assertIn("backend=none", self.read("output"))
        self.assertNotIn("RUSTC_WRAPPER", self.read("env"))
        self.assertIn("none", self.read("summary"))

    def test_no_backend_at_all_is_none(self) -> None:
        self.start({"ACTIONS_CACHE": "false"})
        self.assertIn("backend=none", self.read("output"))
        self.assertEqual(self.log.exists(), False)

    def test_action_never_exports_the_webdav_settings_to_the_job(self) -> None:
        for path in (ACTION / "action.yml", START):
            for line in path.read_text().splitlines():
                if "GITHUB_ENV" in line:
                    self.assertNotIn("WEBDAV", line, path.name)

    def test_static_applies_expiry_on_every_run_and_retires_the_repo_prefix(self) -> None:
        curl = self.bin / "curl"
        curl.write_text(
            "#!/bin/sh\n"
            'echo "$*" >> "$FAKE_CURL"\n'
            'case "$*" in *-I*) printf 200;; esac\n'
            "exit 0\n"
        )
        curl.chmod(0o755)
        calls = pathlib.Path(self.tmp.name) / "curl.log"
        self.start({**STATIC, "GITHUB_REPOSITORY_OWNER": "AcmeOrg", "FAKE_CURL": str(calls)})
        text = calls.read_text()
        # The bucket exists (HEAD answered 200), so it is not created again ...
        self.assertNotIn("-X PUT http://127.0.0.1:1/ci-sccache-acmeorg -o", text)
        # ... but its expiry is still written: the cache namespace after 14 days,
        # the retired `<owner>/` namespace after 1.
        life = [l for l in text.splitlines() if "?lifecycle" in l]
        self.assertEqual(len(life), 1)
        self.assertIn("<Prefix>org-a/repo-a/</Prefix>", life[0])
        self.assertIn("<Days>14</Days>", life[0])
        self.assertIn("<Prefix>AcmeOrg/</Prefix>", life[0])
        self.assertIn("<Days>1</Days>", life[0])


if __name__ == "__main__":
    unittest.main()
