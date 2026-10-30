"""No live credential or platform write is needed for these boundary checks."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / ".github/actions/source-read-probe"
SPEC = importlib.util.spec_from_file_location("source_read_probe", ACTION / "probe.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)
PIN = "a" * 40
POLICY = "orgs/org_test/trust_policies/source-read"
TARGET = {"repository": "example/source", "commit": PIN, "trust_policy": POLICY}


class FakeReceipts:
    def __init__(self, overrides=None):
        self.data = {"sources": [], "requests": []}
        self.calls = []
        self.overrides = overrides or {}

    def save(self):
        pass

    def request(self, url, token=None, body=None, label=None, expected=200, allow_propagation=False, timeout=90):
        self.calls.append((url, token, body, expected))
        self.data["requests"].append({"endpoint": url, "status": expected})
        if label == "GitHub Actions OIDC":
            return {"value": "test-oidc"}
        if url.endswith("/access/github/token"):
            return {"api_key": "test-access", "key": {"name": "orgs/org_test/projects/prj_test/envs/env_test/api_keys/key1", "status": {"principal": "orgs/org_test/principals/prn_test"}},
                    "active_after_ms": 3000, **self.overrides.get("access", {})}
        if url.endswith("/v1/whoami"):
            return {"org": "orgs/org_test", "principal": "prn_test", **self.overrides.get("identity", {})}
        if expected == 403:
            return {"code": "PERMISSION_DENIED"}
        if expected == 400:
            return {"code": "INVALID_FIELD", **self.overrides.get("negative", {})}
        if url.endswith(":exchangeToken"):
            return {"token": "test-provider", "repositories": ["example/source"], "permissions": {"contents": "read"},
                    "token_exchange": POLICY + "/token_exchanges/tkx_test", "issuer": "github-apps/123",
                    **self.overrides.get("broker", {})}
        return {"sha": PIN, **self.overrides.get("commit", {})}


class SourceReadProbeTests(unittest.TestCase):
    def test_bounded_exact_inputs_and_modes(self):
        self.assertEqual(probe.targets_from(json.dumps([TARGET]), "read"), [TARGET])
        for targets, mode in (([], "read"), ([TARGET] * 5, "read"), ([TARGET] * 2, "read"),
                              ([{**TARGET, "commit": "main"}], "read"),
                              ([{**TARGET, "repository": "../source"}], "read"),
                              ([{**TARGET, "token": "no"}], "read"), ([], "build")):
            with self.subTest(targets=targets, mode=mode), self.assertRaises(probe.ProbeError):
                probe.targets_from(json.dumps(targets), mode)
        with self.assertRaises(probe.ProbeError):
            probe.check_ci_name("../../receipt")

    def run_probe(self, receipts, mode="read", denied=""):
        env = {"ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.example/token?audience=old&x=1",
               "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "test-request"}
        with patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()) as output, \
                patch.object(probe.time, "sleep") as sleep, patch.object(probe, "fetch_exact", return_value=PIN) as fetch:
            probe.run(receipts, "source-read", mode, [TARGET], denied)
        return output.getvalue(), sleep, fetch

    def test_bootstrap_returns_only_stable_principal_without_broker_or_fetch(self):
        receipts = FakeReceipts()
        output, sleep, fetch = self.run_probe(receipts, "bootstrap")
        self.assertEqual(receipts.data["principal"], "orgs/org_test/principals/prn_test")
        self.assertEqual(len(receipts.calls), 2)
        self.assertIn("audience=sylphx-access", receipts.calls[0][0])
        self.assertNotIn("audience=old", receipts.calls[0][0])
        sleep.assert_not_called()
        fetch.assert_not_called()
        # Mask commands are the only allowed appearances of fake credentials.
        visible = "\n".join(line for line in output.splitlines() if not line.startswith("::add-mask::"))
        self.assertNotIn("test-access", visible)
        self.assertNotIn("test-oidc", visible)

    def test_read_waits_then_denies_write_and_proves_get_and_fetch(self):
        receipts = FakeReceipts()
        output, sleep, fetch = self.run_probe(receipts, denied="orgs/org_test/trust_policies/foreign-subject")
        sleep.assert_called_once_with(3)
        self.assertEqual([call[3] for call in receipts.calls], [200, 200, 200, 403, 400, 200, 200])
        self.assertTrue(receipts.calls[2][0].endswith("/v1/whoami"))
        self.assertEqual(receipts.calls[4][2]["permissions"], {"contents": "write"})
        self.assertEqual(receipts.calls[5][2], {"repositories": ["example/source"], "permissions": {"contents": "read"}})
        fetch.assert_called_once_with("example/source", PIN, "test-provider")
        self.assertEqual(receipts.data["sources"][0]["fetch_head"], PIN)
        self.assertFalse(receipts.data["sources"][0]["checked_out"])
        self.assertNotIn("test-provider", "\n".join(line for line in output.splitlines() if not line.startswith("::add-mask::")))

    def test_missing_principal_propagation_write_refusal_or_exact_commit_fails(self):
        for overrides in ({"access": {"key": {}}},
                          {"access": {"key": {"name": "orgs/org_test/api_keys/key1", "status": {"principal": "prn_test"}}}},
                          {"access": {"key": {"name": "orgs/org_test/api_keys/key1", "status": {"principal": "orgs/other/principals/prn_test"}}}},
                          {"access": {"active_after_ms": -1}},
                          {"identity": {"org": "orgs/other"}}, {"identity": {"principal": "prn_other"}},
                          {"negative": {"code": "WRONG"}}, {"negative": {"token": "unexpected"}},
                          {"broker": {"repositories": ["other/repo"]}},
                          {"broker": {"permissions": {"contents": "write"}}},
                          {"commit": {"sha": "b" * 40}}):
            with self.subTest(overrides=overrides), self.assertRaises(probe.ProbeError):
                self.run_probe(FakeReceipts(overrides))

    def test_foreign_org_negative_policy_is_not_called(self):
        receipts = FakeReceipts()
        with self.assertRaises(probe.ProbeError):
            self.run_probe(receipts, denied="orgs/org_other/trust_policies/source-read")
        self.assertEqual(len(receipts.calls), 3)

    def test_real_git_fetch_is_depth_one_with_no_checkout_or_saved_credential(self):
        real_run = subprocess.run
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "source"
            source.mkdir()
            env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
            def local_git(*args):
                return real_run(["git", "-C", str(source), *args], env=env, capture_output=True, check=True, text=True).stdout.strip()
            local_git("init", "-q", "--template=")
            (source / "source.txt").write_text("first")
            local_git("add", ".")
            local_git("-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-qm", "first")
            local_git("-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "--allow-empty", "-qm", "second")
            pin = local_git("rev-parse", "HEAD")
            commands = []
            def invoke(command, **kwargs):
                commands.append(command)
                self.assertNotIn("fake-token", " ".join(command))
                self.assertEqual(kwargs["env"]["SYLPHX_GIT_TOKEN"], "fake-token")
                self.assertNotIn("GIT_TRACE", kwargs["env"])
                if "fetch" in command:
                    command = ["file://" + str(source) if arg == "https://github.com/example/source.git" else arg for arg in command]
                    command[3:3] = ["-c", "protocol.file.allow=always"]
                result = real_run(command, **kwargs)
                if "fetch" in command:
                    target = Path(command[2])
                    self.assertTrue((target / ".git/shallow").is_file())
                    self.assertFalse((target / "source.txt").exists())
                return result
            with patch.object(probe.subprocess, "run", side_effect=invoke), patch.dict(os.environ, {"GIT_TRACE": "1"}):
                self.assertEqual(probe.fetch_exact("example/source", pin, "fake-token"), pin)
            self.assertFalse(any("checkout" in command for command in commands))

    def test_http_denial_records_status_raw_nonjson_and_never_retries(self):
        with tempfile.TemporaryDirectory() as root:
            receipt = probe.Receipts(Path(root) / "receipt.json")
            error = urllib.error.HTTPError("https://api.example/token", 403, "denied",
                                           {"cf-ray": "ray-test", "Content-Type": "text/plain"}, io.BytesIO(b"error code: 1010"))
            with patch.object(probe.urllib.request, "build_opener") as opener, contextlib.redirect_stdout(io.StringIO()):
                opener.return_value.open.side_effect = error
                with self.assertRaises(probe.ProbeError):
                    receipt.request("https://api.example/token", "not-logged", {"token": "not-logged"})
                self.assertEqual(opener.return_value.open.call_count, 1)
            saved = receipt.path.read_text()
            self.assertNotIn("not-logged", saved)
            self.assertEqual(receipt.data["requests"][0]["status"], 403)
            self.assertEqual(receipt.data["requests"][0]["cf_ray"], "ray-test")


if __name__ == "__main__":
    unittest.main()
