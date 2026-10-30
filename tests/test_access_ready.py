import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("access_ready", ROOT/".github/actions/_shared/access_ready.py")
ready = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ready)
API = "https://api.example"


class AccessReadyTests(unittest.TestCase):
    def test_advertised_delay_then_same_key_propagation_reads_with_walltime_bound(self):
        now = [0.0]
        def sleep(seconds):
            now[0] += seconds
        with patch.object(ready.time,"sleep",side_effect=sleep) as pause, \
             patch.object(ready.time,"monotonic",side_effect=lambda:now[0]):
            read = unittest.mock.Mock(side_effect=[{"code":"key_not_yet_propagated"},{"principal":"prn_test"}])
            self.assertEqual(ready.wait_for_identity(read,"same-key",3000,API),{"principal":"prn_test"})
        self.assertEqual([call.args[0] for call in pause.call_args_list],[3,2])
        for call in read.call_args_list:
            self.assertEqual(call.args,(API+"/v1/whoami","same-key"))
            self.assertTrue(call.kwargs["allow_propagation"])
        self.assertEqual([call.kwargs["timeout"] for call in read.call_args_list],[30,28])
        now[0] = 0
        with patch.object(ready.time,"sleep",side_effect=sleep), \
             patch.object(ready.time,"monotonic",side_effect=lambda:now[0]), self.assertRaises(ready.ReadinessError):
            ready.wait_for_identity(lambda *args,**kw:{"code":"key_not_yet_propagated"},"same-key",0,API)
        self.assertEqual(now[0],30)

    def test_curl_read_uses_total_timeout_and_stdin_credential_without_logging(self):
        cases = ((200,{"principal":"prn_test"},True),
                 (401,{"code":"key_not_yet_propagated"},True),
                 (401,{"code":"UNAUTHENTICATED"},False),
                 (403,{"code":"key_not_yet_propagated"},False))
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
                  "key":{"name":"orgs/org_test/api_keys/test", "status":{"principal":"orgs/org_test/principals/prn_test"}}}
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
