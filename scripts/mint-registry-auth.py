#!/usr/bin/env python3
"""Mint a repository-scoped registry credential from the runner's SPIFFE identity.

The GitHub job receives no registry secret. Its repository-restricted runner
projects the SPIFFE Workload API socket and a pinned SPIRE agent binary when the
platform's repository publish grant matches the exact demand (repository,
workflow, job, event, branch). This program fetches the exact publisher
JWT-SVID, exchanges it for one short-lived repository JWT, writes tool-specific
auth files, and never prints either token.

Use `--probe` for a fail-fast pre-flight: mint, validate the exact grant, print
the granted repository, and discard the credential. A plain class runner (no
Workload API projection) fails here in seconds instead of after a long build.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_REGISTRY_HOST = "registry.sylphx.com"
DEFAULT_MINT_URL = "https://registry.sylphx.com/token"
REGISTRY_TOKEN_SERVICE = "container_registry"
SPIFFE_AUDIENCE = "registry-v2-token"
SPIFFE_ID = "spiffe://sylphx.local/role/registry-token-minter"
# The per-grant form binds the SVID to one grant and the one registry
# repository it may publish: <SPIFFE_ID>/grant/<label>/repository/<repo>.
# SPIRE issues it from the publisher pod's grant label and repository
# annotation; the registry issuer refuses any other repository for it.
GRANT_SEGMENT = "/grant/"
REPOSITORY_SEGMENT = "/repository/"
SPIFFE_WORKLOAD_API_SOCKET = Path("/spiffe-workload-api/spire-agent.sock")
SPIRE_AGENT_IMAGE_BIN = Path("/opt/spire-from-image/opt/spire/bin/spire-agent")
# JWT-SVID lifetime budget for the CI publisher identity.
#
# The runner's SPIRE registration renders a JWT-SVID from the cluster default
# (`default_jwt_svid_ttl`, 1h here), not from the TTL a caller pins on one
# ClusterSPIFFEID: that pin is per object and the pre-flight identity is not the
# object it belongs to. A budget tuned to a requested 10m therefore refuses the
# lifetime SPIRE actually mints, and every publisher pre-flight dies before the
# registry issuer is ever contacted. The budget must cover the *rendered*
# lifetime; it stays far inside what the registry issuer will exchange, and both
# bounds stay fail-closed on a token that is already expired or not yet valid.
MAX_SVID_LIFETIME_SECONDS = 3600
MAX_REGISTRY_TOKEN_LIFETIME_SECONDS = 900
DEFAULT_TIMEOUT_SECONDS = 20
# How long the runner's identity may take to appear. SPIRE issues the
# publisher SVID only after spire-controller-manager registers the runner Pod
# and the node agent syncs the entry: seconds when healthy, but live
# 2026-09-24 it took 40 s+ while the controller-manager restarted on
# API-server resets, and every image lane failed ("SPIRE Workload API refused
# the publisher identity") against a 20 s budget. The wait is for identity
# propagation only; each HTTP call keeps DEFAULT_TIMEOUT_SECONDS.
DEFAULT_IDENTITY_WAIT_SECONDS = 180
# The token exchange is retried on a transient failure only: a connect or read
# timeout, a connection error, or an HTTP 5xx. A 4xx is the issuer's verdict on
# this publisher and fails at once. Live 2026-10-05 a single 20 s connect
# timeout to the issuer failed whole main image builds while the issuer itself
# was healthy. Worst case: MINT_ATTEMPTS x the HTTP timeout plus the backoffs.
MINT_ATTEMPTS = 3
MINT_BACKOFF_SECONDS = (2.0, 5.0)
# Trusted publishing: the job's own GitHub Actions OIDC token, requested for
# the registry's audience and exchanged at the token service, which matches it
# against the declared publishers (SylphxAI/infra registry-v2 README).
GITHUB_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
GITHUB_OIDC_AUDIENCE = "registry.sylphx.com"
MAX_GITHUB_OIDC_LIFETIME_SECONDS = 3600


def _b64url_json(segment: str) -> dict[str, Any]:
    padding = "=" * (-len(segment) % 4)
    decoded = base64.urlsafe_b64decode(segment + padding)
    value = json.loads(decoded)
    if not isinstance(value, dict):
        raise ValueError("JWT payload is not an object")
    return value


def _jwt_claims(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3 or not token.startswith("eyJ") or not all(parts):
        raise ValueError("credential is not a compact JWT")
    return _b64url_json(parts[1])


def _audiences(claims: dict[str, Any]) -> list[str]:
    audience = claims.get("aud")
    if isinstance(audience, str):
        return [audience]
    if isinstance(audience, list) and all(isinstance(value, str) for value in audience):
        return audience
    return []


def split_image_reference(image: str) -> tuple[str, str]:
    """`registry.sylphx.com/library/name` -> (host, repository)."""
    reference = image.strip()
    if "://" in reference or "@" in reference:
        raise ValueError("image must be a bare host/repository reference")
    host, _, repository = reference.partition("/")
    if not host or not repository:
        raise ValueError("image must include a registry host and a repository path")
    if ":" not in host and "." not in host:
        raise ValueError("image must name a registry host, not a bare repository")
    if repository.startswith("/") or repository.endswith("/") or "//" in repository:
        raise ValueError("image repository path is not canonical")
    if any(segment in {"", ".", ".."} for segment in repository.split("/")):
        raise ValueError("image repository path has an empty or relative segment")
    return host, repository


def _publisher_subject_ok(subject: object, repository: str | None) -> bool:
    """The fixed publisher identity (transition), or the per-grant identity that
    names exactly the repository this lane publishes."""
    if subject == SPIFFE_ID:
        return True
    if not isinstance(subject, str) or not subject.startswith(SPIFFE_ID + GRANT_SEGMENT):
        return False
    rest = subject[len(SPIFFE_ID + GRANT_SEGMENT):]
    label, sep, named = rest.partition(REPOSITORY_SEGMENT)
    if not sep or not label or "/" in label:
        return False
    return repository is None or named == repository


def _validate_svid(token: str, now: int, repository: str | None = None) -> None:
    claims = _jwt_claims(token)
    if not _publisher_subject_ok(claims.get("sub"), repository):
        raise ValueError("JWT-SVID subject does not match the publisher identity")
    if SPIFFE_AUDIENCE not in _audiences(claims):
        raise ValueError("JWT-SVID audience does not authorize the registry issuer")
    issued_at = claims.get("iat")
    expires_at = claims.get("exp")
    if not isinstance(issued_at, int) or not isinstance(expires_at, int):
        raise ValueError("JWT-SVID is missing integer iat/exp")
    if issued_at > now + 30 or expires_at <= now:
        raise ValueError("JWT-SVID is not currently valid")
    if expires_at - issued_at > MAX_SVID_LIFETIME_SECONDS:
        raise ValueError("JWT-SVID lifetime exceeds the publisher contract")


def _validate_registry_token(token: str, now: int, repository: str) -> None:
    claims = _jwt_claims(token)
    if REGISTRY_TOKEN_SERVICE not in _audiences(claims):
        raise ValueError("registry JWT audience mismatch")
    issued_at = claims.get("iat")
    expires_at = claims.get("exp")
    if not isinstance(issued_at, int) or not isinstance(expires_at, int):
        raise ValueError("registry JWT is missing integer iat/exp")
    if issued_at > now + 30 or expires_at <= now:
        raise ValueError("registry JWT is not currently valid")
    if expires_at - issued_at > MAX_REGISTRY_TOKEN_LIFETIME_SECONDS:
        raise ValueError("registry JWT lifetime exceeds the workflow contract")
    grants = claims.get("access")
    if not isinstance(grants, list):
        raise ValueError("registry JWT access claim missing")
    exact = [
        grant
        for grant in grants
        if isinstance(grant, dict)
        and grant.get("type") == "repository"
        and grant.get("name") == repository
    ]
    if len(exact) != 1:
        raise ValueError("registry JWT does not contain exactly one repository grant for this image")
    actions = exact[0].get("actions")
    if not isinstance(actions, list) or set(actions) != {"pull", "push"}:
        raise ValueError("registry JWT grant is not exact pull,push authority")
    if len(grants) != 1:
        raise ValueError("registry JWT contains authority outside the requested repository")


def _copy_agent(agent_source: Path, directory: Path) -> Path:
    if not agent_source.is_file():
        raise RuntimeError(f"SPIRE agent binary missing at {agent_source}")
    destination = directory / "spire-agent"
    shutil.copyfile(agent_source, destination)
    destination.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    return destination


def _all_svids(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        candidate = value.get("svid")
        if isinstance(candidate, str) and candidate.startswith("eyJ"):
            found.append(candidate)
        for nested in value.values():
            if nested is not candidate:
                found.extend(_all_svids(nested))
    elif isinstance(value, list):
        for nested in value:
            found.extend(_all_svids(nested))
    return found


def _pick_svid(document: Any, now: int, repository: str | None) -> str | None:
    """The workload may hold several SVIDs for the audience (fleet and cell
    forms); take the per-grant one naming this repository, else the fixed one."""
    valid: list[tuple[int, str]] = []
    for token in _all_svids(document):
        try:
            _validate_svid(token, now, repository)
        except ValueError:
            continue
        subject = _jwt_claims(token).get("sub")
        valid.append((0 if subject != SPIFFE_ID else 1, token))
    valid.sort(key=lambda item: item[0])
    return valid[0][1] if valid else None


def _fetch_svid(
    agent: Path, socket: Path, timeout_seconds: float, repository: str | None = None
) -> str:
    if not socket.exists():
        raise RuntimeError(f"SPIFFE Workload API socket missing at {socket}")
    deadline = time.monotonic() + timeout_seconds
    last_error = "publisher identity unavailable"
    while True:
        try:
            completed = subprocess.run(
                [
                    str(agent),
                    "api",
                    "fetch",
                    "jwt",
                    "-audience",
                    SPIFFE_AUDIENCE,
                    "-socketPath",
                    str(socket),
                    "-output",
                    "json",
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
            )
            if completed.returncode == 0:
                document = json.loads(completed.stdout)
                token = _pick_svid(document, int(time.time()), repository)
                if token:
                    return token
                last_error = "SPIRE response did not contain the publisher JWT-SVID for this repository"
            else:
                last_error = "SPIRE Workload API refused the publisher identity"
        except (json.JSONDecodeError, OSError, subprocess.SubprocessError, ValueError) as error:
            last_error = str(error)
        if time.monotonic() + 2 >= deadline:
            raise RuntimeError(last_error)
        time.sleep(2)


def _request_registry_token(
    request: urllib.request.Request, mint_url: str, timeout_seconds: float
) -> Any:
    """GET the issuer, retrying only transient failures (see MINT_ATTEMPTS)."""
    for attempt in range(1, MINT_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                if response.geturl().split("?", 1)[0] != mint_url:
                    raise RuntimeError("registry token issuer redirected away from its fixed authority")
                raw = response.read()
            break
        except urllib.error.HTTPError as error:
            # Do not include the response body: an unexpected proxy must not
            # reflect any credential material into CI logs.
            if error.code < 500:
                raise RuntimeError(
                    f"registry token issuer rejected the publisher: HTTP {error.code}"
                ) from error
            failure: Exception = error
            reason = f"HTTP {error.code}"
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            failure = error
            reason = type(getattr(error, "reason", error)).__name__
        if attempt == MINT_ATTEMPTS:
            raise RuntimeError(
                f"registry token issuer unavailable after {MINT_ATTEMPTS} attempts ({reason})"
            ) from failure
        delay = MINT_BACKOFF_SECONDS[min(attempt, len(MINT_BACKOFF_SECONDS)) - 1]
        print(
            f"registry token issuer attempt {attempt}/{MINT_ATTEMPTS} failed ({reason}); "
            f"retrying in {delay:.0f}s",
            file=sys.stderr,
        )
        time.sleep(delay)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("registry token issuer returned invalid JSON") from error


def _mint_registry_token(svid: str, account: str, repository: str, host: str, timeout_seconds: float) -> str:
    mint_url = f"https://{host}/token" if host != DEFAULT_REGISTRY_HOST else DEFAULT_MINT_URL
    query = urllib.parse.urlencode(
        [
            ("service", REGISTRY_TOKEN_SERVICE),
            ("account", account),
            ("scope", f"repository:{repository}:pull,push"),
            ("ttl", str(MAX_REGISTRY_TOKEN_LIFETIME_SECONDS)),
        ]
    )
    request = urllib.request.Request(
        f"{mint_url}?{query}",
        headers={"Accept": "application/json", "Authorization": f"Bearer {svid}"},
        method="GET",
    )
    body = _request_registry_token(request, mint_url, timeout_seconds)
    if not isinstance(body, dict):
        raise RuntimeError("registry token issuer returned a non-object payload")
    token = body.get("token") or body.get("access_token")
    if not isinstance(token, str):
        raise RuntimeError("registry token issuer response omitted the token")
    _validate_registry_token(token, int(time.time()), repository)
    return token


def _fetch_github_oidc_token(timeout_seconds: float) -> str:
    """The job's GitHub Actions OIDC token for the registry audience.

    Needs `permissions: id-token: write` on the job (GitHub then sets
    ACTIONS_ID_TOKEN_REQUEST_URL / _TOKEN). The request token never leaves
    this process and is never printed."""
    url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
    request_token = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
    if not url or not request_token:
        raise RuntimeError(
            "GitHub OIDC is unavailable: the job needs `permissions: id-token: write`"
        )
    separator = "&" if "?" in url else "?"
    request = urllib.request.Request(
        f"{url}{separator}{urllib.parse.urlencode({'audience': GITHUB_OIDC_AUDIENCE})}",
        headers={"Accept": "application/json", "Authorization": f"Bearer {request_token}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"GitHub OIDC token request refused: HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise RuntimeError("GitHub OIDC token request failed") from error
    token = body.get("value") if isinstance(body, dict) else None
    if not isinstance(token, str):
        raise RuntimeError("GitHub OIDC response omitted the token")
    _validate_github_oidc(token, int(time.time()))
    return token


def _validate_github_oidc(token: str, now: int) -> None:
    claims = _jwt_claims(token)
    if claims.get("iss") != GITHUB_OIDC_ISSUER:
        raise ValueError("GitHub OIDC token has the wrong issuer")
    if GITHUB_OIDC_AUDIENCE not in _audiences(claims):
        raise ValueError("GitHub OIDC token audience does not name the registry")
    expires_at = claims.get("exp")
    if not isinstance(expires_at, int) or expires_at <= now:
        raise ValueError("GitHub OIDC token is expired")
    if expires_at - now > MAX_GITHUB_OIDC_LIFETIME_SECONDS:
        raise ValueError("GitHub OIDC token lifetime exceeds the budget")


def _write_auth(directory: Path, token: str, host: str) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(stat.S_IRWXU)
    docker_path = directory / "config.json"
    skopeo_path = directory / "skopeo-auth.json"
    docker_path.write_text(
        json.dumps({"auths": {host: {"identitytoken": token}}}) + "\n",
        encoding="utf-8",
    )
    encoded = base64.b64encode(f"oauth2accesstoken:{token}".encode()).decode("ascii")
    skopeo_path.write_text(
        json.dumps({"auths": {host: {"auth": encoded}}}) + "\n",
        encoding="utf-8",
    )
    docker_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    skopeo_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return docker_path, skopeo_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True, help="registry host + repository, e.g. registry.sylphx.com/library/name")
    parser.add_argument("--output-dir", type=Path, help="directory for docker/skopeo auth files (not used with --probe)")
    parser.add_argument("--run-id", default="probe", help="GitHub run id used as the token account subject")
    parser.add_argument("--probe", action="store_true", help="mint, validate, print, and discard the credential")
    parser.add_argument(
        "--identity",
        choices=("spiffe", "github-oidc"),
        default="spiffe",
        help="publisher identity: the job's GitHub OIDC token (trusted publishing) or a SPIFFE JWT-SVID",
    )
    parser.add_argument("--agent-bin", type=Path, default=SPIRE_AGENT_IMAGE_BIN)
    parser.add_argument("--socket", type=Path, default=SPIFFE_WORKLOAD_API_SOCKET)
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--identity-wait-seconds", type=float, default=DEFAULT_IDENTITY_WAIT_SECONDS
    )
    args = parser.parse_args()
    if not args.probe and args.output_dir is None:
        parser.error("--output-dir is required unless --probe is set")
    if not args.probe and not str(args.run_id).isdecimal():
        parser.error("GitHub run id must be numeric")
    host, repository = split_image_reference(args.image)
    started = time.monotonic()
    mode = args.identity
    with tempfile.TemporaryDirectory(prefix="image-lane-spiffe-") as temporary:
        if mode == "github-oidc":
            credential = _fetch_github_oidc_token(args.timeout_seconds)
        else:
            agent = _copy_agent(args.agent_bin, Path(temporary))
            credential = _fetch_svid(agent, args.socket, args.identity_wait_seconds, repository)
        token = _mint_registry_token(
            credential, f"gha:{args.run_id}", repository, host, args.timeout_seconds
        )
        if args.probe:
            print(
                f"preflight_ok mode={mode} repository={repository} "
                f"seconds={time.monotonic() - started:.2f} ttl<={MAX_REGISTRY_TOKEN_LIFETIME_SECONDS}"
            )
            return 0
        docker_path, skopeo_path = _write_auth(args.output_dir, token, host)
    print(
        f"mint_ok mode={mode} repository={repository} "
        f"seconds={time.monotonic() - started:.2f} ttl<={MAX_REGISTRY_TOKEN_LIFETIME_SECONDS}"
    )
    print(f"docker_config={docker_path}")
    print(f"skopeo_auth_file={skopeo_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
