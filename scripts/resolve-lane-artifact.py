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

The token needs `actions: read` on the run's repository (the publish job
already declares it). No third-party dependency; `urllib` only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

MAX_PAGES = 10
PER_PAGE = 100


class ResolutionError(SystemExit):
    """Fail-closed resolution error (message is the operator-facing reason)."""


def candidate_attempt(name: str, prefix: str) -> int | None:
    """The attempt number encoded in `<prefix>-<attempt>`, or `None`."""
    if not name.startswith(prefix + "-"):
        return None
    suffix = name[len(prefix) + 1 :]
    return int(suffix) if suffix.isdigit() else None


def select_artifact(
    artifacts: list[dict], prefix: str, attempt: int
) -> tuple[str, list[str]]:
    """Pick the transfer artifact name for this run/attempt.

    Returns `(name, notes)`. Raises `ResolutionError` when no candidate can be
    used or the choice would be ambiguous.
    """
    notes: list[str] = []
    candidates: list[tuple[int, str, bool]] = []
    for artifact in artifacts:
        name = str(artifact.get("name", ""))
        number = candidate_attempt(name, prefix)
        if number is None:
            continue
        candidates.append((number, name, bool(artifact.get("expired"))))

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
    name = top[0][1]
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
    return name, notes


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

    artifacts = list_artifacts(
        arguments.repo, arguments.run_id, arguments.token, arguments.api_url
    )
    name, notes = select_artifact(artifacts, arguments.prefix, arguments.attempt)
    for note in notes:
        print(f"resolve-lane-artifact: {note}", file=sys.stderr)
    if arguments.github_output:
        with open(arguments.github_output, "a", encoding="utf-8") as handle:
            handle.write(f"name={name}\n")
    print(name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
