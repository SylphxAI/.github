#!/usr/bin/env python3
"""Retrieve an exact run-store entry using the caller's own identities."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


def metadata(path):
    try:
        response = subprocess.run(["gh", "api", path], check=True, capture_output=True,
                                  text=True, timeout=60)
        return json.loads(response.stdout)
    except (subprocess.SubprocessError, ValueError):
        raise ValueError("GitHub metadata is unavailable to the current gh identity") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", help="owner/repository")
    parser.add_argument("run_id")
    parser.add_argument("name")
    parser.add_argument("destination", type=Path)
    parser.add_argument("--api-key-stdin", action="store_true",
                        help="read the caller's org-wide build:read Access key from stdin; otherwise use SYLPHX_API_KEY")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository):
        parser.error("repository must be owner/repository")
    if not re.fullmatch(r"[0-9]{1,20}", args.run_id):
        parser.error("run id must be numeric")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", args.name):
        parser.error("name must be 1-100 of [A-Za-z0-9._-]")
    key = sys.stdin.read(8193).strip() if args.api_key_stdin else os.environ.get("SYLPHX_API_KEY", "")
    if not re.fullmatch(r"[A-Za-z0-9._~+/=-]{1,8192}", key):
        parser.error("provide the caller's Access key via --api-key-stdin or SYLPHX_API_KEY")
    repo = metadata(f"repos/{args.repository}")
    run = metadata(f"repos/{args.repository}/actions/runs/{args.run_id}")
    repo_id = repo.get("id")
    if (not isinstance(repo_id, int) or repo_id <= 0 or str(run.get("id")) != args.run_id
            or run.get("repository", {}).get("id") != repo_id):
        raise ValueError("the authenticated repository/run identities do not match")
    # Reuse the same bounded multipart and archive validation as workflow get.
    # Manual mode suppresses GitHub mask directives, which a terminal would
    # otherwise print as plaintext credentials.
    with tempfile.TemporaryDirectory(prefix="run-store-get-") as work:
        env = dict(os.environ, MODE="get", NAME=args.name, STORE_PATH=str(args.destination),
                   STORE_RUN_ID=args.run_id, REPO_ID=str(repo_id), REQUIRED="true",
                   RETENTION_DAYS="", RUN_STORE_MANUAL="true", SYLPHX_API_KEY=key,
                   GITHUB_OUTPUT=str(Path(work) / "outputs"), RUNNER_TEMP=work)
        return subprocess.run(["bash", str(Path(__file__).with_name("run-store.sh"))], env=env).returncode


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError) as error:
        print(f"run-store get: {error}", file=sys.stderr)
        sys.exit(1)
