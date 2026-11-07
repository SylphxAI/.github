"""The shared transport's real curl boundary and deterministic retry decisions."""
from contextlib import redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

SHARED = Path(__file__).resolve().parents[1] / ".github/actions/_shared"
sys.path.insert(0, str(SHARED))
import curl_retry as net

SECRET = "synthetic-bearer-secret"
QUERY = "signed-query-sentinel"
BODY = b"private-body-sentinel"


class Endpoint:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                owner.requests.append((self.path, self.headers.get("Authorization")))
                reply = owner.replies.pop(0) if len(owner.replies) > 1 else owner.replies[0]
                if reply == "timeout":
                    time.sleep(0.3)
                    reply = 200
                self.send_response(403 if reply == "403-timeout" else reply)
                self.send_header("Content-Length", str(len(BODY)))
                self.end_headers()
                if reply == "403-timeout":
                    time.sleep(0.3)
                try:
                    self.wfile.write(BODY)
                except OSError:
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/artifact?signature={QUERY}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class CurlRetryTest(unittest.TestCase):
    def fetch(self, replies, policy=None):
        endpoint = Endpoint(replies)
        self.addCleanup(endpoint.close)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "body"
        log = io.StringIO()
        start = time.monotonic()
        with redirect_stderr(log):
            result = net.request(endpoint.url, path, label="test-read",
                                 headers=("Authorization: Bearer " + SECRET,),
                                 policy=policy or net.Policy(base_delay=0))
        return result, log.getvalue(), endpoint, path, time.monotonic() - start

    def assert_safe_diagnostic(self, log):
        for secret in (SECRET, QUERY, BODY.decode(), "Authorization", "http://", "https://"):
            self.assertNotIn(secret, log)
        for field in net.FIELDS:
            self.assertIn(field + "=", log)

    def test_two_real_timeouts_then_success_keep_private_config_and_replace_partial_output(self):
        result, log, endpoint, path, _ = self.fetch(
            ["timeout", "timeout", 200], net.Policy(budget=2, attempt_time=0.06, base_delay=0.005))
        self.assertEqual(result, net.Result("200", 0))
        self.assertEqual(len(endpoint.requests), 3)
        self.assertEqual(path.read_bytes(), BODY)
        self.assertTrue(all(auth == "Bearer " + SECRET and QUERY in path for path, auth in endpoint.requests))
        self.assertEqual(log.count("curl_exit=28"), 2)
        self.assertIn("remote_ip=127.0.0.1", log)
        self.assert_safe_diagnostic(log)

    def test_permanent_auth_and_missing_entries_are_not_retried(self):
        for code in (401, 403, 404):
            with self.subTest(code=code):
                result, log, endpoint, _, _ = self.fetch([code, 200])
                self.assertEqual(result, net.Result(str(code), 0))
                self.assertEqual(len(endpoint.requests), 1)
                self.assertIn("stop=permanent", log)
                self.assert_safe_diagnostic(log)

    def test_permanent_status_wins_when_the_refusal_body_times_out(self):
        result, log, endpoint, _, _ = self.fetch(
            ["403-timeout", 200], net.Policy(budget=2, attempt_time=0.06, base_delay=0))
        self.assertEqual(result, net.Result("403", 28))
        self.assertEqual(len(endpoint.requests), 1)
        self.assertIn("stop=permanent", log)
        self.assert_safe_diagnostic(log)

    def test_rate_limits_and_server_errors_retry(self):
        result, log, endpoint, path, _ = self.fetch([429, 500, 503, 599, 200])
        self.assertEqual(result, net.Result("200", 0))
        self.assertEqual(len(endpoint.requests), 5)
        self.assertEqual(path.read_bytes(), BODY)
        self.assertEqual(log.count("stop=retry"), 4)
        self.assert_safe_diagnostic(log)

    def test_deadline_exhaustion_bounds_the_final_inflight_request_and_logs_timings(self):
        result, log, endpoint, _, elapsed = self.fetch(
            ["timeout"], net.Policy(budget=0.12, attempt_time=10, base_delay=0))
        self.assertEqual(result.returncode, 28)
        self.assertEqual(len(endpoint.requests), 1)
        self.assertLess(elapsed, 1.0)
        self.assertIn("stop=budget-exhausted", log)
        self.assert_safe_diagnostic(log)

    def test_only_allowlisted_curl_exits_retry_and_config_is_not_in_argv(self):
        for code in (1, 5, 6, 7, 22, 28, 35, 52, 55, 56, 60, 63):
            with self.subTest(code=code):
                completed = subprocess.CompletedProcess([], code, b"0.001\t0.002\t0.000\t0.003\t0.004\t127.0.0.1\t000")
                with patch.object(net.subprocess, "run", return_value=completed) as run, redirect_stderr(io.StringIO()):
                    net.request("https://example.test/?signature=" + QUERY, "/unused", label="test-read",
                                headers=("Authorization: Bearer " + SECRET,), policy=net.Policy(base_delay=0))
                self.assertEqual(run.call_count, 5 if code in net.TRANSIENT else 1)
                for call in run.call_args_list:
                    args = call.args[0]
                    self.assertEqual(args[:2], ["curl", "-q"])
                    self.assertNotIn(SECRET, " ".join(args))
                    self.assertNotIn(QUERY, " ".join(args))
                    self.assertIn(SECRET.encode(), call.kwargs["input"])
                    self.assertIn(QUERY.encode(), call.kwargs["input"])

    def test_exponential_backoff_has_bounded_jitter_and_five_attempts(self):
        now = [0.0]
        sleeps = []

        def sleep(delay):
            sleeps.append(delay)
            now[0] += delay

        failure = subprocess.CompletedProcess([], 6, b"")
        with patch.object(net.time, "monotonic", side_effect=lambda: now[0]), \
                patch.object(net.time, "sleep", side_effect=sleep), \
                patch.object(net.random, "uniform", return_value=1.125), \
                patch.object(net.subprocess, "run", return_value=failure) as run, redirect_stderr(io.StringIO()) as log:
            result = net.run(["-o", "/unused"], label="test-read")
        self.assertEqual(result.returncode, 6)
        self.assertEqual(run.call_count, 5)
        self.assertEqual(sleeps, [2.25, 4.5, 9, 18])
        self.assertIn("stop=attempts-exhausted", log.getvalue())
        for call in run.call_args_list:
            self.assertLessEqual(call.kwargs["timeout"], 120)

    def test_backoff_never_sleeps_past_deadline(self):
        failure = subprocess.CompletedProcess([], 7, b"")
        with patch.object(net.subprocess, "run", return_value=failure) as run, \
                patch.object(net.time, "sleep") as sleep, redirect_stderr(io.StringIO()) as log:
            net.run(["-o", "/unused"], label="test-read", policy=net.Policy(budget=0.1))
        self.assertEqual(run.call_count, 1)
        sleep.assert_not_called()
        self.assertIn("stop=budget-exhausted", log.getvalue())

    def test_malformed_metrics_cannot_become_a_log_channel(self):
        bad = (SECRET + "\t" + QUERY + "\t0.0\t0.0\t0.0\t" + BODY.decode() + "\t403").encode()
        with patch.object(net.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, bad)), \
                redirect_stderr(io.StringIO()) as log:
            result = net.run(["-o", "/unused"], label="test-read")
        self.assertEqual(result.code, "403")
        self.assert_safe_diagnostic(log.getvalue())


if __name__ == "__main__":
    unittest.main()
