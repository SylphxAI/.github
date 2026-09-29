#!/usr/bin/env python3
"""The rust-sccache backend order: the runner's per-org credential, then the
organization's own cache user, then the GitHub Actions cache; never the
platform's ci-sccache bucket."""

from __future__ import annotations

import os
import pathlib
import subprocess
import unittest

BACKEND = pathlib.Path(__file__).resolve().parents[1] / ".github" / "actions" / "rust-sccache" / "backend.sh"

ORG = {
    "SYLPHX_SCCACHE_ACCESS_KEY_ID": "ORGKEY",
    "SYLPHX_SCCACHE_SECRET_ACCESS_KEY": "org-secret",
    "SYLPHX_SCCACHE_SESSION_TOKEN": "org-token",
    "SYLPHX_SCCACHE_BUCKET": "ci-org-cache",
    "SYLPHX_SCCACHE_ENDPOINT": "http://rgw.example:80",
    "SYLPHX_SCCACHE_KEY_PREFIX": "sccache/1001",
}
STATIC = {"S3_ACCESS_KEY": "STATICKEY", "S3_SECRET_KEY": "static-secret", "S3_ENDPOINT": "http://rgw.example:80"}
FIELDS = [
    "backend",
    "SCCACHE_BUCKET",
    "SCCACHE_S3_KEY_PREFIX",
    "AWS_ACCESS_KEY_ID",
    "AWS_SESSION_TOKEN",
    "SCCACHE_GHA_ENABLED",
]


def choose(env: dict[str, str]) -> dict[str, str]:
    base = {
        "PATH": os.environ["PATH"],
        "KEY_PREFIX": "org-a/repo-a",
        "GITHUB_REPOSITORY_OWNER": "Org-A",
        # A job's own AWS_* must not leak into the org backend's server.
        "AWS_SESSION_TOKEN": "",
    }
    script = f'. "{BACKEND}"\n' + "".join(f'printf "%s=%s\\n" {f} "${{{f}:-}}"\n' for f in FIELDS)
    out = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        env={**base, **env},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


class RustSccacheBackend(unittest.TestCase):
    def test_runner_credential_comes_first(self) -> None:
        got = choose({**ORG, **STATIC})
        self.assertEqual(got["backend"], "org")
        self.assertEqual(got["SCCACHE_BUCKET"], "ci-org-cache")
        self.assertEqual(got["SCCACHE_S3_KEY_PREFIX"], "sccache/1001/org-a/repo-a")
        self.assertEqual(got["AWS_ACCESS_KEY_ID"], "ORGKEY")
        self.assertEqual(got["AWS_SESSION_TOKEN"], "org-token")

    def test_partial_runner_credential_is_not_used(self) -> None:
        partial = {k: v for k, v in ORG.items() if k != "SYLPHX_SCCACHE_SESSION_TOKEN"}
        got = choose({**partial, **STATIC})
        self.assertEqual(got["backend"], "static")

    def test_org_cache_user_second(self) -> None:
        got = choose(STATIC)
        self.assertEqual(got["backend"], "static")
        self.assertEqual(got["SCCACHE_BUCKET"], "ci-sccache-org-a")
        self.assertEqual(got["AWS_ACCESS_KEY_ID"], "STATICKEY")
        self.assertEqual(got["AWS_SESSION_TOKEN"], "")

    def test_actions_cache_last(self) -> None:
        got = choose({})
        self.assertEqual(got["backend"], "gha")
        self.assertEqual(got["SCCACHE_GHA_ENABLED"], "true")
        self.assertEqual(got["AWS_ACCESS_KEY_ID"], "")

    def test_actions_cache_can_be_left_to_the_caller(self) -> None:
        self.assertEqual(choose({"ACTIONS_CACHE": "false"})["backend"], "none")

    def test_never_the_platform_ci_sccache_bucket(self) -> None:
        got = choose({**STATIC, "S3_BUCKET": "ci-sccache"})
        self.assertNotEqual(got["SCCACHE_BUCKET"], "ci-sccache")
        self.assertEqual(got["backend"], "gha")


if __name__ == "__main__":
    unittest.main()
