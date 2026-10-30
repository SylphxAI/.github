#!/usr/bin/env python3
"""Exact-environment workload identity around the existing public capture CLI."""
import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.sylphx.com"
SCOPES = {"sandboxes:read", "sandboxes:write", "sandboxes:exec"}
PARENT = re.compile(r"orgs/(org_[A-Za-z0-9]+)/projects/(prj_[A-Za-z0-9]+)/envs/(env_[A-Za-z0-9]+)\Z")


class CaptureError(Exception):
    pass


def utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def check_identity(identity, parent):
    match = PARENT.fullmatch(parent)
    if not match:
        raise CaptureError("environment must be a canonical Access resource name")
    org, project, env = match.groups()
    if (identity.get("org") != "orgs/" + org or identity.get("project") != "orgs/" + org + "/projects/" + project
            or identity.get("env") != parent or set(identity.get("scopes", [])) != SCOPES
            or identity.get("resources")):
        raise CaptureError("workload identity is not the exact environment and three capture scopes")


class Receipt:
    def __init__(self, path):
        self.path = path
        self.data = {"started_at": utc(), "requests": []}
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.data, indent=2) + "\n")

    def request(self, endpoint, bearer=None, body=None, label=None):
        row = {"utc": utc(), "endpoint": label or endpoint.split("?", 1)[0],
               "method": "POST" if body is not None else "GET", "state": "submitted"}
        self.data["requests"].append(row)
        self.save()
        headers = {"User-Agent": "Sylphx-device-capture/1", "Accept": "application/json"}
        if bearer:
            headers["Authorization"] = "Bearer " + bearer
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(endpoint, headers=headers, data=None if body is None else json.dumps(body).encode())
        # Neither Access nor the Actions OIDC endpoint needs a redirect. Refuse
        # instead of forwarding a caller credential to another host.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                raise CaptureError("unexpected credential endpoint redirect")
        try:
            response = urllib.request.build_opener(NoRedirect).open(request, timeout=60)
        except urllib.error.HTTPError as error:
            response = error
        except Exception:
            row["state"] = "unknown; inspect before a new dispatch"
            self.save()
            raise CaptureError("identity request failed before a response; no automatic retry") from None
        with response:
            raw = response.read(2 * 1024 * 1024)
            row.update(status=response.code, state="answered", request_id=response.headers.get("x-request-id"), cf_ray=response.headers.get("cf-ray"))
        try:
            value = json.loads(raw)
        except ValueError:
            value = {}
        if not isinstance(value, dict):
            value = {}
        if row["status"] >= 400:
            row["code"] = value.get("code") or value.get("error")
        if isinstance(value.get("key"), dict):
            row["access_key"] = value["key"].get("name")
        self.save()
        print(json.dumps(row), flush=True)
        if row["status"] != 200:
            raise CaptureError("identity request refused before leasing")
        return value


def mask(token):
    if not isinstance(token, str) or not token or any(c in token for c in "\r\n"):
        raise CaptureError("identity response lacks a credential; no lease requested")
    print("::add-mask::" + token, flush=True)
    return token


def execute(receipt, options):
    ci_name, parent, app, scenario, size, version, output = options
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,62}", ci_name) or not PARENT.fullmatch(parent):
        raise CaptureError("invalid CI identity name or canonical environment")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise CaptureError("cli-version must be an exact published version")
    if not Path(app).is_file() or not Path(scenario).is_file():
        raise CaptureError("APK or scenario does not exist")
    installed = subprocess.run(["sylphx", "--version"], capture_output=True, text=True, check=False, timeout=15)
    if installed.returncode or installed.stdout.strip().split()[-1:] != [version]:
        raise CaptureError("installed CLI version differs from the published pin")
    help_result = subprocess.run(["sylphx", "devices", "run", "--help"], capture_output=True, text=True, check=False, timeout=15)
    if help_result.returncode or "--scenario" not in help_result.stdout or "--size" not in help_result.stdout:
        raise CaptureError("installed CLI does not expose native scenario capture")
    request_url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
    if not request_url.startswith("https://"):
        raise CaptureError("caller must grant id-token: write")
    parts = urllib.parse.urlsplit(request_url)
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if k != "audience"]
    query.append(("audience", "sylphx-access"))
    oidc = mask(receipt.request(urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query))),
                               os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"], label="GitHub Actions OIDC").get("value"))
    issued = receipt.request(API + "/v1/access/github/token", body={"name": ci_name, "token": oidc})
    token = mask(issued.get("api_key"))
    delay = issued.get("active_after_ms")
    if not isinstance(delay, int) or not 0 <= delay <= 60000:
        raise CaptureError("invalid identity propagation interval")
    time.sleep(delay / 1000)
    identity = receipt.request(API + "/v1/whoami", token)
    check_identity(identity, parent)
    receipt.data.update(ci_name=ci_name, environment=parent, identity=identity, cli_version=version)
    receipt.save()
    # The key exists only in this child environment. No login/keychain/config
    # writes, no token command-line argument and no copied credential files.
    env = {key: value for key, value in os.environ.items() if not key.startswith(("SYLPHX_", "ACTIONS_ID_TOKEN_", "CAPTURE_"))}
    env.update(SYLPHX_API_KEY=token, SYLPHX_BASE_URL=API, SYLPHX_NO_KEYCHAIN="1", SYLPHX_NO_BROWSER="1")
    receipt.data["capture_started_at"] = utc()
    receipt.save()
    result = subprocess.run(["sylphx", "devices", "run", "--app", app, "--env", parent,
                             "--scenario", scenario, "--size", size, "--out", str(Path(output) / "device")], env=env, check=False)
    receipt.data.update(capture_completed_at=utc(), capture_exit=result.returncode)
    receipt.save()
    if result.returncode:
        raise CaptureError("native capture did not pass; inspect the device and identity receipts")


def main():
    options = (os.environ["CAPTURE_CI_NAME"], os.environ["CAPTURE_ENVIRONMENT"],
               os.environ["CAPTURE_APP"], os.environ["CAPTURE_SCENARIO"],
               os.environ["CAPTURE_SIZE"], os.environ["CAPTURE_CLI_VERSION"], os.environ["CAPTURE_OUTPUT"])
    output = Path(options[-1])
    if output.exists() and any(output.iterdir()):
        raise CaptureError("output directory is not empty; inspect previous evidence")
    output.mkdir(parents=True, exist_ok=True)
    receipt = Receipt(output / "identity-receipt.json")
    with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
        stream.write("receipt=" + str(receipt.path) + "\n")
    execute(receipt, options)


if __name__ == "__main__":
    try:
        main()
    except (CaptureError, KeyError, ValueError, OSError, subprocess.TimeoutExpired) as error:
        print("::error::device-capture: " + (str(error) if isinstance(error, CaptureError) else type(error).__name__), flush=True)
        raise SystemExit(1)
