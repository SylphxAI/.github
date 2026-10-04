#!/usr/bin/env python3
"""Tests for the pages-publish-gate action: the decision table, how the served commit is read, and the
action and its starter template (no network: http_get is replaced)."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
ACTION = ROOT / ".github" / "actions" / "pages-publish-gate"
SPEC = importlib.util.spec_from_file_location("pages_publish_gate", ACTION / "pages_publish_gate.py")
ppg = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ppg)

SERVED = "a" * 40
RUN = "b" * 40


def compare_returning(status):
    def compare(served, run_sha):
        if isinstance(status, Exception):
            raise status
        return status

    return compare


class Decide(unittest.TestCase):
    def check(self, served, status, publish, word):
        got, reason = ppg.decide(RUN, served, compare_returning(status))
        self.assertEqual(got, publish, reason)
        self.assertIn(word, reason)

    def test_ahead_publishes(self):
        self.check(SERVED, "ahead", True, "ahead")

    def test_identical_behind_diverged_skip(self):
        for status in ("identical", "behind", "diverged"):
            with self.subTest(status):
                self.check(SERVED, status, False, status)

    def test_nothing_served_publishes_without_comparing(self):
        self.check("", ppg.ReadError("must not be called"), True, "nothing served")

    def test_the_served_commit_is_the_run_commit(self):
        self.check(RUN, ppg.ReadError("must not be called"), False, "identical")
        self.check(RUN[:12], ppg.ReadError("must not be called"), False, "identical")

    def test_compare_error_skips_with_a_warning(self):
        self.check(SERVED, ppg.ReadError("HTTP 500"), False, "WARN")

    def test_unknown_compare_status_skips_with_a_warning(self):
        self.check(SERVED, "", False, "WARN")
        self.check(SERVED, "weird", False, "WARN")


class ServedFromBody(unittest.TestCase):
    def test_json_key(self):
        self.assertEqual(ppg.served_from_body('{"name": "x", "git_sha": "%s"}' % SERVED, "git_sha"), SERVED)

    def test_other_key(self):
        self.assertEqual(ppg.served_from_body('{"commit": "%s"}' % SERVED, "commit"), SERVED)

    def test_plain_text_and_case(self):
        self.assertEqual(ppg.served_from_body(SERVED.upper() + "\n", "git_sha"), SERVED)

    def test_short_sha_is_kept(self):
        self.assertEqual(ppg.served_from_body('{"git_sha": "b9be7fb72e2b"}', "git_sha"), "b9be7fb72e2b")

    def test_not_a_sha_is_nothing_served(self):
        for body in ("", "Not found", "<html>", '{"name": "x"}', '{"git_sha": "unknown"}', "{not json", "[1]", "artifact of run 7"):
            with self.subTest(body):
                self.assertEqual(ppg.served_from_body(body, "git_sha"), "")


class Main(unittest.TestCase):
    def run_main(self, argv, http, compare=None):
        """Runs main() with http_get replaced; returns (exit code, stdout, GITHUB_OUTPUT text)."""
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "out"
            out.write_text("")
            old_http, old_env = ppg.http_get, os.environ.get("GITHUB_OUTPUT")
            ppg.http_get = http
            os.environ["GITHUB_OUTPUT"] = str(out)
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                    code = ppg.main(argv, compare=compare)
            finally:
                ppg.http_get = old_http
                if old_env is None:
                    os.environ.pop("GITHUB_OUTPUT", None)
                else:
                    os.environ["GITHUB_OUTPUT"] = old_env
            return code, buf.getvalue(), out.read_text()

    URL = "https://site.example/product.meta.json"

    def argv(self, **extra):
        base = ["--run-sha", RUN, "--repository", "o/r", "--served-sha-url", self.URL]
        for k, v in extra.items():
            base += [f"--{k.replace('_', '-')}", v]
        return base

    def test_ahead_of_the_served_json_publishes_and_writes_outputs(self):
        http = lambda url, headers=None: (200, '{"git_sha": "%s"}' % SERVED)
        code, stdout, out = self.run_main(self.argv(), http, compare_returning("ahead"))
        self.assertEqual(code, 0)
        self.assertIn("publish=true\n", out)
        self.assertIn(f"served-sha={SERVED}\n", out)
        self.assertNotIn("::warning::", stdout)

    def test_behind_skips_without_a_warning(self):
        http = lambda url, headers=None: (200, SERVED)
        code, stdout, out = self.run_main(self.argv(), http, compare_returning("behind"))
        self.assertEqual(code, 0)
        self.assertIn("publish=false\n", out)
        self.assertNotIn("::warning::", stdout)

    def test_404_means_nothing_served(self):
        http = lambda url, headers=None: (404, "")
        code, _, out = self.run_main(self.argv(), http, compare_returning(ppg.ReadError("unused")))
        self.assertEqual(code, 0)
        self.assertIn("publish=true\n", out)
        self.assertIn("reason=nothing served\n", out)

    def test_a_site_that_answers_200_without_a_sha_means_nothing_served(self):
        http = lambda url, headers=None: (200, "<html>app</html>")
        _, _, out = self.run_main(self.argv(), http, compare_returning(ppg.ReadError("unused")))
        self.assertIn("publish=true\n", out)

    def test_a_server_error_or_a_network_failure_skips_with_a_warning(self):
        def down(url, headers=None):
            raise ppg.ReadError("connection refused")

        for http in (lambda url, headers=None: (503, ""), down):
            with self.subTest():
                code, stdout, out = self.run_main(self.argv(), http, compare_returning("ahead"))
                self.assertEqual(code, 0)
                self.assertIn("publish=false\n", out)
                self.assertIn("::warning::", stdout)

    def test_compare_error_skips_with_a_warning(self):
        http = lambda url, headers=None: (200, SERVED)
        code, stdout, out = self.run_main(self.argv(), http, compare_returning(ppg.ReadError("HTTP 404")))
        self.assertEqual(code, 0)
        self.assertIn("publish=false\n", out)
        self.assertIn("::warning::", stdout)

    def test_served_sha_input_wins_over_the_url(self):
        def no_http(url, headers=None):
            raise AssertionError("the url must not be read")

        _, _, out = self.run_main(self.argv(served_sha=SERVED), no_http, compare_returning("ahead"))
        self.assertIn("publish=true\n", out)

    def test_compare_call_uses_the_token_and_the_two_commits(self):
        calls = []

        def http(url, headers=None):
            calls.append((url, headers))
            return 200, '{"status": "ahead"}'

        old = ppg.http_get
        ppg.http_get = http
        try:
            self.assertEqual(ppg.github_compare("o/r", "tok")(SERVED, RUN), "ahead")
            ppg.http_get = lambda url, headers=None: (404, "")
            with self.assertRaises(ppg.ReadError):
                ppg.github_compare("o/r", "tok")(SERVED, RUN)
        finally:
            ppg.http_get = old
        self.assertIn(f"/repos/o/r/compare/{SERVED}...{RUN}", calls[0][0])
        self.assertEqual(calls[0][1]["Authorization"], "Bearer tok")

    def test_a_malformed_run_sha_is_refused(self):
        code, _, _ = self.run_main(["--run-sha", "nothex", "--repository", "o/r"], lambda *a, **k: (200, ""))
        self.assertEqual(code, 2)


class ActionAndTemplate(unittest.TestCase):
    def test_action_refuses_a_github_hosted_runner_and_keeps_the_token_out_of_arguments(self):
        text = (ACTION / "action.yml").read_text()
        self.assertIn("github-hosted", text)
        self.assertIn("GATE_TOKEN: ${{ inputs.token }}", text)
        self.assertNotIn("--token", text)

    def test_template_deploys_from_workflow_run_without_cancelling(self):
        text = (ROOT / "workflow-templates" / "pages-publish.yml").read_text()
        self.assertIn("workflow_run:", text)
        self.assertIn("cancel-in-progress: false", text)
        self.assertNotIn("cancel-in-progress: true", text)
        self.assertIn("uses: SylphxAI/.github/.github/actions/pages-publish-gate@", text)
        self.assertNotIn("ubuntu-latest", text)


if __name__ == "__main__":
    unittest.main()
