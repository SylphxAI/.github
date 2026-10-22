#!/usr/bin/env python3
"""Non-regress gate for a web preview deploy (the pages-publish-gate action).

Publish a commit only when it is ahead of the commit the preview serves.

  served sha missing, 404/410, or not a sha   publish  (nothing to protect)
  ahead                                       publish
  identical, behind, diverged                 skip
  served read or compare failed               skip + ::warning:: (the next run corrects it)

The token is read from GATE_TOKEN, never from an argument. The script exits 0 for every decision and
2 only for a malformed run sha, so a gate that cannot decide never turns a green deploy red.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

SHA = re.compile(r"^[0-9a-f]{7,40}$")
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
API = "https://api.github.com"
TIMEOUT = 30
# Cloudflare Pages (*.pages.dev) answers urllib's default "Python-urllib/3.x" with HTTP 403.
USER_AGENT = "sylphx-pages-publish-gate/1 (+https://github.com/SylphxAI/.github)"


class ReadError(Exception):
    """A request failed in a way that says nothing about what is served."""


def http_get(url: str, headers: dict[str, str] | None = None) -> tuple[int, str]:
    """(status, body). An HTTP error status is returned; a network failure raises ReadError."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310 - https URLs from the caller
            return resp.status, resp.read(1 << 20).decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        return err.code, ""
    except (urllib.error.URLError, OSError, ValueError) as err:
        raise ReadError(str(err)) from err


def served_from_body(body: str, key: str) -> str:
    """The served commit named by a JSON object's key or by a plain-text body; "" when there is none."""
    text = body.strip()
    value = text
    if text.startswith("{"):
        try:
            data = json.loads(text)
            value = str(data.get(key, "")) if isinstance(data, dict) else ""
        except ValueError:
            value = ""
    value = value.strip().lower()
    return value if SHA.match(value) else ""


def decide(run_sha: str, served: str, compare) -> tuple[bool, str]:
    """(publish, reason). `compare(served, run_sha)` returns the compare API status or raises ReadError."""
    if not served:
        return True, "nothing served"
    if run_sha.startswith(served):
        return False, f"identical: the preview already serves {served}"
    try:
        status = compare(served, run_sha)
    except ReadError as err:
        return False, f"WARN could not compare {run_sha} with {served} ({err}); not publishing blind"
    if status == "ahead":
        return True, f"ahead of {served}"
    if status in ("identical", "behind", "diverged"):
        return False, f"{status}: the preview serves {served}"
    return False, f"WARN could not compare {run_sha} with {served} (status '{status}'); not publishing blind"


def github_compare(repository: str, token: str):
    def compare(served: str, run_sha: str) -> str:
        code, body = http_get(
            f"{API}/repos/{repository}/compare/{served}...{run_sha}?per_page=1",
            {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        )
        if code != 200:
            raise ReadError(f"compare answered HTTP {code}")
        try:
            return str(json.loads(body).get("status", ""))
        except (ValueError, AttributeError) as err:
            raise ReadError("compare answered a body that is not JSON") from err

    return compare


def read_served(url: str, key: str) -> tuple[str, str]:
    """(served sha, problem). A 404/410 or a body with no sha is "nothing served"; anything else is a problem."""
    try:
        code, body = http_get(url)
    except ReadError as err:
        return "", f"cannot read {url} ({err})"
    if code in (404, 410):
        return "", ""
    if code != 200:
        return "", f"cannot read {url} (HTTP {code})"
    return served_from_body(body, key), ""


def emit(publish: bool, reason: str, served: str) -> None:
    warn = reason.startswith("WARN ")
    reason = reason.removeprefix("WARN ")
    if warn:
        print(f"::warning::preview publish skipped: {reason}")
    else:
        print(f"publish={str(publish).lower()}: {reason}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"publish={str(publish).lower()}\nreason={reason}\nserved-sha={served}\n")


def main(argv: list[str] | None = None, compare=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-sha", required=True)
    ap.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    ap.add_argument("--served-sha", default="")
    ap.add_argument("--served-sha-url", default="")
    ap.add_argument("--served-sha-key", default="git_sha")
    args = ap.parse_args(argv)

    run_sha = args.run_sha.strip().lower()
    if not FULL_SHA.match(run_sha):
        print(f"::error::run-sha must be a 40-hex commit, got '{args.run_sha[:60]}'", file=sys.stderr)
        return 2

    served = args.served_sha.strip().lower()
    if served and not SHA.match(served):
        served = ""
    problem = ""
    if not served and args.served_sha_url:
        served, problem = read_served(args.served_sha_url, args.served_sha_key or "git_sha")
    if problem:
        emit(False, f"WARN {problem}; not publishing blind", "")
        return 0

    if compare is None:
        if not args.repository:
            print("::error::repository is required", file=sys.stderr)
            return 2
        compare = github_compare(args.repository, os.environ.get("GATE_TOKEN", ""))
    publish, reason = decide(run_sha, served, compare)
    emit(publish, reason, served)
    return 0


if __name__ == "__main__":
    sys.exit(main())
