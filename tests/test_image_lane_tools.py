#!/usr/bin/env python3
"""Unit tests for the image lane's credential and evidence tooling.

These are pure-policy tests: no cluster, no registry, no network. They pin the
exact-grant contract (one repository, pull+push, bounded lifetime) that makes
the lane's credential a repository-scoped identity rather than a broad one.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import tempfile
import time
import unittest
import zipfile
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LANE_IMAGE = "registry.sylphx.com/library/sylphx-hands"


def load(module_name: str, relative: str):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


mint = load("image_lane_mint", "scripts/mint-registry-auth.py")
pack = load("image_lane_pack", "scripts/pack-image-evidence.py")
transfer = load("image_lane_transfer", "scripts/resolve-lane-artifact.py")


def jwt(claims: dict) -> str:
    def segment(value: dict) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{segment({'alg': 'ES256'})}.{segment(claims)}.signature"


class SplitImageReferenceTests(unittest.TestCase):
    def test_splits_host_and_repository(self) -> None:
        self.assertEqual(
            mint.split_image_reference(LANE_IMAGE),
            ("registry.sylphx.com", "library/sylphx-hands"),
        )

    def test_rejects_non_canonical_references(self) -> None:
        for bad in [
            "library/sylphx-hands",
            "registry.sylphx.com/",
            "registry.sylphx.com//x",
            "registry.sylphx.com/../x",
            "registry.sylphx.com/x/../y",
            "https://registry.sylphx.com/library/x",
            "registry.sylphx.com/library/x@sha256:" + "0" * 64,
        ]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                mint.split_image_reference(bad)


class RegistryTokenValidationTests(unittest.TestCase):
    def claims(self, grants, iat=None, exp=None, aud=None):
        now = int(time.time())
        return {
            "aud": aud or [mint.REGISTRY_TOKEN_SERVICE],
            "iat": now if iat is None else iat,
            "exp": now + 300 if exp is None else exp,
            "access": grants,
        }

    def test_accepts_exactly_one_repository_grant(self) -> None:
        token = jwt(self.claims([{"type": "repository", "name": "library/sylphx-hands", "actions": ["pull", "push"]}]))
        mint._validate_registry_token(token, int(time.time()), "library/sylphx-hands")

    def test_rejects_a_grant_for_another_repository(self) -> None:
        token = jwt(self.claims([{"type": "repository", "name": "library/other", "actions": ["pull", "push"]}]))
        with self.assertRaises(ValueError):
            mint._validate_registry_token(token, int(time.time()), "library/sylphx-hands")

    def test_rejects_authority_outside_the_requested_repository(self) -> None:
        token = jwt(
            self.claims(
                [
                    {"type": "repository", "name": "library/sylphx-hands", "actions": ["pull", "push"]},
                    {"type": "repository", "name": "library/other", "actions": ["pull", "push"]},
                ]
            )
        )
        with self.assertRaises(ValueError):
            mint._validate_registry_token(token, int(time.time()), "library/sylphx-hands")

    def test_rejects_wide_actions(self) -> None:
        token = jwt(self.claims([{"type": "repository", "name": "library/sylphx-hands", "actions": ["pull", "push", "delete"]}]))
        with self.assertRaises(ValueError):
            mint._validate_registry_token(token, int(time.time()), "library/sylphx-hands")

    def test_rejects_an_expired_token(self) -> None:
        now = int(time.time())
        token = jwt(self.claims([{"type": "repository", "name": "library/sylphx-hands", "actions": ["pull", "push"]}], iat=now - 1200, exp=now - 300))
        with self.assertRaises(ValueError):
            mint._validate_registry_token(token, now, "library/sylphx-hands")

    def test_rejects_an_overlong_token_lifetime(self) -> None:
        now = int(time.time())
        token = jwt(self.claims([{"type": "repository", "name": "library/sylphx-hands", "actions": ["pull", "push"]}], iat=now, exp=now + 4000))
        with self.assertRaises(ValueError):
            mint._validate_registry_token(token, now, "library/sylphx-hands")

    def test_rejects_a_foreign_audience(self) -> None:
        token = jwt(self.claims([{"type": "repository", "name": "library/sylphx-hands", "actions": ["pull", "push"]}], aud=["something-else"]))
        with self.assertRaises(ValueError):
            mint._validate_registry_token(token, int(time.time()), "library/sylphx-hands")


class SvidValidationTests(unittest.TestCase):
    def test_accepts_the_publisher_subject_and_audience(self) -> None:
        now = int(time.time())
        token = jwt({"sub": mint.SPIFFE_ID, "aud": [mint.SPIFFE_AUDIENCE], "iat": now, "exp": now + 300})
        mint._validate_svid(token, now)

    def test_rejects_another_subject(self) -> None:
        now = int(time.time())
        token = jwt({"sub": "spiffe://sylphx.local/role/other", "aud": [mint.SPIFFE_AUDIENCE], "iat": now, "exp": now + 300})
        with self.assertRaises(ValueError):
            mint._validate_svid(token, now)


class GitHubOidcTests(unittest.TestCase):
    def claims(self, **over) -> dict:
        now = int(time.time())
        claims = {"iss": mint.GITHUB_OIDC_ISSUER, "aud": mint.GITHUB_OIDC_AUDIENCE, "iat": now, "exp": now + 300}
        claims.update(over)
        return claims

    def test_accepts_a_github_token_for_the_registry_audience(self) -> None:
        mint._validate_github_oidc(jwt(self.claims()), int(time.time()))

    def test_rejects_another_issuer_audience_or_lifetime(self) -> None:
        now = int(time.time())
        for claims in [
            self.claims(iss="https://example.com"),
            self.claims(aud="sylphx-access"),
            self.claims(exp=now - 1),
            self.claims(exp=now + mint.MAX_GITHUB_OIDC_LIFETIME_SECONDS + 60),
        ]:
            with self.subTest(claims=claims):
                with self.assertRaises(ValueError):
                    mint._validate_github_oidc(jwt(claims), now)

    def test_requests_the_registry_audience_with_the_job_request_token(self) -> None:
        seen = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps({"value": jwt(GitHubOidcTests.claims(GitHubOidcTests()))}).encode()

        def urlopen(request, timeout):
            seen["url"] = request.full_url
            seen["auth"] = request.get_header("Authorization")
            return Response()

        env = {"ACTIONS_ID_TOKEN_REQUEST_URL": "https://gh.example/token?api-version=2.0",
               "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-token"}
        with mock.patch.dict(mint.os.environ, env), mock.patch.object(mint.urllib.request, "urlopen", urlopen):
            token = mint._fetch_github_oidc_token(5)
        self.assertTrue(token.count(".") == 2)
        self.assertIn("api-version=2.0&audience=registry.sylphx.com", seen["url"])
        self.assertEqual(seen["auth"], "Bearer request-token")

    def test_without_id_token_permission_it_fails_closed(self) -> None:
        with mock.patch.dict(mint.os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                mint._fetch_github_oidc_token(5)


class RegistryMintRetryTests(unittest.TestCase):
    """One issuer blip (connect timeout, 5xx) must not fail an image build;
    a refusal (4xx) must fail at once."""

    REPOSITORY = "library/sylphx-hands"

    def registry_token(self) -> str:
        now = int(time.time())
        return jwt({
            "aud": [mint.REGISTRY_TOKEN_SERVICE], "iat": now, "exp": now + 300,
            "access": [{"type": "repository", "name": self.REPOSITORY, "actions": ["pull", "push"]}],
        })

    def response(self):
        token = self.registry_token()

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def geturl(self):
                return mint.DEFAULT_MINT_URL + "?x=1"

            def read(self):
                return json.dumps({"token": token}).encode()

        return Response()

    def run_mint(self, outcomes):
        calls = []
        sleeps = []

        def urlopen(request, timeout):
            calls.append(timeout)
            outcome = outcomes[len(calls) - 1]
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        with mock.patch.object(mint.urllib.request, "urlopen", urlopen), \
                mock.patch.object(mint.time, "sleep", sleeps.append):
            try:
                result = mint._mint_registry_token(
                    "svid", "gha:1", self.REPOSITORY, mint.DEFAULT_REGISTRY_HOST, 20
                )
            except RuntimeError as error:
                result = error
        return result, calls, sleeps

    @staticmethod
    def http_error(code: int):
        return mint.urllib.error.HTTPError(mint.DEFAULT_MINT_URL, code, "x", {}, None)

    def test_first_attempt_timeout_then_success(self) -> None:
        connect_timeout = mint.urllib.error.URLError(TimeoutError("timed out"))
        result, calls, sleeps = self.run_mint([connect_timeout, self.response()])
        self.assertIsInstance(result, str)
        self.assertEqual(len(calls), 2)
        self.assertEqual(sleeps, [mint.MINT_BACKOFF_SECONDS[0]])

    def test_read_timeout_and_5xx_are_retried(self) -> None:
        result, calls, _ = self.run_mint([TimeoutError("read"), self.http_error(503), self.response()])
        self.assertIsInstance(result, str)
        self.assertEqual(len(calls), 3)

    def test_gives_up_after_bounded_attempts(self) -> None:
        blips = [self.http_error(502)] * (mint.MINT_ATTEMPTS + 2)
        result, calls, sleeps = self.run_mint(blips)
        self.assertIsInstance(result, RuntimeError)
        self.assertEqual(len(calls), mint.MINT_ATTEMPTS)
        self.assertEqual(len(sleeps), mint.MINT_ATTEMPTS - 1)
        self.assertIn("HTTP 502", str(result))
        self.assertIn(f"{mint.MINT_ATTEMPTS} attempts", str(result))

    def test_refusal_is_not_retried(self) -> None:
        for code in (400, 401, 403, 404, 429):
            result, calls, sleeps = self.run_mint([self.http_error(code), self.response()])
            self.assertIsInstance(result, RuntimeError)
            self.assertIn(f"HTTP {code}", str(result))
            self.assertEqual(len(calls), 1)
            self.assertEqual(sleeps, [])

    def test_invalid_json_is_not_retried(self) -> None:
        class Bad:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def geturl(self):
                return mint.DEFAULT_MINT_URL

            def read(self):
                return b"not json"

        result, calls, _ = self.run_mint([Bad(), self.response()])
        self.assertIsInstance(result, RuntimeError)
        self.assertEqual(len(calls), 1)


class PerGrantSvidTests(unittest.TestCase):
    GRANT = mint.SPIFFE_ID + "/grant/gha-hands-publish/repository/library/sylphx-hands"

    def svid(self, sub: str) -> str:
        now = int(time.time())
        return jwt({"sub": sub, "aud": [mint.SPIFFE_AUDIENCE], "iat": now, "exp": now + 300})

    def test_accepts_the_per_grant_identity_only_for_its_own_repository(self) -> None:
        now = int(time.time())
        mint._validate_svid(self.svid(self.GRANT), now, "library/sylphx-hands")
        with self.assertRaises(ValueError):
            mint._validate_svid(self.svid(self.GRANT), now, "library/sylphx-data-edge")
        with self.assertRaises(ValueError):
            mint._validate_svid(self.svid(mint.SPIFFE_ID + "/grant//repository/library/x"), now, None)
        with self.assertRaises(ValueError):
            mint._validate_svid(self.svid(mint.SPIFFE_ID + "/grant/a/b/repository/library/x"), now, None)

    def test_picks_the_per_grant_svid_over_the_fixed_one(self) -> None:
        now = int(time.time())
        doc = {"svids": [{"svid": self.svid(mint.SPIFFE_ID)}, {"svid": self.svid(self.GRANT)},
                         {"svid": self.svid("spiffe://sylphx.local/role/other")}]}
        picked = mint._pick_svid(doc, now, "library/sylphx-hands")
        self.assertEqual(mint._jwt_claims(picked)["sub"], self.GRANT)
        fixed_only = {"svids": [{"svid": self.svid(mint.SPIFFE_ID)}]}
        self.assertEqual(mint._jwt_claims(mint._pick_svid(fixed_only, now, "library/sylphx-hands"))["sub"], mint.SPIFFE_ID)
        self.assertIsNone(mint._pick_svid({"svids": []}, now, "library/sylphx-hands"))


class EvidenceDocumentTests(unittest.TestCase):
    def build(self, tmp: Path) -> dict:
        oci = tmp / "image-oci"
        oci.mkdir(parents=True, exist_ok=True)
        (oci / "index.json").write_bytes(b'{"schemaVersion":2,"manifests":[]}\n')
        evidence = tmp / "evidence"
        evidence.mkdir(parents=True, exist_ok=True)
        environ = {
            "IMAGE": LANE_IMAGE,
            "IMAGE_TAG": "abcdef12",
            "PLATFORMS": "linux/amd64",
            "SOURCE_SHA": "a" * 40,
            "OCI_DIR": str(oci),
            "BUILD_EVIDENCE_DIR": str(evidence),
            "GITHUB_REPOSITORY": "SylphxAI/hands",
            "GITHUB_RUN_ID": "123",
            "GITHUB_RUN_ATTEMPT": "2",
        }
        return pack.build_document(environ)

    def test_binds_the_local_index_digest_and_source_revision(self) -> None:
        import hashlib
        import tempfile

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document = self.build(root)
            expected = "sha256:" + hashlib.sha256(b'{"schemaVersion":2,"manifests":[]}\n').hexdigest()
            self.assertEqual(document["ociIndexDigest"], expected)
            self.assertEqual(document["schema"], pack.SCHEMA)
            self.assertEqual(document["sourceSha"], "a" * 40)
            self.assertEqual(document["sourceRepository"], "SylphxAI/hands")
            self.assertEqual(document["platforms"], ["linux/amd64"])

    def test_missing_oci_layout_fails_closed(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            oci = root / "image-oci"
            oci.mkdir()
            with self.assertRaises(SystemExit):
                pack.index_digest(oci)


class ReadbackResolvesWrapperTests(unittest.TestCase):
    """The readback must follow the entry the layout root names.

    `containers/image` resolves `oci:DIR` to that entry, so a caller that wraps a
    one-platform layout in an index (which it must, or the tag carries a bare
    manifest and index-resolving consumers get nothing) leaves the root naming the
    wrapper. The registry then reports the wrapper, and a readback that compared
    the root's children would refuse a correct push -- measured live 2026-09-18:
    `FATAL: registry references 1 manifests, locally built 1; refusing to promote`
    with the wrapper digest on the registry side and the platform manifest digest
    on the local side.
    """

    def setUp(self) -> None:
        self.workflow = (ROOT / ".github/workflows/image-lane.yml").read_text()

    def test_readback_follows_a_single_entry_index_root(self) -> None:
        self.assertIn("def resolved_entry", self.workflow)
        self.assertIn("local_index = resolved_entry(local_index, root_dir)", self.workflow)
        # The resolver must only follow an index entry, never a manifest entry.
        self.assertIn("application/vnd.oci.image.index.v1+json", self.workflow)


class TransferResumeTests(unittest.TestCase):
    """The transfer download resumes across a reset instead of restarting.

    Live 2026-09-27/28: the artifact blob edge stopped a 2.1 GiB transfer after
    21 minutes (exit 0, layout incomplete) and reset a 2.3 GiB one at 7.5
    minutes with `curl: (56)`. Each attempt must re-resolve a fresh signed URL
    and continue from the bytes already on disk.
    """

    def test_the_resume_header_names_the_bytes_on_disk(self) -> None:
        self.assertEqual(transfer.range_header(0), {})
        self.assertEqual(transfer.range_header(1), {"Range": "bytes=1-"})
        self.assertEqual(
            transfer.range_header(2_280_056_703),
            {"Range": "bytes=2280056703-"},
        )

    def test_the_newest_unexpired_attempt_wins_and_says_so(self) -> None:
        artifacts = [
            {"name": "image-lane-image-abc-7-1", "expired": False},
            {"name": "image-lane-image-abc-7-2", "expired": True},
            {"name": "image-lane-evidence-abc-7-1", "expired": False},
        ]
        record, notes = transfer.select_artifact_record(
            artifacts, "image-lane-image-abc-7", 2
        )
        self.assertEqual(record["name"], "image-lane-image-abc-7-1")
        self.assertTrue(any("expired" in note for note in notes))
        name, _ = transfer.select_artifact(artifacts, "image-lane-image-abc-7", 2)
        self.assertEqual(name, "image-lane-image-abc-7-1")

    def test_an_empty_run_fails_closed(self) -> None:
        with self.assertRaises(SystemExit):
            transfer.select_artifact_record([], "image-lane-image-abc-7", 1)

    def test_a_transfer_without_metadata_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(SystemExit):
                transfer.download_artifact(
                    {"name": "image-lane-image-abc-7-1"},
                    Path(temporary) / "out",
                    "token",
                    attempts=1,
                    sleep_seconds=0.0,
                    timeout=1.0,
                )

    def test_the_archive_is_hashed_before_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "artifact.zip"
            archive.write_bytes(b"hello")
            self.assertEqual(
                transfer.sha256_of(archive),
                "sha256:" + hashlib.sha256(b"hello").hexdigest(),
            )


class TransferExtractionTests(unittest.TestCase):
    """Extraction empties the destination and refuses unsafe members."""

    def _layout(self, root: Path) -> Path:
        archive = root / "layout.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("index.json", "{}")
            bundle.writestr("oci-layout", '{"imageLayoutVersion": "1.0.0"}')
            bundle.writestr("blobs/sha256/" + "a" * 64, b"layer")
        return archive

    def test_extracts_a_layout_into_an_emptied_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "image"
            destination.mkdir()
            stale = destination / "index.json"
            stale.write_text("old")
            members = transfer.extract_archive(self._layout(root), destination)
            self.assertEqual(members, 3)
            self.assertEqual(
                stale.read_text(), "{}", "a stale file survived the extraction"
            )
            self.assertTrue((destination / "blobs" / "sha256" / ("a" * 64)).is_file())

    def test_refuses_a_member_that_escapes_the_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("../escape", "x")
            with self.assertRaises(SystemExit):
                transfer.extract_archive(archive, root / "out")
            self.assertFalse((root / "escape").exists())

    def test_refuses_a_symbolic_link_member(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "link.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                info = zipfile.ZipInfo("index.json")
                info.external_attr = 0o120777 << 16
                bundle.writestr(info, "target")
            with self.assertRaises(SystemExit):
                transfer.extract_archive(archive, root / "out")


if __name__ == "__main__":
    unittest.main()


class PublisherIdentityWaitTest(unittest.TestCase):
    """The runner identity may appear after the job starts (SPIRE entry sync)."""

    def test_waits_for_an_identity_that_appears_late(self) -> None:
        import json as _json
        import os
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            socket = root / "agent.sock"
            socket.touch()
            counter = root / "calls"
            agent = root / "spire-agent"
            agent.write_text(
                "#!/bin/sh\n"
                f"n=$(cat {counter} 2>/dev/null || echo 0); n=$((n+1)); echo $n > {counter}\n"
                '[ "$n" -ge 3 ] || exit 1\n'
                "cat <<'JSON'\n"
                + _json.dumps({"svids": [{"svid": "token"}]})
                + "\nJSON\n"
            )
            agent.chmod(0o755)
            with mock.patch.object(mint, "_pick_svid", return_value="token"), mock.patch.object(
                mint.time, "sleep"
            ):
                self.assertEqual(mint._fetch_svid(agent, socket, 60), "token")
            self.assertEqual(int(counter.read_text()), 3)

    def test_the_identity_wait_outlasts_the_http_timeout(self) -> None:
        self.assertGreaterEqual(mint.DEFAULT_IDENTITY_WAIT_SECONDS, 120)
        self.assertLess(mint.DEFAULT_TIMEOUT_SECONDS, mint.DEFAULT_IDENTITY_WAIT_SECONDS)

