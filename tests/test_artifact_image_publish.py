"""Exercise the public publishing boundary without credentials or a registry."""

import base64
import copy
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / ".github/actions/artifact-image-publish"
spec = importlib.util.spec_from_file_location(
    "artifact_image_publish", ACTION / "publish.py"
)
lane = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lane)
PARENT = "orgs/org_example/projects/prj_example"
IMAGE = lane.expected_image(PARENT)


def token(claims):
    encode = lambda value: (
        base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    )
    return f"{encode({'alg': 'ES256'})}.{encode(claims)}.fixture-signature"


def upload(tag="up_one", now=None):
    now = int(time.time()) if now is None else now
    # Match the public Artifact prepareUpload mint TTL, not the old lane's TTL.
    claims = {
        "aud": "container_registry",
        "iat": now,
        "exp": now + 3600,
        "access": [
            {
                "type": "repository",
                "name": IMAGE.split("/", 1)[1],
                "actions": ["pull", "push"],
            }
        ],
    }
    return {
        "image_reference": f"{IMAGE}:{tag}",
        "registry_auth": json.dumps(
            {"auths": {lane.REGISTRY: {"identitytoken": token(claims)}}}
        ),
        "expires_at": datetime.fromtimestamp(now + 3600, timezone.utc).isoformat(),
    }


def mutate_claims(answer, change):
    auth = json.loads(answer["registry_auth"])
    record = auth["auths"][lane.REGISTRY]
    claims = lane.claims(record["identitytoken"])
    change(claims)
    record["identitytoken"] = token(claims)
    answer["registry_auth"] = json.dumps(auth)


def make_layout(directory, marker="runtime", wrapper=False):
    blobs = directory / "blobs/sha256"
    blobs.mkdir(parents=True, exist_ok=True)

    def blob(value, media):
        body = (
            value
            if isinstance(value, bytes)
            else json.dumps(value, separators=(",", ":")).encode()
        )
        digest = lane.sha256(body)
        (blobs / digest[7:]).write_bytes(body)
        return {"mediaType": media, "digest": digest, "size": len(body)}

    config = blob(
        {"architecture": "amd64", "os": "linux", "marker": marker},
        "application/vnd.oci.image.config.v1+json",
    )
    layer = blob(
        ("fixture-" + marker).encode(), "application/vnd.oci.image.layer.v1.tar+gzip"
    )
    manifest = {
        "schemaVersion": 2,
        "mediaType": lane.IMAGE_TYPES[0],
        "config": config,
        "layers": [layer],
    }
    descriptor = blob(manifest, lane.IMAGE_TYPES[0])
    if wrapper:
        descriptor = blob(
            {
                "schemaVersion": 2,
                "mediaType": lane.INDEX_TYPES[0],
                "manifests": [descriptor],
            },
            lane.INDEX_TYPES[0],
        )
    (directory / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}')
    (directory / "index.json").write_text(
        json.dumps({"schemaVersion": 2, "manifests": [descriptor]})
    )
    return descriptor


def record(descriptor, subject=""):
    digest, media, size = descriptor
    return {
        "name": lane.artifact_name(PARENT, digest),
        "uid": "art_fixture",
        "digest": digest,
        "kind": "image",
        "media_type": media,
        "size_bytes": str(size),
        "reference": f"{IMAGE}@{digest}",
        "subject": subject,
        "verification_state": "unverified",
    }


@pytest.mark.parametrize("camel", [False, True])
def test_public_upload_selects_exact_project_repository_and_credentials(camel):
    answer = upload()
    if camel:
        answer = {
            "imageReference": answer["image_reference"],
            "registryAuth": answer["registry_auth"],
            "expiresAt": answer["expires_at"],
        }
    reference, jwt, auth = lane.validate_upload(answer, PARENT)
    assert reference == f"{IMAGE}:up_one"
    assert lane.claims(jwt)["access"][0]["name"] == IMAGE.split("/", 1)[1]
    assert set(auth["auths"]) == {lane.REGISTRY}


def test_public_upload_expiry_allows_bounded_issuer_response_latency():
    now = int(time.time())
    answer = upload(now=now)
    answer["expires_at"] = datetime.fromtimestamp(now + 3605, timezone.utc).isoformat()
    lane.validate_upload(answer, PARENT, now=now)
    answer["expires_at"] = datetime.fromtimestamp(now + 3631, timezone.utc).isoformat()
    with pytest.raises(ValueError, match="expiry"):
        lane.validate_upload(answer, PARENT, now=now)


@pytest.mark.parametrize(
    "change",
    [
        lambda a: a.update(image_reference="registry.sylphx.com/other/repo:up_one"),
        lambda a: a.update(image_reference=f"{IMAGE}:latest/../wrong"),
        lambda a: a.update(image_reference=f"{IMAGE}@sha256:" + "a" * 64),
        lambda a: a.update(registry_auth="not-json"),
        lambda a: a.update(
            registry_auth='{"auths":{"attacker.example":{"identitytoken":"secret"}}}'
        ),
        lambda a: a.update(expires_at="2001-01-01T00:00:00Z"),
        lambda a: a.update(imageReference="conflicting-alias"),
        lambda a: mutate_claims(a, lambda c: c["access"][0].update(name="other/repo")),
        lambda a: mutate_claims(
            a, lambda c: c["access"].append(copy.deepcopy(c["access"][0]))
        ),
        lambda a: mutate_claims(
            a, lambda c: c["access"][0]["actions"].append("delete")
        ),
        lambda a: mutate_claims(a, lambda c: c["access"][0]["actions"].append("push")),
        lambda a: mutate_claims(a, lambda c: c.update(aud="wrong")),
        lambda a: mutate_claims(a, lambda c: c.update(exp=c["iat"] - 1)),
        lambda a: mutate_claims(a, lambda c: c.update(exp=c["iat"] + 3601)),
    ],
)
def test_malformed_or_overbroad_prepare_answers_fail_closed(change):
    answer = upload()
    change(answer)
    with pytest.raises((ValueError, TypeError)):
        lane.validate_upload(answer, PARENT)


def test_auth_files_are_private_and_docker_and_skopeo_compatible(tmp_path):
    _, jwt, auth = lane.validate_upload(upload(), PARENT)
    directory = tmp_path / "auth"
    lane.write_auth(directory, jwt, auth)
    for path in directory.iterdir():
        assert path.stat().st_mode & 0o777 == 0o600
    assert directory.stat().st_mode & 0o777 == 0o700
    docker = json.loads((directory / "config.json").read_text())
    skopeo = json.loads((directory / "skopeo-auth.json").read_text())
    assert docker["auths"][lane.REGISTRY]["identitytoken"] == jwt
    assert (
        base64.b64decode(skopeo["auths"][lane.REGISTRY]["auth"]).decode()
        == "oauth2accesstoken:" + jwt
    )


def test_access_exchange_uses_source_job_oidc_audience_and_declared_name(monkeypatch):
    monkeypatch.setenv(
        "ACTIONS_ID_TOKEN_REQUEST_URL",
        "https://github-oidc.example/token?audience=wrong&x=1",
    )
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "request-secret")
    with (
        mock.patch.object(
            lane,
            "json_request",
            side_effect=[
                {"value": "job-oidc"},
                {
                    "api_key": "ci-key",
                    "active_after_ms": 500,
                    "expires_at": datetime.fromtimestamp(
                        time.time() + 600, timezone.utc
                    ).isoformat(),
                },
            ],
        ) as request,
        mock.patch.object(lane.time, "sleep") as sleep,
    ):
        access = lane.Access("declared-publisher")
    assert access.key == "ci-key"
    assert "audience=sylphx-access" in request.call_args_list[0].args[0]
    assert "audience=wrong" not in request.call_args_list[0].args[0]
    assert request.call_args_list[1].kwargs["document"] == {
        "name": "declared-publisher",
        "token": "job-oidc",
    }
    sleep.assert_called_once_with(0.5)


@pytest.mark.parametrize(
    "answer",
    [
        {},
        {"api_key": ""},
        {"api_key": ["bad"]},
        {"api_key": "bad\nheader"},
        {"api_key": "key", "active_after_ms": -1},
        {"api_key": "key", "active_after_ms": 30001},
        {"api_key": "key", "expires_at": "invalid"},
        {"api_key": "key", "expires_at": "2001-01-01T00:00:00Z"},
    ],
)
def test_malformed_access_answers_are_not_used(monkeypatch, answer):
    monkeypatch.setenv(
        "ACTIONS_ID_TOKEN_REQUEST_URL", "https://github-oidc.example/token"
    )
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "request-secret")
    with mock.patch.object(
        lane, "json_request", side_effect=[{"value": "job-oidc"}, answer]
    ):
        with pytest.raises((ValueError, KeyError)):
            lane.Access("declared-publisher")


@pytest.mark.parametrize(
    "event", ["pull_request", "pull_request_target", "workflow_dispatch", "merge_group"]
)
def test_untrusted_or_nonpush_events_never_request_auth(monkeypatch, event):
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    with (
        mock.patch.object(lane, "Access") as access,
        mock.patch.object(lane, "json_request") as request,
    ):
        with pytest.raises(ValueError, match="push events only"):
            lane.run()
    access.assert_not_called()
    request.assert_not_called()


def test_source_mismatch_fails_before_auth(monkeypatch):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("INPUT_PROJECT", PARENT)
    monkeypatch.setenv("INPUT_SOURCE_SHA", "a" * 40)
    with (
        mock.patch.object(lane.subprocess, "check_output", return_value="b" * 40),
        mock.patch.object(lane, "Access") as access,
    ):
        with pytest.raises(ValueError, match="checkout"):
            lane.run()
    access.assert_not_called()


@pytest.mark.parametrize("wrapper", [False, True])
def test_local_layout_verifies_blob_graph_and_resolves_transport_entry(
    tmp_path, wrapper
):
    descriptor = make_layout(tmp_path, wrapper=wrapper)
    selected, body = lane.local_manifest(tmp_path)
    assert selected == descriptor
    assert lane.sha256(body) == descriptor["digest"]
    (tmp_path / "blobs/sha256" / descriptor["digest"][7:]).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="digest/size"):
        lane.local_manifest(tmp_path)


@pytest.mark.skipif(
    not shutil.which("skopeo"),
    reason="optional local OCI transport qualification needs skopeo",
)
@pytest.mark.parametrize("wrapper", [False, True])
def test_real_skopeo_resolves_and_preserves_the_verified_transport_entry(
    tmp_path, wrapper
):
    descriptor = make_layout(tmp_path / "source", wrapper=wrapper)
    _, body = lane.local_manifest(tmp_path / "source")
    selected = subprocess.check_output(
        ["skopeo", "inspect", "--raw", f"oci:{tmp_path / 'source'}"]
    )
    assert selected == body
    subprocess.run(
        [
            "skopeo",
            "copy",
            "--all",
            "--preserve-digests",
            f"oci:{tmp_path / 'source'}",
            f"oci:{tmp_path / 'copy'}",
        ],
        check=True,
        capture_output=True,
    )
    copied, copied_body = lane.local_manifest(tmp_path / "copy")
    assert copied["digest"] == descriptor["digest"]
    assert copied_body == body


def registry_response(descriptor, body):
    return {
        "Docker-Content-Digest": descriptor["digest"],
        "Content-Type": descriptor["mediaType"],
    }, body


def test_registry_head_digest_get_and_local_bytes_must_all_agree(tmp_path):
    descriptor = make_layout(tmp_path / "layout")
    _, body = lane.local_manifest(tmp_path / "layout")
    headers, _ = registry_response(descriptor, body)
    with mock.patch.object(
        lane, "request", side_effect=[(headers, b""), (headers, body)]
    ) as request:
        result = lane.readback(
            IMAGE, "up_one", "secret", descriptor, body, tmp_path / "remote.json"
        )
    assert result == (descriptor["digest"], descriptor["mediaType"], len(body))
    assert request.call_args_list[1].args[0].endswith(descriptor["digest"])
    assert (tmp_path / "remote.json").read_bytes() == body
    for changed in (b"{}", body + b" "):
        output = tmp_path / "refused.json"
        with mock.patch.object(
            lane, "request", side_effect=[(headers, b""), (headers, changed)]
        ):
            with pytest.raises(ValueError):
                lane.readback(IMAGE, "up_one", "secret", descriptor, body, output)
        assert not output.exists()


def test_registry_cannot_substitute_another_valid_manifest(tmp_path):
    one = make_layout(tmp_path / "one", "one")
    two = make_layout(tmp_path / "two", "two")
    _, body_one = lane.local_manifest(tmp_path / "one")
    _, body_two = lane.local_manifest(tmp_path / "two")
    headers, body = registry_response(two, body_two)
    with mock.patch.object(
        lane, "request", side_effect=[(headers, b""), (headers, body)]
    ):
        with pytest.raises(ValueError, match="locally built"):
            lane.readback(
                IMAGE, "up_one", "secret", one, body_one, tmp_path / "remote.json"
            )
    assert not (tmp_path / "remote.json").exists()


@pytest.mark.parametrize("conflict", [False, True])
def test_registration_uses_managed_body_mapping_and_validates_durable_readback(
    conflict,
):
    descriptor = ("sha256:" + "a" * 64, lane.IMAGE_TYPES[0], 42)
    artifact = record(descriptor)
    access = mock.Mock()
    access.call.side_effect = [
        lane.HttpFailure(409, "RESOURCE_ALREADY_EXISTS") if conflict else artifact,
        artifact,
    ]
    assert lane.register(access, PARENT, IMAGE, descriptor) == artifact["name"]
    call = access.call.call_args_list[0]
    assert call.args[0] == f"/v1/{PARENT}/artifacts?managed_upload=true"
    assert call.kwargs["document"] == {
        "digest": descriptor[0],
        "kind": "image",
        "media_type": descriptor[1],
        "size_bytes": "42",
    }
    assert call.kwargs["idempotency"].startswith("artifact-image-")
    assert access.call.call_args_list[1].args[0] == f"/v1/{artifact['name']}"


@pytest.mark.parametrize(
    "change",
    [
        {"name": "another/name"},
        {"digest": "sha256:" + "b" * 64},
        {"kind": "bundle"},
        {"reference": ""},
        {"reference": "registry.sylphx.com/other/repo@sha256:" + "a" * 64},
        {"media_type": lane.INDEX_TYPES[0]},
        {"size_bytes": "43"},
        {"subject": "another/artifact"},
        {"uid": ""},
        {"verification_state": "failed"},
        {"meta": {"delete_time": "2026-01-01T00:00:00Z"}},
    ],
)
def test_conflict_readback_cannot_accept_wrong_generic_or_failed_record(change):
    descriptor = ("sha256:" + "a" * 64, lane.IMAGE_TYPES[0], 42)
    access = mock.Mock()
    access.call.side_effect = [
        lane.HttpFailure(409, "RESOURCE_ALREADY_EXISTS"),
        {**record(descriptor), **change},
    ]
    with pytest.raises(ValueError):
        lane.register(access, PARENT, IMAGE, descriptor)


def test_registration_does_not_hide_other_conflicts_or_malformed_success():
    descriptor = ("sha256:" + "a" * 64, lane.IMAGE_TYPES[0], 42)
    access = mock.Mock()
    access.call.side_effect = lane.HttpFailure(409, "IDEMPOTENCY_IN_PROGRESS")
    with pytest.raises(lane.HttpFailure):
        lane.register(access, PARENT, IMAGE, descriptor)
    access.call.assert_called_once()
    access.call.side_effect = None
    access.call.return_value = {}
    with pytest.raises(ValueError):
        lane.register(access, PARENT, IMAGE, descriptor)


@pytest.mark.parametrize(
    "failure", [None, "build", "evidence", "push", "readback", "register", "verify"]
)
def test_pipeline_cleans_credentials_and_emits_receipt_only_after_full_success(
    tmp_path, monkeypatch, failure
):
    env = {
        "GITHUB_EVENT_NAME": "push",
        "INPUT_PROJECT": PARENT,
        "INPUT_SOURCE_SHA": "a" * 40,
        "GITHUB_SHA": "a" * 40,
        "GITHUB_WORKFLOW_REF": "owner/source/.github/workflows/image.yml@refs/heads/main",
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "2",
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_OUTPUT": str(tmp_path / "output"),
        "INPUT_CI_NAME": "test-ci",
        "INPUT_PLATFORMS": "linux/amd64",
        "INPUT_BUILD_COMMAND": "build",
        "INPUT_EVIDENCE_COMMAND": "evidence",
        "INPUT_VERIFY_COMMAND": "verify",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    preparations = [
        lane.validate_upload(upload(tag), PARENT)
        for tag in ("up_runtime", "up_evidence", "up_refresh")
    ]
    access = mock.Mock()
    access.prepare.side_effect = preparations
    observed = []

    def command(command, child_env):
        root = Path(child_env["LANE_ROOT"])
        observed.append(command)
        assert child_env["SOURCE_SHA"] == "a" * 40
        assert child_env["SOURCE_WORKFLOW"] == env["GITHUB_WORKFLOW_REF"]
        assert child_env["SOURCE_RUN_ID"] == "123"
        assert child_env["SOURCE_RUN_ATTEMPT"] == "2"
        if command in ("build", "evidence"):
            assert Path(child_env["ARTIFACT_REGISTRY_AUTH_FILE"]).is_file()
            assert (root / "registry-auth/config.json").is_file()
        else:
            assert "ARTIFACT_REGISTRY_AUTH_FILE" not in child_env
            assert not (root / "registry-auth").exists()
            assert Path(child_env["IMAGE_REGISTRY_MANIFEST"]).is_file()
            assert Path(child_env["EVIDENCE_REGISTRY_MANIFEST"]).is_file()
        if command == failure:
            raise RuntimeError("fixture failure")
        if command == "build":
            make_layout(Path(child_env["OCI_DIR"]))
        elif command == "evidence":
            (Path(child_env["BUILD_EVIDENCE_DIR"]) / "evidence.json").write_text("{}")
            make_layout(Path(child_env["BUILD_EVIDENCE_OCI_DIR"]), "evidence")
        else:
            (root / "verify/receipt.json").write_text('{"verified":true}')

    def readback(image, tag, token, descriptor, body, output):
        if failure == "readback":
            raise ValueError("fixture readback failure")
        output.write_bytes(body)
        return descriptor["digest"], descriptor["mediaType"], len(body)

    def register(access, parent, image, descriptor, subject=""):
        if failure == "register":
            raise ValueError("fixture registration failure")
        return lane.artifact_name(parent, descriptor[0])

    with (
        mock.patch.object(lane, "Access", return_value=access) as exchange,
        mock.patch.object(lane, "shell", side_effect=command),
        mock.patch.object(lane.platform, "machine", return_value="x86_64"),
        mock.patch.object(lane.shutil, "which", return_value="/fixture/tool"),
        mock.patch.object(lane.subprocess, "check_output", return_value="a" * 40),
        mock.patch.object(
            lane,
            "push",
            side_effect=RuntimeError("fixture push failure")
            if failure == "push"
            else None,
        ) as push,
        mock.patch.object(lane, "readback", side_effect=readback),
        mock.patch.object(lane, "register", side_effect=register),
    ):
        if failure:
            with pytest.raises((RuntimeError, ValueError)):
                lane.run()
        else:
            lane.run()
            assert exchange.call_count == 2
            assert [call.args[1] for call in push.call_args_list] == [
                f"{IMAGE}:up_runtime",
                f"{IMAGE}:up_evidence",
            ]
    roots = list(tmp_path.glob("artifact-image-*"))
    assert len(roots) == 1
    assert not (roots[0] / "registry-auth").exists()
    outputs = dict(
        line.split("=", 1) for line in (tmp_path / "output").read_text().splitlines()
    )
    assert all(
        secret not in (tmp_path / "output").read_text()
        for secret in ("fixture-signature", "identitytoken", "skopeo-auth")
    )
    if failure:
        assert "digest" not in outputs and "verify_receipt" not in outputs
    else:
        assert observed == ["build", "evidence", "verify"]
        assert outputs["image_reference"] == IMAGE + "@" + outputs["digest"]
        assert outputs["evidence_reference"] == IMAGE + "@" + outputs["evidence_digest"]
        with tarfile.open(
            fileobj=io.BytesIO(base64.b64decode(outputs["verify_receipt"])), mode="r:gz"
        ) as archive:
            assert json.load(archive.extractfile("./receipt.json")) == {
                "verified": True
            }


def test_receipt_is_required_and_bounded(tmp_path):
    with pytest.raises(ValueError, match="no receipt"):
        lane.receipt(tmp_path)
    (tmp_path / "large.bin").write_bytes(os.urandom(524288))
    with pytest.raises(ValueError, match="524288"):
        lane.receipt(tmp_path)


def test_http_never_follows_redirects_and_cleanup_is_fenced(tmp_path, monkeypatch):
    assert (
        lane.NoRedirect().redirect_request(
            None, None, 302, "", {}, "https://other.example"
        )
        is None
    )
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    with pytest.raises(ValueError, match="outside"):
        lane.cleanup(tmp_path.parent)


def test_provider_and_tool_errors_do_not_log_secrets(capsys, monkeypatch):
    monkeypatch.setattr(lane.sys, "argv", ["publish.py"])
    with mock.patch.object(
        lane, "run", side_effect=ValueError("private-provider-token")
    ):
        assert lane.main() == 1
    assert "private-provider-token" not in capsys.readouterr().err
    with mock.patch.object(
        lane.subprocess,
        "run",
        return_value=subprocess.CompletedProcess(
            [], 1, b"private-token", b"private-token"
        ),
    ):
        with pytest.raises(RuntimeError) as error:
            lane.push(Path("layout"), f"{IMAGE}:up_one", Path("auth-file"))
    assert "private-token" not in str(error.value)


def test_composite_keeps_caller_identity_and_always_cleans_up():
    action = yaml.safe_load((ACTION / "action.yml").read_text())
    assert action["runs"]["using"] == "composite"
    assert not {"image", "repository", "token", "registry", "api-url"} & set(
        action["inputs"]
    )
    steps = action["runs"]["steps"]
    assert "always()" in steps[-1]["if"]
    assert all("${{ inputs." not in step.get("run", "") for step in steps)
    assert "_root" not in action["outputs"]
    for name in (
        "digest",
        "evidence_digest",
        "verify_receipt",
        "image_reference",
        "evidence_reference",
    ):
        assert name in action["outputs"]
    source = (ACTION / "publish.py").read_text()
    for forbidden in (
        "mint-registry-auth",
        "spire-agent",
        "token-issuer",
        "/spiffe-workload-api",
    ):
        assert forbidden not in source
