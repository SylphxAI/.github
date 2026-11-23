#!/usr/bin/env python3
"""The rust-sccache bucket step fails loudly with the object store's status
code, and the end-of-job report warns when cache writes fail or never happen."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import tempfile
import unittest

ACTION = pathlib.Path(__file__).resolve().parents[1] / ".github" / "actions" / "rust-sccache"

# Answers by method: "HEAD:<code>", "PUT:<code>[:<S3 error code>]", one per line in $FAKE_ANSWERS.
FAKE_CURL = r"""#!/usr/bin/env bash
out=""; method=GET; prev=""; url=""
for a in "$@"; do
  case "$prev" in -o) out="$a" ;; -X) method="$a" ;; esac
  [ "$a" = -I ] && method=HEAD
  prev="$a"; url="$a"
done
case "$url" in *"?lifecycle") method=LIFECYCLE ;; */.write-probe) method="OBJ$method" ;; esac
line="$(grep "^$method:" "$FAKE_ANSWERS" | head -n1)"
IFS=: read -r _ code s3 <<<"$line"
[ -n "$out" ] && { [ -n "${s3:-}" ] && printf '<Error><Code>%s</Code></Error>' "$s3" > "$out" || : > "$out"; }
printf '%s' "${code:-000}"
"""

FAKE_SCCACHE = r"""#!/usr/bin/env bash
case "$*" in
  *json*) printf '%s' "$JSON_STATS" ;;
  *) printf 'Cache hits                          %s\nCache misses                        %s\nCache hits rate                   0.00 %%\nCache write errors                  %s\n' "$HITS" "$MISSES" "$WRITE_ERRORS" ;;
esac
"""


def run(script: str, bin_script: str, bin_name: str, env: dict[str, str]) -> tuple[subprocess.CompletedProcess[str], str]:
    with tempfile.TemporaryDirectory() as tmp:
        fake = pathlib.Path(tmp) / bin_name
        fake.write_text(bin_script)
        fake.chmod(0o755)
        answers = pathlib.Path(tmp) / "answers"
        answers.write_text(env.pop("ANSWERS", ""))
        summary = pathlib.Path(tmp) / "summary"
        result = subprocess.run(
            ["bash", str(ACTION / script)],
            env={
                "PATH": f"{tmp}:{os.environ['PATH']}",
                "FAKE_ANSWERS": str(answers),
                "SCCACHE_ENDPOINT": "http://rgw.example:80/",
                "SCCACHE_BUCKET": "ci-sccache-org-a",
                "AWS_ACCESS_KEY_ID": "KEYID",
                "AWS_SECRET_ACCESS_KEY": "do-not-print",
                "GITHUB_STEP_SUMMARY": str(summary),
                **env,
            },
            capture_output=True,
            text=True,
        )
        return result, summary.read_text() if summary.exists() else ""


def bucket(answers: str) -> subprocess.CompletedProcess[str]:
    return run("ensure-bucket.sh", FAKE_CURL, "curl", {"ANSWERS": answers})[0]


def report(
    hits: int, misses: int, write_errors: int, writes: int, *,
    mode: str | None = "ReadWrite", **settings: str,
) -> tuple[subprocess.CompletedProcess[str], str]:
    stats = {"stats": {"cache_write_errors": write_errors, "cache_writes": writes}}
    if mode is not None:
        stats["effective_cache_mode"] = mode
    env = dict(HITS=str(hits), MISSES=str(misses), WRITE_ERRORS=str(write_errors),
               JSON_STATS=json.dumps(stats), **settings)
    return run("cache-report.sh", FAKE_SCCACHE, "sccache", env)


class EnsureBucket(unittest.TestCase):
    def test_existing_writable_bucket_is_left_alone(self) -> None:
        r = bucket("HEAD:200\nOBJPUT:200\nOBJDELETE:204\n")
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_existing_bucket_that_refuses_writes_warns_with_the_status(self) -> None:
        r = bucket("HEAD:200\nOBJPUT:403:AccessDenied\n")
        self.assertEqual(r.returncode, 0)
        self.assertIn("::warning::", r.stdout)
        self.assertIn("HTTP 403 AccessDenied", r.stdout)

    def test_missing_bucket_is_created(self) -> None:
        self.assertEqual(bucket("HEAD:404\nPUT:200\nLIFECYCLE:200\nOBJPUT:200\n").returncode, 0)

    def test_already_exists_is_not_a_failure(self) -> None:
        r = bucket("HEAD:403\nPUT:409:BucketAlreadyExists\nLIFECYCLE:200\n")
        self.assertEqual(r.returncode, 0)
        self.assertIn("409", r.stdout)

    def test_create_failure_fails_with_the_status_and_s3_code(self) -> None:
        r = bucket("HEAD:403\nPUT:403:AccessDenied\n")
        self.assertEqual(r.returncode, 1)
        self.assertIn("::error::", r.stdout)
        self.assertIn("HTTP 403 AccessDenied", r.stdout)

    def test_unreachable_endpoint_fails_with_000(self) -> None:
        r = bucket("")
        self.assertEqual(r.returncode, 1)
        self.assertIn("HTTP 000", r.stdout)

    def test_lifecycle_failure_warns(self) -> None:
        r = bucket("HEAD:404\nPUT:200\nLIFECYCLE:403:AccessDenied\n")
        self.assertEqual(r.returncode, 0)
        self.assertIn("::warning::", r.stdout)
        self.assertIn("HTTP 403 AccessDenied", r.stdout)

    def test_never_prints_the_secret(self) -> None:
        r = bucket("HEAD:403\nPUT:403:AccessDenied\n")
        self.assertNotIn("do-not-print", r.stdout + r.stderr)


class CacheReport(unittest.TestCase):
    def test_write_errors_warn_and_do_not_fail(self) -> None:
        r, _ = report(0, 592, 592, 0)
        self.assertEqual(r.returncode, 0)
        self.assertIn("::warning::sccache: 592 cache write(s) failed", r.stdout)

    def test_misses_without_writes_warn(self) -> None:
        r, _ = report(10, 5, 0, 0)
        self.assertIn("5 cache miss(es) but 0 writes", r.stdout)

    def test_healthy_cache_is_silent(self) -> None:
        self.assertNotIn("::warning::", report(10, 5, 0, 5)[0].stdout)

    def test_all_hits_is_silent(self) -> None:
        self.assertNotIn("::warning::", report(20, 0, 0, 0)[0].stdout)

    def test_confirmed_read_only_keeps_the_counters_without_warning(self) -> None:
        for backend, setting in (("buildcache", "SCCACHE_WEBDAV_RW_MODE"),
                                 ("static", "SCCACHE_S3_RW_MODE"),
                                 ("gha", "SCCACHE_GHA_RW_MODE")):
            with self.subTest(backend=backend):
                r, summary = report(76, 1344, 1344, 0, mode="ReadOnly",
                                    BACKEND=backend, **{setting: "READ_ONLY"})
                self.assertEqual(r.returncode, 0)
                self.assertNotIn("::warning::", r.stdout)
                self.assertIn("1344 refused write(s)", r.stdout)
                self.assertRegex(summary, r"Cache write errors +1344")
                self.assertRegex(summary, r"Cache writes +0")
                self.assertRegex(summary, r"Effective cache mode +ReadOnly")
                self.assertIn("This job does not warm the cache.", summary)

    def test_read_only_by_policy_without_writes_is_not_a_failure(self) -> None:
        r, _ = report(2, 5, 0, 0, mode="ReadOnly", BACKEND="buildcache",
                      SCCACHE_WEBDAV_RW_MODE="READ_ONLY")
        self.assertNotIn("::warning::", r.stdout)

    def test_write_probe_downgrade_warns_even_on_all_hits(self) -> None:
        r, _ = report(20, 0, 0, 0, mode="ReadOnly", BACKEND="buildcache",
                      SCCACHE_WEBDAV_RW_MODE="READ_WRITE")
        self.assertEqual(r.returncode, 0)
        self.assertIn("::warning::sccache: the server is read-only", r.stdout)

    def test_other_backend_read_only_does_not_hide_downgrade(self) -> None:
        r, _ = report(4, 1, 1, 0, mode="ReadOnly", BACKEND="gha",
                      SCCACHE_WEBDAV_RW_MODE="READ_ONLY")
        self.assertIn("::warning::sccache: the server is read-only", r.stdout)

    def test_unconfirmed_read_only_does_not_mask_write_counter(self) -> None:
        for mode in (None, "ReadWrite"):
            with self.subTest(mode=mode):
                r, _ = report(0, 5, 5, 0, mode=mode, BACKEND="buildcache",
                              SCCACHE_WEBDAV_RW_MODE="READ_ONLY")
                self.assertIn("::warning::sccache: 5 cache write(s) failed", r.stdout)


if __name__ == "__main__":
    unittest.main()
