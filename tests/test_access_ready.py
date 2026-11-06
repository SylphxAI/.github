import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("access_ready", ROOT/".github/actions/_shared/access_ready.py")
ready = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ready)
API = "https://api.example"


class AccessReadyTests(unittest.TestCase):
    def test_refusal_reports_status_public_body_and_redacts_credentials(self):
        token = "same-issued-key"
        body = {"code": "PERMISSION_DENIED", "detail": "refused Bearer " + token,
                "request_id": "req_test", "api_key": "must-not-be-logged",
                "token": "also-private", "other": "not-a-public-error-field"}
        result = subprocess.CompletedProcess([], 0, json.dumps(body) + "\n403", "")
        with patch.object(ready.subprocess, "run", return_value=result), \
                self.assertRaises(ready.ReadinessError) as caught:
            ready.read_identity(API + "/v1/whoami", token, allow_propagation=True, timeout=7)
        message = str(caught.exception)
        for expected in ("HTTP 403", "PERMISSION_DENIED", "req_test", "[REDACTED]"):
            self.assertIn(expected, message)
        for private in (token, "must-not-be-logged", "also-private", "not-a-public-error-field"):
            self.assertNotIn(private, message)

    def test_excerpt_is_bounded_single_line_and_redacts_before_truncating(self):
        token = "same-issued-key"
        body = {"error": {"code": "UNAUTHENTICATED", "message": "\\\"\n" + token + " " * 600 + token}}
        excerpt = ready.refusal_excerpt(body, token)
        self.assertLessEqual(len(excerpt), 512)
        self.assertNotIn("\n", excerpt)
        self.assertNotIn(token, excerpt)
        self.assertIn("UNAUTHENTICATED", excerpt)
        self.assertIn("[REDACTED]", excerpt)
        other_token = "gh" + "p_" + "x" * 32
        self.assertNotIn(other_token, ready.refusal_excerpt({"detail": other_token}, token))

    def test_cli_reports_safe_reason_and_keeps_failure_exit(self):
        for error, expected in ((ready.ReadinessError("Access readiness refused with HTTP 401; response={\"code\": \"UNAUTHENTICATED\"}"), "HTTP 401"),
                                (ready.ReadinessError("ready identity differs from the issued key"), "ready identity differs"),
                                (subprocess.TimeoutExpired(["curl", "private-input"], 1), "identity read timed out"),
                                (ValueError("private-input"), "malformed readiness input or identity")):
            with self.subTest(error=type(error).__name__), patch.object(ready, "main", side_effect=error), \
                    contextlib.redirect_stderr(io.StringIO()) as output:
                self.assertEqual(ready.cli(), 1)
            self.assertIn(expected, output.getvalue())
            self.assertIn("no Broker request was sent", output.getvalue())
            self.assertNotIn("private-input", output.getvalue())

    def test_shipped_entrypoint_prints_refusal_without_credential_echo(self):
        issued = {"api_key": "same-issued-key", "active_after_ms": 0,
                  "key": {"name": "orgs/org_test/api_keys/test",
                          "status": {"principal": "orgs/org_test/principals/prn_test"}}}
        with tempfile.TemporaryDirectory() as directory:
            curl = Path(directory) / "curl"
            curl.write_text("#!" + sys.executable + "\nimport json, sys\n"
                            "sys.stdin.read()\n"
                            "print(json.dumps({'code':'PERMISSION_DENIED', 'detail':'refused same-issued-key', 'request_id':'req_test'}))\n"
                            "sys.stdout.write('403')\n")
            curl.chmod(0o700)
            result = subprocess.run([sys.executable, str(ROOT / ".github/actions/_shared/access_ready.py"),
                                     "--api-url", API], input=json.dumps(issued), capture_output=True, text=True,
                                    env={**os.environ, "PATH": directory + os.pathsep + os.environ.get("PATH", "")}, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        for expected in ("HTTP 403", "PERMISSION_DENIED", "req_test", "no Broker request was sent"):
            self.assertIn(expected, result.stderr)
        self.assertNotIn("same-issued-key", result.stderr)

    def test_issued_subject_accepts_org_project_and_environment_key_parents(self):
        for parent in ("orgs/org_test", "orgs/org_test/projects/prj_test",
                       "orgs/org_test/projects/prj_test/envs/env_test"):
            with self.subTest(parent=parent):
                issued = {"key": {"name": parent + "/api_keys/key_test",
                                  "status": {"principal": "orgs/org_test/principals/prn_test"}}}
                self.assertEqual(ready.issued_subject(issued), ("orgs/org_test", "orgs/org_test/principals/prn_test"))

    def test_issued_subject_refuses_malformed_or_foreign_names_before_identity_read(self):
        for name, principal in (("orgs/org_test/projects/prj_test/envs/env_test/api_keys/key_test", "orgs/other/principals/prn_test"),
                                ("orgs/org_test/projects/prj_test/envs/env_test/api_keys/key_test", "prn_test"),
                                ("orgs/org_test/extra/api_keys/key_test", "orgs/org_test/principals/prn_test"),
                                ("orgs/org_test/projects/prj_test/envs/env_test/api_keys/key_test/trailing", "orgs/org_test/principals/prn_test"),
                                ("orgs/org_test/projects/prj_test/envs/env_test", "orgs/org_test/principals/prn_test"),
                                ("orgs/org_test/api_keys/key_test", None)):
            issued = {"api_key": "same-issued-key", "active_after_ms": 0,
                      "key": {"name": name, "status": {"principal": principal}}}
            with self.subTest(name=name, principal=principal), patch("sys.argv", ["access_ready.py", "--api-url", API]), \
                    patch("sys.stdin", io.StringIO(json.dumps(issued))), patch.object(ready, "wait_for_identity") as read:
                with self.assertRaises(ready.ReadinessError):
                    ready.main()
                read.assert_not_called()

    def test_advertised_delay_then_same_key_propagation_reads_with_walltime_bound(self):
        now = [0.0]
        def sleep(seconds):
            now[0] += seconds
        with patch.object(ready.time,"sleep",side_effect=sleep) as pause, \
             patch.object(ready.time,"monotonic",side_effect=lambda:now[0]):
            read = unittest.mock.Mock(side_effect=[{"code":"key_not_yet_propagated"},{"code":"NOT_YET_PROPAGATED"},{"principal":"prn_test"}])
            self.assertEqual(ready.wait_for_identity(read,"same-key",3000,API),{"principal":"prn_test"})
        self.assertEqual([call.args[0] for call in pause.call_args_list],[3,2,2])
        for call in read.call_args_list:
            self.assertEqual(call.args,(API+"/v1/whoami","same-key"))
            self.assertTrue(call.kwargs["allow_propagation"])
        self.assertEqual([call.kwargs["timeout"] for call in read.call_args_list],[180,178,176])
        now[0] = 0
        with patch.object(ready.time,"sleep",side_effect=sleep), \
             patch.object(ready.time,"monotonic",side_effect=lambda:now[0]), self.assertRaises(ready.ReadinessError):
            ready.wait_for_identity(lambda *args,**kw:{"code":"key_not_yet_propagated"},"same-key",0,API)
        self.assertEqual(now[0],180)

    def test_curl_read_uses_total_timeout_and_stdin_credential_without_logging(self):
        cases = ((200,{"principal":"prn_test"},True),
                 (401,{"code":"key_not_yet_propagated"},True),
                 (401,{"code":"NOT_YET_PROPAGATED"},True),
                 (401,{"code":"UNAUTHENTICATED"},False),
                 (403,{"code":"key_not_yet_propagated"},False),
                 (403,{"code":"NOT_YET_PROPAGATED"},False))
        for status,body,accepted in cases:
            with self.subTest(status=status,body=body):
                def curl(args,**kwargs):
                    self.assertNotIn("same-key"," ".join(args))
                    self.assertIn("same-key",kwargs["input"])
                    self.assertEqual(args[args.index("--max-time")+1],"7")
                    self.assertEqual(kwargs["timeout"],7)
                    return subprocess.CompletedProcess(args,0,json.dumps(body)+"\n"+str(status),"")
                with patch.object(ready.subprocess,"run",side_effect=curl):
                    if accepted:
                        self.assertEqual(ready.read_identity(API+"/v1/whoami","same-key",allow_propagation=True,timeout=7),body)
                    else:
                        with self.assertRaises(ready.ReadinessError):
                            ready.read_identity(API+"/v1/whoami","same-key",allow_propagation=True,timeout=7)

    def test_invalid_delay_and_network_refusal_do_not_retry(self):
        read = unittest.mock.Mock()
        with self.assertRaises(ready.ReadinessError):
            ready.wait_for_identity(read,"same-key",None,API)
        read.assert_not_called()
        read.side_effect = ready.ReadinessError("network")
        with patch.object(ready.time,"sleep"), self.assertRaises(ready.ReadinessError):
            ready.wait_for_identity(read,"same-key",0,API)
        self.assertEqual(read.call_count,1)

    def test_cli_wrapper_checks_issued_subject_and_never_outputs_the_key(self):
        issued = {"api_key":"same-issued-key","active_after_ms":0,
                  "key":{"name":"orgs/org_test/projects/prj_test/envs/env_test/api_keys/test", "status":{"principal":"orgs/org_test/principals/prn_test"}}}
        for identity, accepted in (({"org":"orgs/org_test","principal":"prn_test"},True),
                                   ({"org":"orgs/other","principal":"prn_test"},False),
                                   ({"org":"orgs/org_test","principal":"prn_other"},False)):
            with self.subTest(identity=identity), patch("sys.argv",["access_ready.py","--api-url",API]), \
                 patch("sys.stdin",io.StringIO(json.dumps(issued))), \
                 patch.object(ready,"wait_for_identity",return_value=identity), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                if accepted:
                    ready.main()
                    self.assertNotIn("same-issued-key",output.getvalue())
                    self.assertTrue(json.loads(output.getvalue())["ready"])
                else:
                    with self.assertRaises(ready.ReadinessError):
                        ready.main()


if __name__ == "__main__":
    unittest.main()
