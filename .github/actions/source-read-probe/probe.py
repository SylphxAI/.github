#!/usr/bin/env python3
"""Signed, bounded source-read verification. Secrets stay in process memory."""
import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.sylphx.com"
POLICY = re.compile(r"orgs/[A-Za-z0-9_-]+/trust_policies/[a-z][a-z0-9_-]{0,62}\Z")
REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")


class ProbeError(Exception):
    pass


def utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def targets_from(raw, mode):
    if mode not in ("bootstrap", "read"):
        raise ProbeError("mode must be bootstrap or read")
    targets = json.loads(raw)
    if not isinstance(targets, list) or len(targets) > 4:
        raise ProbeError("targets must be an array of at most four sources")
    if mode == "read" and not targets:
        raise ProbeError("read mode requires at least one exact source")
    seen = set()
    for index in range(len(targets)):
        target = targets[index]
        if not isinstance(target, dict) or set(target) != {"repository", "commit", "trust_policy"}:
            raise ProbeError("each target requires only repository, commit and trust_policy")
        for name, pattern in (("repository", REPO), ("commit", SHA), ("trust_policy", POLICY)):
            if not isinstance(target[name], str) or not pattern.fullmatch(target[name]):
                raise ProbeError("invalid exact target " + name)
        if target["repository"] in seen:
            raise ProbeError("duplicate repository")
        seen.add(target["repository"])
        if target["repository"].split("/")[1] in (".", ".."):
            raise ProbeError("invalid repository name")
    return targets


def check_ci_name(ci_name):
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,62}", ci_name):
        raise ProbeError("invalid CI declaration name")


def mask(value):
    if not isinstance(value, str) or not value or any(c in value for c in "\r\n"):
        raise ProbeError("credential absent or malformed; not used")
    print("::add-mask::" + value, flush=True)
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProbeError("redirect refused; use the canonical source endpoint")


class Receipts:
    def __init__(self, path):
        self.path = path
        self.data = {"started_at": utc(), "requests": [], "sources": []}
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.data, indent=2) + "\n")

    def request(self, url, token=None, body=None, label=None, expected=200):
        method = "POST" if body is not None else "GET"
        # OIDC request URLs include a query; neither they nor opaque bodies are logged.
        receipt = {"utc": utc(), "method": method, "endpoint": label or url.split("?", 1)[0], "state": "submitted"}
        self.data["requests"].append(receipt)
        self.save()
        headers = {"User-Agent": "Sylphx-source-read-probe/1", "Accept": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, headers=headers, data=None if body is None else json.dumps(body).encode())
        try:
            response = urllib.request.build_opener(NoRedirect).open(request, timeout=90)
        except urllib.error.HTTPError as error:
            response = error
        except Exception:
            receipt["state"] = "unknown; do not blindly resend"
            self.save()
            raise ProbeError("request failed before a response; inspect receipt before retry") from None
        with response:
            status = response.code
            raw = response.read(4 * 1024 * 1024)
            receipt.update(status=status, request_id=response.headers.get("x-request-id") or response.headers.get("x-github-request-id"), cf_ray=response.headers.get("cf-ray"), state="answered")
        try:
            value = json.loads(raw)
        except ValueError:
            value = {}
        if not isinstance(value, dict):
            value = {}
        # Keep only non-sensitive error identifiers, never raw problem bodies.
        if status >= 400:
            receipt["code"] = value.get("code") or value.get("title")
        if isinstance(value.get("token_exchange"), str):
            receipt["token_exchange"] = value["token_exchange"]
        if isinstance(value.get("key"), dict):
            receipt["access_key"] = value["key"].get("name")
        self.save()
        print(json.dumps(receipt), flush=True)
        if status != expected:
            raise ProbeError("unexpected HTTP status for " + receipt["endpoint"])
        return value


def fetch_exact(repository, commit, token):
    """Git reads its own masked token from a transient helper, never a URL/file."""
    helper = '!f() { if [ "$1" = get ]; then printf "username=x-access-token\\npassword=%s\\n" "$SYLPHX_GIT_TOKEN"; fi; }; f'
    # Do not inherit tracing, alternate config, credential helpers or object dirs.
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("GIT_", "PROBE_", "ACTIONS_ID_TOKEN_"))}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0", SYLPHX_GIT_TOKEN=token)
    with tempfile.TemporaryDirectory(prefix="source-read-") as directory:
        def git(*args):
            result = subprocess.run(["git", "-C", directory, *args], env=env,
                                    capture_output=True, text=True, timeout=120)
            if result.returncode:
                raise ProbeError("git source-read command failed, exit " + str(result.returncode))
            return result.stdout.strip()
        git("init", "-q", "--template=")
        git("-c", "credential.helper=", "-c", "credential.helper=" + helper,
            "-c", "http.followRedirects=false", "fetch", "--depth=1", "--no-tags",
            "https://github.com/" + repository + ".git", commit)
        actual = git("rev-parse", "FETCH_HEAD")
        if actual != commit:
            raise ProbeError("FETCH_HEAD differs from exact requested commit")
        config = (Path(directory) / ".git/config").read_text()
        if token in config or "credential" in config or "[remote " in config:
            raise ProbeError("source read persisted credentials or a remote")
        return actual


def run(receipts, ci_name, mode, targets, denied_policy):
    check_ci_name(ci_name)
    if denied_policy and not POLICY.fullmatch(denied_policy):
        raise ProbeError("invalid denied-policy")
    oidc_url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
    if not oidc_url.startswith("https://"):
        raise ProbeError("caller must grant id-token: write")
    parts = urllib.parse.urlsplit(oidc_url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query = [(key, value) for key, value in query if key != "audience"]
    query.append(("audience", "sylphx-access"))
    oidc_url = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))
    oidc = mask(receipts.request(oidc_url, os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"], label="GitHub Actions OIDC").get("value"))
    exchange = receipts.request(API + "/v1/access/github/token", body={"name": ci_name, "token": oidc})
    access = mask(exchange.get("api_key"))
    key = exchange.get("key", {})
    principal = key.get("status", {}).get("principal")
    if not isinstance(principal, str) or not re.fullmatch(r"prn_[A-Za-z0-9]+", principal):
        raise ProbeError("Access response lacks the stable workload principal")
    receipts.data.update(principal=principal, ci_name=ci_name, mode=mode,
                         access_key=key.get("name"), access_expires_at=exchange.get("expires_at"))
    receipts.save()
    print(json.dumps({"principal": principal, "ci_name": ci_name, "mode": mode}), flush=True)
    if mode == "bootstrap":
        return
    delay = exchange.get("active_after_ms")
    if not isinstance(delay, int) or not 0 <= delay <= 60000:
        raise ProbeError("invalid Access propagation interval")
    time.sleep(delay / 1000)
    org = key.get("name", "").split("/api_keys/", 1)[0]
    if any(not target["trust_policy"].startswith(org + "/trust_policies/") for target in targets):
        raise ProbeError("target policies must be in the issued key's org")
    if denied_policy:
        if not denied_policy.startswith(org + "/trust_policies/"):
            raise ProbeError("denied-policy must be in the issued key's org")
        negative = receipts.request(API + "/v1/" + denied_policy + ":exchangeToken", access,
                                    {"repositories": [targets[0]["repository"]]}, expected=403)
        if negative.get("code") != "PERMISSION_DENIED" or negative.get("token") or negative.get("token_exchange"):
            raise ProbeError("foreign-subject refusal returned a credential or exchange")
    for index in range(len(targets)):
        target = targets[index]
        repository, commit, policy = (target[name] for name in ("repository", "commit", "trust_policy"))
        endpoint = API + "/v1/" + policy + ":exchangeToken"
        negative = receipts.request(endpoint, access, {"repositories": [repository], "permissions": {"contents": "write"}}, expected=400)
        if negative.get("code") != "INVALID_FIELD" or negative.get("token") or negative.get("token_exchange"):
            raise ProbeError("write-ceiling refusal lacks INVALID_FIELD or returned authority")
        token_reply = receipts.request(endpoint, access, {"repositories": [repository], "permissions": {"contents": "read"}})
        token = mask(token_reply.get("token"))
        if (token_reply.get("repositories") != [repository]
                or token_reply.get("permissions", {}).get("contents") != "read"
                or any(level not in ("read", "none") for level in token_reply.get("permissions", {}).values())
                or not token_reply.get("token_exchange")):
            raise ProbeError("Broker response is not one-repository read-only authority")
        receipts.data["requests"][-1]["token_exchange"] = token_reply["token_exchange"]
        receipts.save()
        source = receipts.request("https://api.github.com/repos/" + repository + "/commits/" + commit, token)
        if source.get("sha") != commit:
            raise ProbeError("commit GET did not return the exact requested pin")
        actual = fetch_exact(repository, commit, token)
        receipt = {**target, "utc": utc(), "fetch_head": actual, "depth": 1,
                   "token_exchange": token_reply["token_exchange"], "issuer": token_reply.get("issuer"),
                   "connection": token_reply.get("connection"), "checked_out": False}
        receipts.data["sources"].append(receipt)
        receipts.save()
        print(json.dumps(receipt), flush=True)


def main():
    mode = os.environ["PROBE_MODE"]
    targets = targets_from(os.environ["PROBE_TARGETS"], mode)
    ci_name = os.environ["PROBE_CI_NAME"]
    check_ci_name(ci_name)
    output = Path(os.environ["RUNNER_TEMP"]) / ("source-read-" + ci_name + ".json")
    if output.exists():
        raise ProbeError("receipt exists; inspect the prior submission instead of resending")
    receipt = Receipts(output)
    with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
        stream.write("receipt=" + str(output) + "\n")
    run(receipt, ci_name, mode, targets, os.environ.get("PROBE_DENIED_POLICY", ""))
    receipt.data["completed_at"] = utc()
    receipt.save()


if __name__ == "__main__":
    try:
        main()
    except (ProbeError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        # Never print arbitrary subprocess/HTTP objects carrying credentials.
        print("::error::source-read-probe failed: " + (str(error) if isinstance(error, ProbeError) else type(error).__name__), flush=True)
        raise SystemExit(1)
