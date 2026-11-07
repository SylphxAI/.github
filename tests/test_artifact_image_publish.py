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
ACCESS_KEY_NAME = PARENT + "/api_keys/key_test"


def issued_key(wait=500):
    return {
        "api_key": "ci-key", "active_after_ms": wait,
        "expires_at": datetime.fromtimestamp(time.time() + 600, timezone.utc).isoformat(),
        "key": {"name": ACCESS_KEY_NAME,
                "status": {"principal": "orgs/org_example/principals/prn_test"}},
    }


def ready_key():
    return {"org": "orgs/org_example", "principal": "prn_test",
            "api_key": ACCESS_KEY_NAME}


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
                issued_key(),
            ],
        ) as request,
        mock.patch.object(lane.time, "sleep") as sleep,
        mock.patch.object(lane, "read_identity", return_value=ready_key()) as identity,
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
    assert identity.call_args.args == (lane.API + "/v1/whoami", "ci-key")
    assert identity.call_args.kwargs["allow_propagation"] is True


def access_environment(monkeypatch):
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", "https://github-oidc.example/token")
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "request-secret")


def test_delayed_key_uses_shared_readiness_and_never_reissues_or_retries_writes(monkeypatch):
    access_environment(monkeypatch)
    now = [0.0]
    with (
        mock.patch.object(lane.time, "monotonic", side_effect=lambda: now[0]),
        mock.patch.object(lane.time, "sleep", side_effect=lambda seconds: now.__setitem__(0, now[0] + seconds)),
        mock.patch.object(lane, "json_request", side_effect=[
            {"value": "job-oidc"}, issued_key(3000),
        ]) as request,
        mock.patch.object(lane, "read_identity", side_effect=[
            {"code": "key_not_yet_propagated"}, ready_key(),
        ]) as identity,
    ):
        assert lane.Access("declared-publisher").key == "ci-key"
    assert now[0] == 5
    assert request.call_count == 2
    assert identity.call_count == 2
    for call in identity.call_args_list:
        assert call.args == (lane.API + "/v1/whoami", "ci-key")
        assert call.kwargs["allow_propagation"] is True
    assert [call.kwargs["timeout"] for call in identity.call_args_list] == [180, 178]


def test_never_ready_key_expires_one_read_budget_without_artifact_calls(monkeypatch):
    access_environment(monkeypatch)
    now = [0.0]
    with (
        mock.patch.object(lane.time, "monotonic", side_effect=lambda: now[0]),
        mock.patch.object(lane.time, "sleep", side_effect=lambda seconds: now.__setitem__(0, now[0] + seconds)),
        mock.patch.object(lane, "json_request", side_effect=[
            {"value": "job-oidc"}, issued_key(3000),
        ]) as request,
        mock.patch.object(lane, "read_identity", return_value={"code": "key_not_yet_propagated"}),
        pytest.raises(lane.ReadinessError),
    ):
        lane.Access("declared-publisher")
    assert now[0] == 183
    assert request.call_count == 2


@pytest.mark.parametrize("failure", [
    lane.ReadinessError("Access readiness refused with HTTP 401"),
    lane.ReadinessError("Access readiness refused with HTTP 403"),
    lane.ReadinessError("Access readiness refused with HTTP 503"),
    lane.ReadinessError("Access readiness read failed at the network"),
])
def test_readiness_permanent_and_transport_refusals_are_not_retried(monkeypatch, failure):
    access_environment(monkeypatch)
    with (
        mock.patch.object(lane.time, "sleep"),
        mock.patch.object(lane, "json_request", side_effect=[
            {"value": "job-oidc"}, issued_key(),
        ]) as request,
        mock.patch.object(lane, "read_identity", side_effect=failure) as identity,
        pytest.raises(lane.ReadinessError) as caught,
    ):
        lane.Access("declared-publisher")
    assert "step=access-readiness" in str(caught.value)
    assert request.call_count == 2
    assert identity.call_count == 1


@pytest.mark.parametrize("field,value", [
    ("org", "orgs/other"), ("principal", "prn_other"),
    ("api_key", PARENT + "/api_keys/another_key"),
])
def test_readiness_binds_the_exact_issued_key_and_subject(monkeypatch, field, value):
    access_environment(monkeypatch)
    with (
        mock.patch.object(lane.time, "sleep"),
        mock.patch.object(lane, "json_request", side_effect=[
            {"value": "job-oidc"}, issued_key(),
        ]) as request,
        mock.patch.object(lane, "read_identity", return_value=dict(ready_key(), **{field: value})),
        pytest.raises(lane.ReadinessError),
    ):
        lane.Access("declared-publisher")
    assert request.call_count == 2


@pytest.mark.parametrize("body,code", [
    ({"code": "key_not_yet_propagated", "error": "key_not_yet_propagated"}, "key_not_yet_propagated"),
    ({"error": {"code": "NOT_YET_PROPAGATED", "message": "private-provider-token"}}, "NOT_YET_PROPAGATED"),
    ({"error": {"code": "PERMISSION_DENIED", "message": "private-provider-token"}}, "PERMISSION_DENIED"),
    ({"error": "key_expired"}, "key_expired"),
    ({"code": "private-provider-token", "error": "private-provider-token"}, ""),
    ({"error": ["private-provider-token"]}, ""),
    (["private-provider-token"], ""),
])
def test_http_refusals_retain_only_known_codes_for_both_public_error_shapes(body, code):
    failure = lane.urllib.error.HTTPError(
        lane.API + "/v1/whoami", 401, "private-provider-token", {},
        io.BytesIO(json.dumps(body).encode()),
    )
    with mock.patch.object(lane.urllib.request, "build_opener") as opener:
        opener.return_value.open.side_effect = failure
        with pytest.raises(lane.HttpFailure) as caught:
            lane.json_request(lane.API + "/v1/whoami")
    assert caught.value.code == code
    assert "private-provider-token" not in str(caught.value)
    if code:
        assert "code=" + code in str(caught.value)


@pytest.mark.parametrize("step,status,code", [
    ("github-oidc", 401, "UNAUTHENTICATED"),
    ("access-exchange", 403, "PERMISSION_DENIED"),
    ("access-readiness", 401, "UNAUTHENTICATED"),
    ("artifact-prepare-upload", 401, "UNAUTHENTICATED"),
    ("artifact-prepare-upload", 503, "UNAVAILABLE"),
    ("artifact-prepare-upload", 401, "private-provider-token"),
])
def test_prepare_http_refusal_names_the_exact_step_status_and_only_safe_code(
    monkeypatch, capsys, step, status, code,
):
    access_environment(monkeypatch)
    body = {"code": code, "detail": "private-provider-token request-secret job-oidc ci-key"}

    def open_request(req, timeout):
        if req.full_url.startswith("https://github-oidc.example/"):
            current, answer = "github-oidc", {"value": "job-oidc"}
        elif req.full_url == lane.API + "/v1/access/github/token":
            current, answer = "access-exchange", issued_key()
        else:
            assert req.full_url == lane.API + f"/v1/{PARENT}/artifacts:prepareUpload"
            current, answer = "artifact-prepare-upload", upload()
        if current == step:
            raise lane.urllib.error.HTTPError(req.full_url, status, "private-provider-token", {},
                                             io.BytesIO(json.dumps(body).encode()))
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = req.full_url
        response.headers = {}
        response.read.return_value = json.dumps(answer).encode()
        return response

    identity = (subprocess.CompletedProcess([], 0, json.dumps(body) + "\n" + str(status), "")
                if step == "access-readiness" else
                subprocess.CompletedProcess([], 0, json.dumps(ready_key()) + "\n200", ""))
    with (
        mock.patch.object(lane.urllib.request, "build_opener") as opener,
        mock.patch.object(lane.subprocess, "run", return_value=identity),
        mock.patch.object(lane.time, "sleep"),
        pytest.raises(lane.HttpFailure) as caught,
    ):
        opener.return_value.open.side_effect = open_request
        access = lane.Access("declared-publisher")
        access.prepare(PARENT)
    failure = caught.value
    assert (failure.step, failure.status) == (step, status)
    assert failure.code == (code if code in lane.SAFE_ERROR_CODES else "")
    with mock.patch.object(lane, "run", side_effect=failure), mock.patch("sys.argv", ["publish.py"]):
        assert lane.main() == 1
    diagnostic = capsys.readouterr().err
    assert f"step={step}" in diagnostic
    assert f"HTTP {status}" in diagnostic
    if failure.code:
        assert "code=" + code in diagnostic
    for private in ("private-provider-token", "request-secret", "job-oidc", "ci-key", "https://"):
        assert private not in diagnostic


def test_http_step_labels_never_echo_caller_supplied_text():
    failure = lane.HttpFailure(401, "private-provider-token", "https://private-provider-token")
    assert "step=artifact-api" in str(failure)
    assert "private-provider-token" not in str(failure)


@pytest.mark.parametrize("code", ["NOT_YET_PROPAGATED", "key_not_yet_propagated"])
def test_access_call_retries_cross_replica_401_not_yet_propagated_within_bound(monkeypatch, code):
    access_environment(monkeypatch)
    with (
        mock.patch.object(lane.time, "sleep"),
        mock.patch.object(lane, "json_request", side_effect=[
            {"value": "job-oidc"}, issued_key(),
        ]),
        mock.patch.object(lane, "read_identity", return_value=ready_key()),
    ):
        access = lane.Access("declared-publisher")
    assert access.expiry > time.time() + 500
    answer = upload()
    with (
        mock.patch.object(lane.time, "sleep") as sleep,
        mock.patch.object(
            lane,
            "json_request",
            side_effect=[
                lane.HttpFailure(401, code),
                lane.HttpFailure(401, code),
                answer,
            ],
        ) as request,
    ):
        reference, _, _ = access.prepare(PARENT)
    assert reference == f"{IMAGE}:up_one"
    assert request.call_count == 3
    assert [call.args[0] for call in sleep.call_args_list] == [1, 1]


def test_access_call_does_not_retry_other_401_codes_or_exceed_retry_bound(monkeypatch):
    access_environment(monkeypatch)
    with (
        mock.patch.object(lane.time, "sleep"),
        mock.patch.object(lane, "json_request", side_effect=[
            {"value": "job-oidc"}, issued_key(),
        ]),
        mock.patch.object(lane, "read_identity", return_value=ready_key()),
    ):
        access = lane.Access("declared-publisher")
    with (
        mock.patch.object(lane.time, "sleep") as sleep,
        mock.patch.object(
            lane, "json_request", side_effect=lane.HttpFailure(401, "UNAUTHENTICATED")
        ) as request,
        pytest.raises(lane.HttpFailure) as caught,
    ):
        access.call(f"/v1/{PARENT}/artifacts:prepareUpload", document={})
    assert caught.value.code == "UNAUTHENTICATED"
    assert request.call_count == 1
    sleep.assert_not_called()
    with (
        mock.patch.object(lane.time, "sleep") as sleep,
        mock.patch.object(
            lane, "json_request", side_effect=lane.HttpFailure(401, "NOT_YET_PROPAGATED")
        ) as request,
        pytest.raises(lane.HttpFailure) as caught,
    ):
        access.call(f"/v1/{PARENT}/artifacts:prepareUpload", document={})
    assert caught.value.code == "NOT_YET_PROPAGATED"
    assert request.call_count == 6
    assert len(sleep.call_args_list) == 5


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
    access.expiry = time.time() + 600
    access.prepare.side_effect = preparations
    observed = []

    def command(command, child_env):
        root = Path(child_env["LANE_ROOT"])
        observed.append(command)
        assert child_env["SOURCE_SHA"] == "a" * 40
        assert child_env["SOURCE_WORKFLOW"] == env["GITHUB_WORKFLOW_REF"]
        assert child_env["SOURCE_RUN_ID"] == "123"
        assert child_env["SOURCE_RUN_ATTEMPT"] == "2"
        assert child_env["ARTIFACT_DRY_RUN"] == "false"
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
            assert exchange.call_count == 1
            assert access.prepare.call_count == 2
            assert [call.args[1] for call in push.call_args_list] == [
                f"{IMAGE}:up_runtime",
                f"{IMAGE}:up_evidence",
            ]
            shutil.rmtree(next(tmp_path.glob("artifact-image-*")))
            access.expiry = time.time() + 300
            access.prepare.side_effect = preparations
            exchange.reset_mock()
            access.prepare.reset_mock()
            push.reset_mock()
            observed.clear()
            lane.run()
            assert exchange.call_count == 2
            assert access.prepare.call_count == 3
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


def dry_run_environment(tmp_path, monkeypatch, event="pull_request"):
    values = {
        "INPUT_DRY_RUN": "true", "GITHUB_EVENT_NAME": event,
        "INPUT_PROJECT": PARENT, "INPUT_SOURCE_SHA": "a" * 40,
        "GITHUB_SHA": "a" * 40, "GITHUB_REPOSITORY": "owner/source",
        "GITHUB_REPOSITORY_ID": "42", "GITHUB_REF": "refs/pull/7/merge",
        "GITHUB_WORKFLOW_REF": "owner/source/.github/workflows/image.yml@refs/pull/7/merge",
        "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2",
        "RUNNER_TEMP": str(tmp_path), "GITHUB_OUTPUT": str(tmp_path / "output"),
        "INPUT_CI_NAME": "test-ci", "INPUT_PLATFORMS": "linux/amd64",
        "INPUT_BUILD_COMMAND": "same-build", "INPUT_EVIDENCE_COMMAND": "same-evidence",
        "INPUT_VERIFY_COMMAND": "remote-verify-must-not-run",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "private-oidc-request",
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.invalid/token",
        "GH_TOKEN": "private-github-token", "GITHUB_TOKEN": "private-github-token",
        "SYLPHX_API_KEY": "private-existing-api-key",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return values


@pytest.mark.parametrize("event", ["pull_request", "merge_group", "push"])
def test_dry_run_reuses_build_and_evidence_with_zero_writes_and_exact_manifest(
    tmp_path, monkeypatch, event,
):
    source = dry_run_environment(tmp_path, monkeypatch, event)
    observed = []

    class ReadOnlyAccess:
        def prepare(self, parent):
            pytest.fail("dry run attempted an upload reservation")

        def call(self, path, **kwargs):
            if kwargs.get("document") is not None:
                pytest.fail("dry run attempted an API write")
            return {}

    def command(command, env):
        observed.append(command)
        assert env["ARTIFACT_DRY_RUN"] == "true"
        assert env["GITHUB_EVENT_NAME"] == event
        assert env["GITHUB_REF"] == source["GITHUB_REF"]
        assert env["SOURCE_WORKFLOW"] == source["GITHUB_WORKFLOW_REF"]
        assert env["IMAGE_TAG"] == "dry-run-aaaaaaaaaaaa-runtime"
        assert env["BUILD_EVIDENCE_TAG"] == "dry-run-aaaaaaaaaaaa-evidence"
        assert not any(Path(env["DOCKER_CONFIG"]).iterdir())
        for private in ("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_URL",
                        "GH_TOKEN", "GITHUB_TOKEN", "SYLPHX_API_KEY", "ARTIFACT_REGISTRY_AUTH_FILE"):
            assert private not in env
        if command == "same-build":
            make_layout(Path(env["OCI_DIR"]))
        else:
            assert command == "same-evidence"
            (Path(env["BUILD_EVIDENCE_DIR"]) / "nested").mkdir()
            (Path(env["BUILD_EVIDENCE_DIR"]) / "nested/proof.json").write_bytes(b'{"proved":true}\n')
            make_layout(Path(env["BUILD_EVIDENCE_OCI_DIR"]), "evidence")

    with (
        mock.patch.object(lane, "Access", return_value=ReadOnlyAccess()) as access,
        mock.patch.object(lane, "request", side_effect=AssertionError("external HTTP request")) as request,
        mock.patch.object(lane, "push", side_effect=AssertionError("registry write")) as push,
        mock.patch.object(lane, "register", side_effect=AssertionError("Artifact write")) as register,
        mock.patch.object(lane, "write_auth", side_effect=AssertionError("credential materialization")) as auth,
        mock.patch.object(lane, "readback", side_effect=AssertionError("remote readback")) as readback,
        mock.patch.object(lane, "shell", side_effect=command),
        mock.patch.object(lane.platform, "machine", return_value="x86_64"),
        mock.patch.object(lane.shutil, "which", return_value="/fixture/tool"),
        mock.patch.object(lane.subprocess, "check_output", return_value="a" * 40),
    ):
        lane.run()
    for external in (access, request, push, register, auth, readback):
        external.assert_not_called()
    assert observed == ["same-build", "same-evidence"]
    root = next(tmp_path.glob("artifact-image-*"))
    assert not (root / "registry-auth").exists()
    outputs = dict(line.split("=", 1) for line in (tmp_path / "output").read_text().splitlines())
    assert set(outputs) == {"_root", "dry_run", "dry_run_manifest"}
    assert outputs["dry_run"] == "true"
    manifest = json.loads(outputs["dry_run_manifest"])
    assert manifest == json.loads((root / "dry-run-manifest.json").read_text())
    assert manifest["dryRun"] is True and manifest["published"] is False
    assert manifest["artifactsRegistered"] is False and manifest["uploadTagsReserved"] is False
    assert manifest["authenticatedManifestReadback"] is False and manifest["promotionAuthorized"] is False
    assert manifest["access"]["checked"] is False
    assert manifest["remoteVerification"].startswith("skipped:")
    assert manifest["target"] == {
        "project": PARENT, "image": IMAGE, "ciName": "test-ci", "sourceSha": "a" * 40,
        "platforms": "linux/amd64", "repository": "owner/source", "repositoryID": "42",
        "workflow": source["GITHUB_WORKFLOW_REF"], "event": event, "ref": "refs/pull/7/merge",
        "runID": "123", "runAttempt": "2",
    }
    for key, directory in (("runtime", "image-oci"), ("evidence", "evidence-oci")):
        descriptor, raw = lane.local_manifest(root / directory)
        assert manifest[key]["digest"] == descriptor["digest"] == lane.sha256(raw)
        assert manifest[key]["imageReference"] == IMAGE + "@" + descriptor["digest"]
        assert manifest[key]["artifactName"] == lane.artifact_name(PARENT, descriptor["digest"])
    assert manifest["evidence"]["subject"] == manifest["runtime"]["artifactName"]
    assert manifest["evidence"]["files"] == {
        "nested/proof.json": {"digest": lane.sha256(b'{"proved":true}\n'), "sizeBytes": "16"},
    }
    assert all(secret not in outputs["dry_run_manifest"] for secret in
               ("private-oidc-request", "private-github-token", "private-existing-api-key"))


def test_dry_run_shipped_entrypoint_executes_real_local_commands_without_oidc(tmp_path, monkeypatch):
    dry_run_environment(tmp_path, monkeypatch)
    fixture = tmp_path / "fixture"
    make_layout(fixture / "runtime")
    make_layout(fixture / "evidence-oci", "evidence")
    (fixture / "evidence").mkdir()
    (fixture / "evidence/proof.json").write_bytes(b"real fixture\n")
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "skopeo").write_text("#!/bin/sh\nexit 91\n")
    (tools / "skopeo").chmod(0o700)
    source = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    env = {
        **os.environ,
        "GITHUB_SHA": source, "INPUT_SOURCE_SHA": source,
        "PREVIEW_FIXTURE": str(fixture), "PATH": str(tools) + os.pathsep + os.environ["PATH"],
        "INPUT_BUILD_COMMAND": 'test -z "${ACTIONS_ID_TOKEN_REQUEST_TOKEN+x}"; '
                               'cp -a "$PREVIEW_FIXTURE/runtime/." "$OCI_DIR/"',
        "INPUT_EVIDENCE_COMMAND": 'cp -a "$PREVIEW_FIXTURE/evidence-oci/." "$BUILD_EVIDENCE_OCI_DIR/"; '
                                  'cp -a "$PREVIEW_FIXTURE/evidence/." "$BUILD_EVIDENCE_DIR/"',
        "INPUT_VERIFY_COMMAND": "",
    }
    result = subprocess.run([lane.sys.executable, str(ACTION / "publish.py")], cwd=ROOT,
                            env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "access_check=skipped" in result.stdout
    assert "remote_verification=skipped published=false" in result.stdout
    assert "stage=publish" not in result.stdout and "stage=verify" not in result.stdout
    outputs = dict(line.split("=", 1) for line in (tmp_path / "output").read_text().splitlines())
    assert set(outputs) == {"_root", "dry_run", "dry_run_manifest"}
    manifest = json.loads(outputs["dry_run_manifest"])
    assert manifest["target"]["event"] == "pull_request"
    assert manifest["target"]["sourceSha"] == source
    assert manifest["runtime"]["digest"] == lane.local_manifest(fixture / "runtime")[0]["digest"]
    assert manifest["evidence"]["files"]["proof.json"]["digest"] == lane.sha256(b"real fixture\n")


@pytest.mark.parametrize("failure", ["build", "evidence", "changed-blob", "symlink", "oversized-manifest"])
def test_dry_run_failure_emits_no_manifest_or_promotion_output(tmp_path, monkeypatch, failure):
    dry_run_environment(tmp_path, monkeypatch)
    if failure == "oversized-manifest":
        monkeypatch.setenv("GITHUB_WORKFLOW_REF", "w" * 524289)

    def command(command, env):
        if failure == {"same-build": "build", "same-evidence": "evidence"}[command]:
            raise RuntimeError("fixture command failure")
        if command == "same-build":
            layout = Path(env["OCI_DIR"])
            make_layout(layout)
            if failure == "changed-blob":
                next((layout / "blobs/sha256").iterdir()).write_bytes(b"changed")
        else:
            evidence = Path(env["BUILD_EVIDENCE_DIR"])
            if failure == "symlink":
                (evidence / "foreign-proof").symlink_to(tmp_path / "private-file")
            else:
                (evidence / "proof.json").write_text("{}")
            make_layout(Path(env["BUILD_EVIDENCE_OCI_DIR"]), "evidence")

    with (
        mock.patch.object(lane, "Access", side_effect=AssertionError("Access attempted")),
        mock.patch.object(lane, "shell", side_effect=command),
        mock.patch.object(lane.platform, "machine", return_value="x86_64"),
        mock.patch.object(lane.shutil, "which", return_value="/fixture/tool"),
        mock.patch.object(lane.subprocess, "check_output", return_value="a" * 40),
        pytest.raises((RuntimeError, ValueError)),
    ):
        lane.run()
    outputs = dict(line.split("=", 1) for line in (tmp_path / "output").read_text().splitlines())
    assert set(outputs) == {"_root"}
    assert not (next(tmp_path.glob("artifact-image-*")) / "dry-run-manifest.json").exists()


@pytest.mark.parametrize("value", ["TRUE", "true ", "1", "yes", ""])
def test_dry_run_rejects_ambiguous_boolean_before_access(monkeypatch, value):
    monkeypatch.setenv("INPUT_DRY_RUN", value)
    with mock.patch.object(lane, "Access") as access, pytest.raises(ValueError, match="dry-run"):
        lane.run()
    access.assert_not_called()


@pytest.mark.parametrize("dry,missing", [("true", "INPUT_BUILD_COMMAND"),
                                           ("true", "INPUT_EVIDENCE_COMMAND"),
                                           ("false", "INPUT_VERIFY_COMMAND")])
def test_required_commands_fail_before_any_credential_or_mutation(tmp_path, monkeypatch, dry, missing):
    dry_run_environment(tmp_path, monkeypatch, "push")
    monkeypatch.setenv("INPUT_DRY_RUN", dry)
    monkeypatch.delenv(missing)
    with (
        mock.patch.object(lane, "Access") as access,
        mock.patch.object(lane.platform, "machine", return_value="x86_64"),
        mock.patch.object(lane.shutil, "which", return_value="/fixture/tool"),
        mock.patch.object(lane.subprocess, "check_output", return_value="a" * 40),
        pytest.raises(ValueError, match="required"),
    ):
        lane.run()
    access.assert_not_called()
    assert not (tmp_path / "output").exists()


def test_dry_run_defaults_false_and_keeps_publication_push_only(monkeypatch):
    monkeypatch.delenv("INPUT_DRY_RUN", raising=False)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    with mock.patch.object(lane, "Access") as access, pytest.raises(ValueError, match="push events only"):
        lane.run()
    access.assert_not_called()


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
    assert action["inputs"]["dry-run"]["default"] == "false"
    assert action["inputs"]["verify-command"]["required"] is False
    assert action["runs"]["steps"][0]["env"]["INPUT_DRY_RUN"] == "${{ inputs.dry-run }}"
    assert {"dry_run", "dry_run_manifest"} <= set(action["outputs"])
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
