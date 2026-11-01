#!/usr/bin/env python3
"""Build and register OCI bytes through the public Sylphx Artifact API."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

API = "https://api.sylphx.com"
REGISTRY = "registry.sylphx.com"
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
PARENT = re.compile(r"orgs/([a-z0-9_-]{1,128})/projects/([a-z0-9_-]{1,128})\Z")
TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z")
INDEX_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
)
IMAGE_TYPES = (
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
)
MANIFEST_TYPES = INDEX_TYPES + IMAGE_TYPES
# Public Artifact prepareUpload mint cap (kernel/artifacts).
MAX_REGISTRY_TOKEN_SECONDS = 3600


class HttpFailure(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(f"public Artifact request refused (HTTP {status})")
        self.status, self.code = status, code


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(url: str, *, method: str = "GET", headers=None, document=None):
    data = None if document is None else json.dumps(document).encode()
    supplied = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=supplied, method=method)
    try:
        with urllib.request.build_opener(NoRedirect()).open(
            req, timeout=30
        ) as response:
            if response.geturl() != url:
                raise ValueError("response changed authority")
            return response.headers, response.read(8 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as error:
        # Only the machine code is retained. Error messages/bodies can contain secrets.
        try:
            body = json.loads(error.read(65536))
            code = body.get("code", body.get("error", {}).get("code", ""))
        except (ValueError, AttributeError, TypeError):
            code = ""
        raise HttpFailure(error.code, code) from None


def json_request(url, **kwargs):
    _, raw = request(url, **kwargs)
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("API response exceeds limit")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("API response must be an object")
    return value


def field(value, name):
    """The generated public client emits snake_case and accepts ProtoJSON aliases."""
    words = name.split("_")
    camel = words[0] + "".join(word.title() for word in words[1:])
    if name in value and camel in value and value[name] != value[camel]:
        raise ValueError("response contains conflicting aliases")
    return value.get(name, value.get(camel))


def claims(token):
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        raise ValueError("registry credential is not a JWT")
    value = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    if not isinstance(value, dict):
        raise ValueError("registry credential claims are malformed")
    return value


def expected_image(parent):
    match = PARENT.fullmatch(parent)
    if not match:
        raise ValueError("project must be orgs/{org-id}/projects/{project-id}")
    return f"{REGISTRY}/{match[1]}/{match[2]}-image"


def validate_upload(value, parent, now=None):
    now = time.time() if now is None else now
    image = expected_image(parent)
    ref = field(value, "image_reference")
    if (
        not isinstance(ref, str)
        or not ref.startswith(image + ":")
        or not TAG.fullmatch(ref[len(image) + 1 :])
    ):
        raise ValueError(
            "upload reference does not belong to this project's managed repository"
        )
    auth = json.loads(field(value, "registry_auth"))
    if set(auth) != {"auths"} or set(auth["auths"]) != {REGISTRY}:
        raise ValueError("upload credentials name another registry")
    record = auth["auths"][REGISTRY]
    if set(record) != {"identitytoken"} or not isinstance(record["identitytoken"], str):
        raise ValueError(
            "upload credentials must contain only the returned identity token"
        )
    token = record["identitytoken"]
    decoded = claims(token)
    # These are fail-closed scope checks, not local signature verification.
    # TLS authenticates the API answer; the registry verifies the signed credential.
    issued, expiry = decoded.get("iat"), decoded.get("exp")
    audience = decoded.get("aud")
    if audience != "container_registry" and audience != ["container_registry"]:
        raise ValueError("registry credential audience mismatch")
    if (
        type(issued) is not int
        or type(expiry) is not int
        or issued > now + 30
        or expiry <= now + 30
        or not 0 < expiry - issued <= MAX_REGISTRY_TOKEN_SECONDS
    ):
        raise ValueError("registry credential lifetime mismatch")
    grants = decoded.get("access")
    required = {
        "type": "repository",
        "name": image.split("/", 1)[1],
        "actions": ["pull", "push"],
    }
    if (
        not isinstance(grants, list)
        or len(grants) != 1
        or not isinstance(grants[0], dict)
    ):
        raise ValueError("registry credential must contain one repository grant")
    grant = grants[0]
    if (
        grant.get("type") != required["type"]
        or grant.get("name") != required["name"]
        or sorted(grant.get("actions", [])) != required["actions"]
    ):
        raise ValueError("registry credential grant differs from the upload repository")
    declared = datetime.fromisoformat(field(value, "expires_at").replace("Z", "+00:00"))
    # prepareUpload reports receipt-time + expires_in; the signed JWT uses
    # issuer time. Allow bounded request latency/clock skew, never extra TTL.
    if (
        declared.tzinfo is None
        or not now + 30 < declared.timestamp() <= now + MAX_REGISTRY_TOKEN_SECONDS + 30
        or abs(declared.timestamp() - expiry) > 30
    ):
        raise ValueError("upload expiry does not match its credential")
    return ref, token, auth


def write_auth(directory, token, auth):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    encoded = base64.b64encode(f"oauth2accesstoken:{token}".encode()).decode()
    for name, value in (
        ("config.json", auth),
        ("skopeo-auth.json", {"auths": {REGISTRY: {"auth": encoded}}}),
    ):
        path = directory / name
        # Owner-only files, also on self-hosted runners with a permissive umask.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream)


class Access:
    def __init__(self, ci_name):
        self.key = ""
        url = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"]
        parsed = urllib.parse.urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise ValueError("GitHub OIDC endpoint must be HTTPS")
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        query = [(key, value) for key, value in query if key != "audience"] + [
            ("audience", "sylphx-access")
        ]
        url = urllib.parse.urlunsplit(
            parsed._replace(query=urllib.parse.urlencode(query))
        )
        oidc = json_request(
            url,
            headers={
                "Authorization": "Bearer "
                + os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]
            },
        )
        token = oidc.get("value")
        if not isinstance(token, str) or not token:
            raise ValueError("GitHub OIDC response has no token")
        answer = json_request(
            API + "/v1/access/github/token",
            method="POST",
            document={"name": ci_name, "token": token},
        )
        key = answer.get("api_key")
        wait = answer.get("active_after_ms", 0)
        if not isinstance(key, str) or not key or any(c.isspace() for c in key):
            raise ValueError("Access response has no API key")
        if type(wait) is not int or not 0 <= wait <= 30000:
            raise ValueError("Access activation delay is invalid")
        expiry = datetime.fromisoformat(answer["expires_at"].replace("Z", "+00:00"))
        if expiry.tzinfo is None or expiry.timestamp() <= time.time() + wait / 1000:
            raise ValueError("Access returned an expired key")
        self.key = key
        time.sleep(wait / 1000)

    def call(self, path, *, document=None, idempotency=None):
        headers = {"Authorization": "Bearer " + self.key}
        if idempotency:
            headers["Idempotency-Key"] = idempotency
        return json_request(
            API + path,
            method="GET" if document is None else "POST",
            headers=headers,
            document=document,
        )

    def prepare(self, parent):
        return validate_upload(
            self.call(f"/v1/{parent}/artifacts:prepareUpload", document={}), parent
        )


def sha256(body):
    return "sha256:" + hashlib.sha256(body).hexdigest()


def local_manifest(directory):
    """Resolve containers/image's OCI transport entry, with a verified blob graph."""
    if json.loads((directory / "oci-layout").read_bytes()) != {
        "imageLayoutVersion": "1.0.0"
    }:
        raise ValueError("unsupported OCI layout")
    index = json.loads((directory / "index.json").read_bytes())
    entries = index.get("manifests")
    if not isinstance(entries, list) or len(entries) != 1:
        raise ValueError(
            "OCI layout must name one transport entry (wrap multiple manifests in an index)"
        )

    def resolve(descriptor, depth=0):
        if (
            depth > 8
            or not isinstance(descriptor, dict)
            or not DIGEST.fullmatch(descriptor.get("digest", ""))
        ):
            raise ValueError("invalid OCI descriptor graph")
        path = directory / "blobs" / "sha256" / descriptor["digest"][7:]
        with path.open("rb") as stream:
            digest = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
        if (
            digest != descriptor["digest"]
            or type(descriptor.get("size")) is not int
            or descriptor["size"] != path.stat().st_size
        ):
            raise ValueError("OCI blob digest/size mismatch")
        media = descriptor.get("mediaType")
        body = b""
        if media in MANIFEST_TYPES:
            if path.stat().st_size > 8 * 1024 * 1024:
                raise ValueError("OCI manifest exceeds limit")
            body = path.read_bytes()
        if media in INDEX_TYPES:
            doc = json.loads(body)
            children = doc.get("manifests")
            if not isinstance(children, list) or not children:
                raise ValueError("OCI index has no manifests")
            for child in children:
                resolve(child, depth + 1)
        elif media in IMAGE_TYPES:
            doc = json.loads(body)
            if not isinstance(doc.get("config"), dict) or not isinstance(
                doc.get("layers"), list
            ):
                raise ValueError("OCI manifest has no config/layers")
            for child in [doc["config"], *doc["layers"]]:
                resolve(child, depth + 1)
        return body

    descriptor = entries[0]
    body = resolve(descriptor)
    if descriptor.get("mediaType") not in MANIFEST_TYPES:
        raise ValueError("OCI transport entry must be an image or image index")
    return descriptor, body


def readback(image, tag, token, descriptor, local_body, output):
    base = f"https://{REGISTRY}/v2/{image.split('/', 1)[1]}/manifests/"
    headers = {"Authorization": "Bearer " + token, "Accept": ",".join(MANIFEST_TYPES)}
    head, _ = request(base + tag, method="HEAD", headers=headers)
    digest = head.get("Docker-Content-Digest", "")
    media = head.get("Content-Type", "").split(";", 1)[0].strip()
    if not DIGEST.fullmatch(digest) or media not in MANIFEST_TYPES:
        raise ValueError("manifest HEAD did not select a supported digest/media type")
    response, body = request(base + digest, headers=headers)
    if (
        len(body) > 8 * 1024 * 1024
        or response.get("Docker-Content-Digest") != digest
        or response.get("Content-Type", "").split(";", 1)[0].strip() != media
        or sha256(body) != digest
    ):
        raise ValueError("digest GET disagrees with manifest HEAD")
    # --preserve-digests means the registry must hold the exact OCI transport
    # entry, not just an envelope with similar child digests.
    if (
        digest != descriptor["digest"]
        or media != descriptor["mediaType"]
        or body != local_body
    ):
        raise ValueError("registry manifest differs from locally built bytes")
    output.write_bytes(body)
    return digest, media, len(body)


def artifact_name(parent, digest):
    return f"{parent}/artifacts/{digest.replace(':', '-')}"


def register(access, parent, image, descriptor, subject=""):
    digest, media, size = descriptor
    name = artifact_name(parent, digest)
    body = {
        "digest": digest,
        "kind": "image",
        "media_type": media,
        "size_bytes": str(size),
    }
    if subject:
        body["subject"] = subject
    key = (
        "artifact-image-"
        + hashlib.sha256((parent + digest + subject).encode()).hexdigest()
    )

    def validate(record):
        if (
            record.get("name") != name
            or record.get("digest") != digest
            or record.get("kind") != "image"
            or record.get("reference") != f"{image}@{digest}"
            or field(record, "media_type") != media
            or str(field(record, "size_bytes")) != str(size)
            or record.get("subject", "") != subject
            or not record.get("uid")
        ):
            raise ValueError("registered Artifact does not match the managed upload")
        meta = record.get("meta") or {}
        if field(meta, "delete_time") or field(record, "verification_state") in (
            "failed",
            "VERIFICATION_STATE_FAILED",
        ):
            raise ValueError("registered Artifact is deleted or failed")

    try:
        validate(
            access.call(
                f"/v1/{parent}/artifacts?managed_upload=true",
                document=body,
                idempotency=key,
            )
        )
    except HttpFailure as error:
        if error.status != 409 or error.code != "RESOURCE_ALREADY_EXISTS":
            raise
    # Always read the durable record, including after an idempotent POST.
    record = access.call(f"/v1/{name}")
    validate(record)
    return name


def shell(command, env):
    subprocess.run(["bash", "-e", "-o", "pipefail", "-c", command], env=env, check=True)


def push(directory, reference, auth_file):
    # Tool failures do not echo provider-supplied credentials into logs.
    result = subprocess.run(
        [
            "skopeo",
            "copy",
            "--authfile",
            str(auth_file),
            "--all",
            "--preserve-digests",
            f"oci:{directory}",
            f"docker://{reference}",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode:
        raise RuntimeError("skopeo push failed; no promotion receipt was produced")


def receipt(directory):
    if not any(directory.iterdir()):
        raise ValueError("verify-command produced no receipt")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        archive.add(directory, arcname=".")
    encoded = base64.b64encode(buffer.getvalue()).decode()
    if len(encoded) > 524288:
        raise ValueError("verification receipt exceeds 524288 encoded bytes")
    return encoded


def output(values):
    with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
        for key, value in values.items():
            if "\n" in str(value) or "\r" in str(value):
                raise ValueError("action output contains a newline")
            stream.write(f"{key}={value}\n")


def cleanup(root):
    path = Path(root).resolve()
    if path.parent != Path(
        os.environ["RUNNER_TEMP"]
    ).resolve() or not path.name.startswith("artifact-image-"):
        raise ValueError("refusing cleanup outside the action temporary directory")
    shutil.rmtree(path / "registry-auth", ignore_errors=True)


def run():
    # Refuse PRs before reading an OIDC endpoint or creating any credential.
    if os.environ.get("GITHUB_EVENT_NAME") != "push":
        raise ValueError("artifact-image-publish admits push events only")
    parent = os.environ["INPUT_PROJECT"]
    image = expected_image(parent)
    source = os.environ.get("INPUT_SOURCE_SHA") or os.environ["GITHUB_SHA"]
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", source):
        raise ValueError("source-sha must be a full source commit")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if head != source:
        raise ValueError("caller checkout does not match source-sha")
    native = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(platform.machine())
    platforms = os.environ.get("INPUT_PLATFORMS", "linux/amd64")
    if native is None or platforms != f"linux/{native}":
        raise ValueError("platforms must equal the runner's native Linux platform")
    for tool in ("bash", "skopeo"):
        if not shutil.which(tool):
            raise ValueError(f"required publisher tool is missing: {tool}")
    commands = [
        os.environ.get("INPUT_" + name + "_COMMAND", "")
        for name in ("BUILD", "EVIDENCE", "VERIFY")
    ]
    if not all(command.strip() for command in commands):
        raise ValueError(
            "build-command, evidence-command and verify-command are required"
        )
    root = Path(
        tempfile.mkdtemp(prefix="artifact-image-", dir=os.environ["RUNNER_TEMP"])
    )
    output({"_root": root})  # Internal step output for the always() cleanup only.
    auth_dir = root / "registry-auth"
    env = dict(os.environ)
    env.update(
        {
            "LANE_ROOT": str(root),
            "IMAGE": image,
            "PLATFORMS": platforms,
            "SOURCE_SHA": source,
            "SOURCE_WORKFLOW": os.environ["GITHUB_WORKFLOW_REF"],
            "SOURCE_RUN_ID": os.environ["GITHUB_RUN_ID"],
            "SOURCE_RUN_ATTEMPT": os.environ["GITHUB_RUN_ATTEMPT"],
            "OCI_DIR": str(root / "image-oci"),
            "BUILD_EVIDENCE_DIR": str(root / "evidence"),
            "EVIDENCE_DIR": str(root / "evidence"),
            "BUILD_EVIDENCE_OCI_DIR": str(root / "evidence-oci"),
            "READBACK_DIR": str(root / "readback"),
            "DOCKER_CONFIG": str(auth_dir),
            "ARTIFACT_REGISTRY_AUTH_FILE": str(auth_dir / "skopeo-auth.json"),
        }
    )
    for directory in ("image-oci", "evidence", "evidence-oci", "readback", "verify"):
        (root / directory).mkdir()
    access = None
    try:
        print("artifact_publish stage=prepare-build", flush=True)
        access = Access(os.environ["INPUT_CI_NAME"])
        image_ref, token, auth = access.prepare(parent)
        evidence_ref, evidence_token, evidence_auth = access.prepare(parent)
        if evidence_ref == image_ref:
            raise ValueError("runtime and evidence upload references must be distinct")
        env.update(
            {
                "IMAGE_TAG": image_ref.rsplit(":", 1)[1],
                "BUILD_EVIDENCE_TAG": evidence_ref.rsplit(":", 1)[1],
            }
        )
        write_auth(auth_dir, evidence_token, evidence_auth)
        access.key = ""
        token = evidence_token = ""
        auth = evidence_auth = None
        print("artifact_publish stage=build", flush=True)
        shell(commands[0], env)
        print("artifact_publish stage=package-evidence", flush=True)
        shell(commands[1], env)
        if not any((root / "evidence").iterdir()):
            raise ValueError("evidence-command produced no evidence bundle")
        runtime = local_manifest(root / "image-oci")
        evidence = local_manifest(root / "evidence-oci")
        # Fresh public credentials after a possibly long build. The original
        # unique server-issued upload references remain the publish targets.
        print("artifact_publish stage=publish", flush=True)
        access = Access(os.environ["INPUT_CI_NAME"])
        _, token, auth = access.prepare(parent)
        write_auth(auth_dir, token, auth)
        push(root / "image-oci", image_ref, auth_dir / "skopeo-auth.json")
        push(root / "evidence-oci", evidence_ref, auth_dir / "skopeo-auth.json")
        runtime_desc = readback(
            image, env["IMAGE_TAG"], token, *runtime, root / "readback/image.json"
        )
        evidence_desc = readback(
            image,
            env["BUILD_EVIDENCE_TAG"],
            token,
            *evidence,
            root / "readback/evidence.json",
        )
        print("artifact_publish stage=register", flush=True)
        name = register(access, parent, image, runtime_desc)
        evidence_name = register(access, parent, image, evidence_desc, subject=name)
        access.key = ""
        token = evidence_token = ""
        auth = evidence_auth = None
        cleanup(root)
        env.pop("ARTIFACT_REGISTRY_AUTH_FILE")
        env["DOCKER_CONFIG"] = str(root / "verifier-docker")
        env.update(
            {
                "REGISTRY_DIGEST": runtime_desc[0],
                "BUILD_EVIDENCE_DIGEST": evidence_desc[0],
                "IMAGE_REGISTRY_MANIFEST": str(root / "readback/image.json"),
                "EVIDENCE_REGISTRY_MANIFEST": str(root / "readback/evidence.json"),
            }
        )
        print("artifact_publish stage=verify", flush=True)
        shell(commands[2], env)
        encoded = receipt(root / "verify")
        output(
            {
                "image": image,
                "image_tag": env["IMAGE_TAG"],
                "digest": runtime_desc[0],
                "image_reference": f"{image}@{runtime_desc[0]}",
                "artifact": name,
                "evidence_tag": env["BUILD_EVIDENCE_TAG"],
                "evidence_digest": evidence_desc[0],
                "evidence_reference": f"{image}@{evidence_desc[0]}",
                "evidence_artifact": evidence_name,
                "verify_receipt": encoded,
            }
        )
    finally:
        if access is not None:
            access.key = ""
        cleanup(root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cleanup")
    args = parser.parse_args()
    if args.cleanup:
        cleanup(args.cleanup)
        return 0
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda signum, frame: sys.exit(128 + signum))
    try:
        run()
    except HttpFailure as error:
        print(str(error), file=sys.stderr)
        return 1
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        RuntimeError,
        subprocess.SubprocessError,
        urllib.error.URLError,
    ):
        # Never include arbitrary provider bodies, credentials or argv in errors.
        print(
            "Artifact image publication failed validation, command execution or transport; no promotion output was produced",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
