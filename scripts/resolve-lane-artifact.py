#!/usr/bin/env python3
"""Resolve this run's transfer artifact for the image lane's publish leg.

The compile leg uploads attempt-scoped artifacts
(`image-lane-image-<sha>-<run_id>-<run_attempt>`) so a re-run can never merge
bytes from two attempts into one artifact. The publish leg must therefore not
recompute that name from `github.run_attempt`: a *partial* re-run ("Re-run
failed jobs") re-runs only the publish job, the compile leg does not run
again, and the only artifacts in the run still carry the earlier attempt
number — the recomputed `…-<attempt>` name can never match.

Live 2026-09-15 (hands run 34986604012, source 0c15bb56): attempt 1's publish
job failed and its log was already gone; attempt 2 (re-run of the failed
publish job only) failed closed with

    Unable to download artifact(s): Artifact not found for name:
    image-lane-image-0c15bb56…-34986604012-2

while the only artifact in the run was `…-34986604012-1`. The standard
recovery action on the release path was structurally impossible.

Resolution order (fail-closed; never mixes source shas, because the name
embeds the sha):

1. this attempt's artifact, exactly one, not expired;
2. otherwise the highest attempt number with a non-expired artifact, and that
   number must be unambiguous;
3. otherwise fail, naming the attempts that do exist.

`--download-to DIR` then fetches that artifact into DIR: the archive download
URL is re-resolved per attempt and the transfer resumes from the bytes already
on disk, because a single connection to the artifact blob edge can end short
and still report success (live 2026-09-27: a 2.1 GiB image transfer stopped
after 21 minutes, exit 0, and the push step found no index.json; the same
edge reset a 2.3 GiB download at 7.5 minutes with `curl: (56)` on 2026-09-28).
The archive is checked against the artifact's size and sha256 digest before a
single byte is extracted, and extraction refuses members that are not plain
relative paths.

The token needs `actions: read` on the run's repository (the publish job
already declares it). No third-party dependency; `urllib` only.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import shutil
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

MAX_PAGES = 10
PER_PAGE = 100

# Resume budget for `--download-to`: the blob edge cut a 2.3 GiB transfer after
# 7.5 minutes and a 2.1 GiB one after 21 minutes within one day, so a reset is
# expected rather than exceptional. Each attempt re-resolves a fresh signed URL
# and costs only the bytes since the last one.
DOWNLOAD_ATTEMPTS = 8
DOWNLOAD_SLEEP_SECONDS = 5.0
# A read that produces nothing for this long is treated as a dead connection
# and resumed, instead of waiting out the runner's 30-minute job timeout.
DOWNLOAD_READ_TIMEOUT_SECONDS = 120.0


class ResolutionError(SystemExit):
    """Fail-closed resolution error (message is the operator-facing reason)."""


def candidate_attempt(name: str, prefix: str) -> int | None:
    """The attempt number encoded in `<prefix>-<attempt>`, or `None`."""
    if not name.startswith(prefix + "-"):
        return None
    suffix = name[len(prefix) + 1 :]
    return int(suffix) if suffix.isdigit() else None


def select_artifact_record(
    artifacts: list[dict], prefix: str, attempt: int
) -> tuple[dict, list[str]]:
    """Pick the transfer artifact (its full record) for this run/attempt.

    Returns `(record, notes)`. Raises `ResolutionError` when no candidate can
    be used or the choice would be ambiguous.
    """
    notes: list[str] = []
    candidates: list[tuple[int, dict, bool]] = []
    for artifact in artifacts:
        name = str(artifact.get("name", ""))
        number = candidate_attempt(name, prefix)
        if number is None:
            continue
        candidates.append((number, artifact, bool(artifact.get("expired"))))

    live = [candidate for candidate in candidates if not candidate[2]]
    expired = [candidate for candidate in candidates if candidate[2]]
    seen = sorted({candidate[0] for candidate in candidates})

    current = [candidate for candidate in live if candidate[0] == attempt]
    if len(current) > 1:  # pragma: no cover - names are unique per run
        raise ResolutionError(
            f"FATAL: {len(current)} artifacts named {prefix}-{attempt}; refusing to guess"
        )
    if current:
        return current[0][1], notes

    if not live:
        detail = f"attempts seen: {seen}" if seen else "no matching artifact at all"
        raise ResolutionError(
            f"FATAL: no artifact matching {prefix}-* that is not expired ({detail})"
        )

    highest = max(candidate[0] for candidate in live)
    top = [candidate for candidate in live if candidate[0] == highest]
    if len(top) != 1:  # pragma: no cover - names are unique per run
        raise ResolutionError(
            f"FATAL: {len(top)} artifacts remain for attempt {highest} under {prefix}; refusing to guess"
        )
    record = top[0][1]
    name = str(record.get("name", ""))
    if any(candidate[0] == attempt for candidate in expired):
        notes.append(
            f"this attempt's artifact {prefix}-{attempt} is expired; "
            f"using {name} (same run, same source sha)"
        )
    else:
        notes.append(
            f"this attempt produced no transfer artifact (partial re-run); "
            f"using {name} from attempt {highest} (same run, same source sha)"
        )
    return record, notes


def select_artifact(
    artifacts: list[dict], prefix: str, attempt: int
) -> tuple[str, list[str]]:
    """Pick the transfer artifact name (a thin wrapper over the record form)."""
    record, notes = select_artifact_record(artifacts, prefix, attempt)
    return str(record.get("name", "")), notes


def _next_link(header: str) -> str | None:
    for part in header.split(","):
        segments = part.split(";")
        if len(segments) >= 2 and 'rel="next"' in segments[1]:
            return segments[0].strip().strip("<>")
    return None


def list_artifacts(repo: str, run_id: str, token: str, api_url: str) -> list[dict]:
    url = (
        f"{api_url.rstrip('/')}/repos/{repo}/actions/runs/{run_id}/artifacts"
        f"?per_page={PER_PAGE}"
    )
    artifacts: list[dict] = []
    for _ in range(MAX_PAGES):
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
            link = response.headers.get("Link", "")
        page = payload.get("artifacts") or []
        artifacts.extend(page)
        next_url = _next_link(link)
        if not next_url or len(page) < PER_PAGE:
            return artifacts
        url = next_url
    raise ResolutionError(
        f"FATAL: artifact listing for run {run_id} did not end within {MAX_PAGES} pages"
    )


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Stop at the redirect so the caller's token never reaches the blob host."""

    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


def signed_archive_url(url: str, token: str, *, timeout: float) -> str:
    """The artifact's signed blob URL, read off the redirect without following it."""
    opener = urllib.request.build_opener(NoRedirect)
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        location = error.headers.get("Location")
        if error.code in (301, 302, 303, 307, 308) and location:
            return str(location)
        raise ResolutionError(
            f"FATAL: artifact download URL refused with HTTP {error.code}"
        ) from error
    raise ResolutionError("FATAL: artifact download URL answered without a redirect")


def range_header(have: int) -> dict[str, str]:
    """The resume request's Range header, or nothing for a fresh start."""
    return {"Range": f"bytes={have}-"} if have > 0 else {}


def sha256_of(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(chunk)
    return "sha256:" + hasher.hexdigest()


def fetch_archive(
    url: str,
    token: str,
    archive: Path,
    expected_size: int,
    *,
    attempts: int,
    sleep_seconds: float,
    timeout: float,
) -> int:
    """Download `url` into `archive`, resuming across resets.

    Returns the byte count on disk, which equals `expected_size`. Each attempt
    resolves a fresh signed URL, so an expired or reset connection costs only
    the bytes written since the last attempt.
    """
    have = 0
    for attempt in range(1, attempts + 1):
        signed = signed_archive_url(url, token, timeout=timeout)
        have = archive.stat().st_size if archive.exists() else 0
        if have > expected_size:
            archive.unlink()
            have = 0
        request = urllib.request.Request(signed, headers=range_header(have))
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                # A host that ignores the Range header sends the whole body;
                # restart rather than mix two copies in one file.
                mode = "wb" if have and response.status != 206 else "ab"
                with archive.open(mode) as handle:
                    while True:
                        chunk = response.read(1 << 20)
                        if not chunk:
                            break
                        handle.write(chunk)
        except (urllib.error.URLError, http.client.IncompleteRead, ConnectionError, TimeoutError) as error:
            print(
                f"artifact download: attempt {attempt} ended early: {error}",
                file=sys.stderr,
            )
        have = archive.stat().st_size if archive.exists() else 0
        if have == expected_size:
            return have
        if attempt < attempts:
            print(
                f"artifact download at {have} of {expected_size} bytes; "
                f"resuming attempt {attempt + 1}",
                file=sys.stderr,
            )
            time.sleep(sleep_seconds)
    raise ResolutionError(
        f"FATAL: artifact download incomplete after {attempts} attempts: "
        f"{have} of {expected_size} bytes"
    )


def extract_archive(archive: Path, destination: Path) -> int:
    """Extract `archive` into a fresh `destination`, refusing unsafe members."""
    shutil.rmtree(destination, ignore_errors=True)
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        for info in members:
            name = PurePosixPath(info.filename)
            if name.is_absolute() or ".." in name.parts:
                raise ResolutionError(
                    f"FATAL: artifact member {info.filename!r} is not a plain relative path"
                )
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ResolutionError(
                    f"FATAL: artifact member {info.filename!r} is a symbolic link"
                )
        bundle.extractall(destination)
    return len(members)


def download_artifact(
    record: dict,
    destination: Path,
    token: str,
    *,
    attempts: int,
    sleep_seconds: float,
    timeout: float,
) -> None:
    """Fetch the resolved artifact into `destination`, verified end to end."""
    name = str(record.get("name", ""))
    url = record.get("archive_download_url")
    expected_size = record.get("size_in_bytes")
    digest = str(record.get("digest") or "")
    if not url or not isinstance(expected_size, int) or expected_size <= 0:
        raise ResolutionError(
            f"FATAL: artifact {name} has no download metadata (url/size_in_bytes)"
        )
    with tempfile.TemporaryDirectory(prefix="image-lane-transfer-") as temporary:
        archive = Path(temporary) / "artifact.zip"
        have = fetch_archive(
            str(url),
            token,
            archive,
            expected_size,
            attempts=attempts,
            sleep_seconds=sleep_seconds,
            timeout=timeout,
        )
        actual = sha256_of(archive)
        if digest and actual != digest:
            raise ResolutionError(
                f"FATAL: artifact {name} hashes to {actual}, not {digest}"
            )
        members = extract_archive(archive, destination)
    print(f"artifact_downloaded bytes={have} members={members} digest={actual}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/name of the running repository")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--attempt", required=True, type=int)
    parser.add_argument(
        "--prefix",
        required=True,
        help="artifact prefix without the attempt: e.g. image-lane-image-<sha>-<run_id>",
    )
    parser.add_argument(
        "--github-output",
        default=os.environ.get("GITHUB_OUTPUT", ""),
        help="file to append `name=<artifact>` to (defaults to $GITHUB_OUTPUT)",
    )
    parser.add_argument(
        "--download-to",
        default="",
        help=(
            "download the resolved artifact into this directory (emptied first) "
            "with a resumable transfer, then extract it"
        ),
    )
    parser.add_argument(
        "--download-attempts", type=int, default=DOWNLOAD_ATTEMPTS
    )
    parser.add_argument(
        "--download-sleep-seconds", type=float, default=DOWNLOAD_SLEEP_SECONDS
    )
    parser.add_argument(
        "--download-read-timeout-seconds",
        type=float,
        default=DOWNLOAD_READ_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--api-url",
        default=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", ""),
    )
    arguments = parser.parse_args(argv)

    if not arguments.token:
        raise ResolutionError("FATAL: no GH_TOKEN/GITHUB_TOKEN for the artifact listing")

    if arguments.download_attempts < 1:
        raise ResolutionError("FATAL: --download-attempts must be at least 1")
    if arguments.download_read_timeout_seconds <= 0:
        raise ResolutionError("FATAL: --download-read-timeout-seconds must be positive")

    artifacts = list_artifacts(
        arguments.repo, arguments.run_id, arguments.token, arguments.api_url
    )
    record, notes = select_artifact_record(
        artifacts, arguments.prefix, arguments.attempt
    )
    name = str(record.get("name", ""))
    for note in notes:
        print(f"resolve-lane-artifact: {note}", file=sys.stderr)
    if arguments.github_output:
        with open(arguments.github_output, "a", encoding="utf-8") as handle:
            handle.write(f"name={name}\n")
    print(name)
    if arguments.download_to:
        download_artifact(
            record,
            Path(arguments.download_to),
            arguments.token,
            attempts=arguments.download_attempts,
            sleep_seconds=arguments.download_sleep_seconds,
            timeout=arguments.download_read_timeout_seconds,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
