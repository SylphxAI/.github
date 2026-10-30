import contextlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / ".github/actions/device-capture"
SPEC = importlib.util.spec_from_file_location("device_capture", ACTION / "capture.py")
capture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(capture)
PARENT = "orgs/org_example/projects/prj_example/envs/env_example"
IDENTITY = {"org":"orgs/org_example", "project":"orgs/org_example/projects/prj_example", "env":PARENT,
            "scopes":["sandboxes:read","sandboxes:write","sandboxes:exec"], "resources":[],
            "principal":"prn_example"}


class FakeReceipt:
    def __init__(self, identity=None):
        self.data = {}
        self.calls = []
        self.identity = IDENTITY if identity is None else identity

    def save(self):
        pass

    def request(self, url, bearer=None, body=None, label=None, allow_propagation=False, timeout=60):
        self.calls.append((url,bearer,body,label))
        if label:
            return {"value":"test-oidc"}
        if url.endswith("github/token"):
            return {"api_key":"test-capture-key","active_after_ms":3000}
        return self.identity


class DeviceCaptureTests(unittest.TestCase):
    def test_exact_environment_and_scope_ceiling(self):
        capture.check_identity(IDENTITY,PARENT)
        for changes in ({"org":"orgs/org_other"},{"env":""},{"project":""},
                        {"scopes":["sandboxes:write"]},{"scopes":[*IDENTITY["scopes"],"access:admin"]},
                        {"resources":["one-resource"]}):
            with self.subTest(changes=changes), self.assertRaises(capture.CaptureError):
                capture.check_identity({**IDENTITY,**changes},PARENT)

    def exercise(self, identity=None, cli="sylphx 1.2.3", flags="--scenario JSON --size LABEL", exit_code=0):
        receipt = FakeReceipt(identity)
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app,scenario = root/"app.apk",root/"scenario.json"
            app.write_bytes(b"fixture")
            scenario.write_text("{}")
            options = ("capture-test",PARENT,str(app),str(scenario),"phone","1.2.3",str(root/"output"))
            def run(args,**kwargs):
                calls.append((args,kwargs))
                if args[-1]=="--version":
                    return subprocess.CompletedProcess(args,0,cli,"")
                if args[-1]=="--help":
                    return subprocess.CompletedProcess(args,0,flags,"")
                self.assertNotIn("test-capture-key"," ".join(args))
                self.assertEqual(kwargs["env"]["SYLPHX_API_KEY"],"test-capture-key")
                self.assertNotIn("ACTIONS_ID_TOKEN_REQUEST_TOKEN",kwargs["env"])
                self.assertEqual(kwargs["env"]["SYLPHX_BASE_URL"],capture.API)
                self.assertEqual(args[args.index("--env")+1],PARENT)
                self.assertEqual(args[args.index("--size")+1],"phone")
                return subprocess.CompletedProcess(args,exit_code)
            with patch.object(capture.subprocess,"run",side_effect=run), \
                 patch.object(capture.time,"sleep") as sleep, \
                 patch.dict(os.environ,{"ACTIONS_ID_TOKEN_REQUEST_URL":"https://oidc.example/token?x=1",
                                        "ACTIONS_ID_TOKEN_REQUEST_TOKEN":"test-request"}), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                capture.execute(receipt,options)
        return receipt,calls,sleep,output.getvalue()

    def test_positive_published_cli_exact_identity_before_single_lease_driver(self):
        receipt,calls,sleep,output = self.exercise()
        self.assertEqual(len(receipt.calls),3)
        self.assertEqual(receipt.calls[1][2],{"name":"capture-test","token":"test-oidc"})
        self.assertIn("audience=sylphx-access",receipt.calls[0][0])
        sleep.assert_called_once_with(3)
        self.assertEqual(len(calls),3)
        self.assertEqual(receipt.data["capture_exit"],0)
        visible = "\n".join(line for line in output.splitlines() if not line.startswith("::add-mask::"))
        self.assertNotIn("test-capture-key",visible)
        self.assertNotIn("test-oidc",visible)

    def test_wrong_pin_missing_capability_or_wrong_identity_never_runs_capture(self):
        for changes in ({"cli":"sylphx 1.2.2"},{"flags":"--app PATH"},{"identity":{**IDENTITY,"env":""}}):
            with self.subTest(changes=changes), self.assertRaises(capture.CaptureError):
                self.exercise(**changes)

    def test_cli_failure_is_not_reported_as_capture_success(self):
        with self.assertRaises(capture.CaptureError):
            self.exercise(exit_code=2)

    def test_only_same_token_propagation_reads_are_retried_with_a_finite_bound(self):
        receipt = FakeReceipt()
        now = [0.0]
        def sleep_for(seconds):
            now[0] += seconds
        with patch.object(receipt,"request",side_effect=[{"code":"key_not_yet_propagated"},IDENTITY]) as read, \
             patch.object(capture.time,"sleep",side_effect=sleep_for) as sleep, \
             patch.object(capture.time,"monotonic",side_effect=lambda:now[0]):
            self.assertEqual(capture.propagated_identity(receipt,"same-issued-key"),IDENTITY)
            self.assertEqual(read.call_count,2)
            sleep.assert_called_once_with(2)
            for args in read.call_args_list:
                self.assertEqual(args.args,(capture.API+"/v1/whoami","same-issued-key"))
                self.assertTrue(args.kwargs["allow_propagation"])
                self.assertGreater(args.kwargs["timeout"],0)
                self.assertLessEqual(args.kwargs["timeout"],30)
        now[0] = 0.0
        with patch.object(receipt,"request",return_value={"code":"key_not_yet_propagated"}) as read, \
             patch.object(capture.time,"sleep",side_effect=sleep_for) as sleep, \
             patch.object(capture.time,"monotonic",side_effect=lambda:now[0]), self.assertRaises(capture.CaptureError):
            capture.propagated_identity(receipt,"same-issued-key")
        self.assertEqual(now[0],30)
        self.assertEqual(read.call_count,15)
        self.assertEqual(sleep.call_count,15)
        with patch.object(receipt,"request",side_effect=capture.CaptureError("other refusal")) as read, \
             patch.object(capture.time,"sleep") as sleep, self.assertRaises(capture.CaptureError):
            capture.propagated_identity(receipt,"same-issued-key")
        self.assertEqual(read.call_count,1)
        sleep.assert_not_called()

    def test_request_allowlist_is_exact_http_status_code_and_read_endpoint(self):
        cases = ((401,"key_not_yet_propagated",capture.API+"/v1/whoami",None,True),
                 (401,"UNAUTHENTICATED",capture.API+"/v1/whoami",None,False),
                 (403,"key_not_yet_propagated",capture.API+"/v1/whoami",None,False),
                 (401,"key_not_yet_propagated",capture.API+"/v1/access/github/token",{"name":"test"},False))
        for status,code,endpoint,body,allowed in cases:
            with self.subTest(status=status,code=code,endpoint=endpoint), tempfile.TemporaryDirectory() as directory:
                receipt = capture.Receipt(Path(directory)/"receipt.json")
                payload = ('{"code":"'+code+'"}').encode()
                error = urllib.error.HTTPError(endpoint,status,"refused",{},io.BytesIO(payload))
                with patch.object(capture.urllib.request,"build_opener") as opener, contextlib.redirect_stdout(io.StringIO()):
                    opener.return_value.open.side_effect = error
                    if allowed:
                        self.assertEqual(receipt.request(endpoint,"same-key",body,allow_propagation=True,timeout=7),{"code":code})
                    else:
                        with self.assertRaises(capture.CaptureError):
                            receipt.request(endpoint,"same-key",body,allow_propagation=True,timeout=7)
                    self.assertEqual(opener.return_value.open.call_count,1)
                    self.assertEqual(opener.return_value.open.call_args.kwargs["timeout"],7)
                self.assertEqual(receipt.data["requests"][0]["status"],status)
                self.assertNotIn("same-key",receipt.path.read_text())

    def test_elapsed_request_cost_consumes_the_propagation_deadline(self):
        now = [0.0]
        receipt = FakeReceipt()
        def slow_read(*args,**kwargs):
            self.assertEqual(kwargs["timeout"],30)
            now[0] += 31
            return IDENTITY
        with patch.object(receipt,"request",side_effect=slow_read) as read, \
             patch.object(capture.time,"monotonic",side_effect=lambda:now[0]), \
             patch.object(capture.time,"sleep") as sleep, self.assertRaises(capture.CaptureError):
            capture.propagated_identity(receipt,"same-key")
        self.assertEqual(read.call_count,1)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
