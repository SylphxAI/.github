#!/usr/bin/env python3
"""Read readiness of one already-issued Access key; never mint or retry writes."""
import argparse
import json
import subprocess
import sys
import time
from urllib.parse import urlsplit


class ReadinessError(Exception):
    pass


def wait_for_identity(read, token, active_after_ms, api_url):
    if type(active_after_ms) is not int or not 0 <= active_after_ms <= 60000:
        raise ReadinessError("invalid Access propagation interval")
    time.sleep(active_after_ms / 1000)
    endpoint = api_url.rstrip("/") + "/v1/whoami"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        identity = read(endpoint, token, allow_propagation=True, timeout=remaining)
        if time.monotonic() >= deadline:
            break
        if identity.get("code") != "key_not_yet_propagated":
            return identity
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    raise ReadinessError("issued Access key did not become ready within 30 seconds")


def read_identity(endpoint, token, *, allow_propagation, timeout):
    # Header travels on stdin, never argv, a token URL or a credential file.
    config = 'header = "Authorization: Bearer ' + token.replace('\\', '\\\\').replace('"', '\\"') + '"\n'
    result = subprocess.run(["curl", "-sS", "--max-time", str(timeout), "-K", "-",
                             "--user-agent", "Sylphx-access-readiness/1", "--url", endpoint,
                             "--write-out", "\n%{http_code}"], input=config,
                            capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise ReadinessError("Access readiness read failed at the network")
    try:
        body, status = result.stdout.rsplit("\n", 1)
        value = json.loads(body)
        status = int(status)
    except (ValueError, TypeError):
        raise ReadinessError("Access readiness returned an invalid response") from None
    if not isinstance(value, dict):
        raise ReadinessError("Access readiness returned an invalid identity")
    if status == 200:
        return value
    if allow_propagation and status == 401 and value.get("code") == "key_not_yet_propagated":
        return value
    raise ReadinessError("Access readiness refused with HTTP " + str(status))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", required=True)
    args = parser.parse_args()
    url = urlsplit(args.api_url)
    if url.scheme != "https" or not url.netloc or url.query or url.fragment or url.username or url.password:
        raise ReadinessError("Access readiness requires the ordinary HTTPS API endpoint")
    issued = json.load(sys.stdin)
    token = issued.get("api_key")
    if not isinstance(token, str) or not token or any(c in token for c in "\r\n"):
        raise ReadinessError("issued Access key is absent or malformed")
    identity = wait_for_identity(read_identity, token, issued.get("active_after_ms"), args.api_url)
    key = issued.get("key", {})
    name = key.get("name", "")
    org = name.split("/api_keys/", 1)[0]
    principal = key.get("status", {}).get("principal", "")
    if (not org.startswith("orgs/") or identity.get("org") != org
            or principal != org + "/principals/" + identity.get("principal", "")):
        raise ReadinessError("ready identity differs from the issued key")
    print(json.dumps({"ready": True, "principal": identity.get("principal"), "org": identity.get("org")}))


if __name__ == "__main__":
    try:
        main()
    except (ReadinessError, KeyError, ValueError, TypeError, subprocess.TimeoutExpired):
        print("Access readiness failed; no Broker request was sent", file=sys.stderr)
        raise SystemExit(1)
