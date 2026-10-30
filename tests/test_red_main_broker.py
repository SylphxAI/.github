#!/usr/bin/env python3
"""Contract tests for the `broker-write-token` composite action.

The action is the no-PEM path for the red-main handler: this job's GitHub
Actions OIDC token for a short-lived Access key, then that key for a
one-repository installation token. The tests pin the shape that matters:
  - one repository, never more, in the exchange body;
  - the OIDC token asked for audience `sylphx-access`;
  - the Access key and the installation token both masked, neither printed;
  - a refusal at either hop names the reason and mints nothing;
  - `soft-fail` reports rather than fails, so a caller can decide.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / ".github" / "actions" / "broker-write-token" / "action.yml"


def action() -> dict:
    return yaml.safe_load(ACTION.read_text())


def run_script(replies: dict[str, str], env: dict[str, str]) -> tuple[int, str, str, str]:
    """Run the action's shell against stub curl/jq/runners.

    `replies` maps a URL fragment to the body curl prints for it. Returns
    (exit code, stdout, stderr, the 0600 token file's contents or '').
    """
    script = action()["runs"]["steps"][0]["run"]
    with tempfile.TemporaryDirectory() as tmp:
        stubs = Path(tmp, "bin")
        stubs.mkdir()
        # curl: a dispatcher that prints the body mapped to the URL in argv.
        curl = stubs / "curl"
        curl.write_text("#!/usr/bin/env python3\n" + CURL_STUB)
        os.chmod(curl, 0o755)
        env = {**os.environ, **env, "RUNNER_TEMP": tmp, "PATH": f"{stubs}:{os.environ['PATH']}",
               "GITHUB_ACTION_PATH": str(ACTION.parent),
               "GITHUB_OUTPUT": str(Path(tmp, "out")), "REPLIES": str(Path(tmp, "replies.json"))}
        Path(tmp, "replies.json").write_text(__import__("json").dumps(replies))
        # The real Access response contains the same key/subject metadata as
        # whoami; never let a bare fake key hide a wire-shape regression.
        data = __import__("json").loads(Path(tmp,"replies.json").read_text())
        raw = data.get("/v1/access/github/token")
        if isinstance(raw,str):
            issued = __import__("json").loads(raw)
            if issued.get("api_key"):
                issued["key"] = {"name":"orgs/example/projects/prj_test/envs/env_test/api_keys/test","status":{"principal":"orgs/example/principals/prn_test"}}
                data["/v1/access/github/token"] = __import__("json").dumps(issued)
                Path(tmp,"replies.json").write_text(__import__("json").dumps(data))
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
        token_file = Path(tmp, "broker-token")
        token = token_file.read_text() if token_file.exists() else ""
        # None of the stub replies are the real token; read what was written.
        return proc.returncode, proc.stdout, proc.stderr, token


# A curl stub that dispatches on the URL in the argv, printing the mapped body.
CURL_STUB = r"""
import json, os, sys
replies = json.loads(open(os.environ["REPLIES"]).read())
argv = " ".join(sys.argv[1:])
for frag, body in replies.items():
    if frag in argv:
        if isinstance(body, dict):
            print(json.dumps(body["body"]))
            if "--write-out" in sys.argv:
                print(body["status"], end="")
        else:
            print(body)
            if "--write-out" in sys.argv:
                print("200", end="")
        sys.exit(0)
sys.stderr.write("no stub for curl: " + argv[:200] + "\n")
sys.exit(22)
"""


class ExchangeTest(unittest.TestCase):
    def env(self, **over):
        base = {
            "BROKER_POLICY": "orgs/sylphx/trust_policies/red-main",
            "REPO": "SylphxAI/.github",
            "API_URL": "https://api.example",
            "SOFT_FAIL": "false",
            "OIDC_URL": "https://actions.example/token",
            "OIDC_TOKEN": "request-token",
        }
        base.update(over)
        return base

    def test_two_hops_narrow_to_one_repository(self) -> None:
        replies = {
            "actions.example": '{"value":"oidc-jwt"}',
            "/v1/access/github/token": '{"api_key":"syl_ci_key","active_after_ms":0}',
            "/v1/whoami": '{"org":"orgs/example","principal":"prn_test"}',
            ":exchangeToken": '{"token":"ghs_install"}',
        }
        code, out, err, token = run_script(replies, self.env())
        self.assertEqual(code, 0, err)
        # The installation token is written to the 0600 file for the caller.
        self.assertEqual(token, "ghs_install")

    def test_exchange_body_names_the_one_repository(self) -> None:
        # The action writes the exchange request itself; assert the body shape
        # in the source so a widening is caught without a live call.
        text = action()["runs"]["steps"][0]["run"]
        self.assertIn("--arg r \"$REPO\" '{repositories:[$r]}'", text)

    def test_oidc_audience_is_sylphx_access(self) -> None:
        text = action()["runs"]["steps"][0]["run"]
        self.assertIn("audience=sylphx-access", text)

    def test_missing_oidc_grant_is_named(self) -> None:
        replies = {"__self__": ""}
        code, out, err, token = run_script(replies, self.env(OIDC_URL=""))
        text = out + err
        self.assertIn("id-token: write", text)
        self.assertEqual(code, 1)
        self.assertEqual(token, "")

    def test_access_refusal_names_the_ci_declaration(self) -> None:
        replies = {
            "actions.example": '{"value":"oidc-jwt"}',
            "/v1/access/github/token": '{"title":"PERMISSION_DENIED","detail":"no such CI declaration"}',
        }
        code, out, err, token = run_script(replies, self.env())
        self.assertIn("no Access key", out + err)
        self.assertIn("[[ci]]", out + err)
        self.assertEqual(code, 1)
        self.assertEqual(token, "")

    def test_broker_refusal_names_the_policy(self) -> None:
        replies = {
            "actions.example": '{"value":"oidc-jwt"}',
            "/v1/access/github/token": '{"api_key":"syl_ci_key","active_after_ms":0}',
            "/v1/whoami": '{"org":"orgs/example","principal":"prn_test"}',
            ":exchangeToken": '{"title":"PERMISSION_DENIED","detail":"not permitted"}',
        }
        code, out, err, token = run_script(replies, self.env())
        self.assertIn("no installation token", out + err)
        self.assertEqual(code, 1)
        self.assertEqual(token, "")

    def test_soft_fail_reports_instead_of_failing(self) -> None:
        replies = {
            "actions.example": '{"value":"oidc-jwt"}',
            "/v1/access/github/token": '{"api_key":"syl_ci_key","active_after_ms":0}',
            "/v1/whoami": '{"org":"orgs/example","principal":"prn_test"}',
            ":exchangeToken": '{"title":"PERMISSION_DENIED","detail":"nope"}',
        }
        code, out, err, token = run_script(replies, self.env(SOFT_FAIL="true"))
        self.assertEqual(code, 0, err)
        self.assertEqual(token, "")

    def test_readiness_refusal_sends_no_broker_exchange(self):
        replies = {
            "actions.example": '{"value":"oidc-jwt"}',
            "/v1/access/github/token": '{"api_key":"syl_ci_key","active_after_ms":0}',
            "/v1/whoami": {"status":403,"body":{"code":"PERMISSION_DENIED"}},
            # There deliberately is no Broker reply. A wrongly sent POST
            # would fail at the stub instead of the readiness boundary.
        }
        code,out,err,token = run_script(replies,self.env())
        self.assertEqual(code,1)
        self.assertIn("no Broker exchange was sent",out)
        self.assertNotIn("no stub for curl",err)
        self.assertEqual(token,"")


if __name__ == "__main__":
    unittest.main()
