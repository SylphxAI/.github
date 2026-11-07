#!/usr/bin/env python3
"""Bounded curl requests with allowlisted, content-free failure diagnostics.

Bodies belong in caller-owned files. Secrets (including signed URLs) belong in
stdin config, not argv. Each replay uses identical config, body and destination;
callers remain responsible for the endpoint's idempotency contract.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from email.parser import BytesParser
import ipaddress
import math
from pathlib import Path
import random
import re
import selectors
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

TRANSIENT = {6, 7, 28, 35, 52, 56}
HTTP_REDIRECTS = ("--location", "--max-redirs", "5", "--proto-redir", "=http,https")
FIELDS = ("time_namelookup", "time_connect", "time_appconnect",
          "time_starttransfer", "time_total", "remote_ip", "http_code")
WRITE_OUT = "\t".join("%{" + field + "}" for field in FIELDS)


@dataclass(frozen=True)
class Policy:
    attempts: int = 5
    budget: float = 120
    attempt_time: float = 30
    base_delay: float = 2

    def __post_init__(self):
        if (not 1 <= self.attempts <= 5 or
                any(not math.isfinite(n) or n <= 0 for n in (self.budget, self.attempt_time)) or
                not math.isfinite(self.base_delay) or self.base_delay < 0):
            raise ValueError("invalid network retry policy")


@dataclass(frozen=True)
class Result:
    code: str
    returncode: int

    def require_success(self, label):
        if self.returncode or not self.code.startswith("2"):
            cause = f"curl exit {self.returncode}" if self.returncode else f"HTTP {self.code}"
            raise OSError(f"{label}: {cause}")  # Never include stderr, URL or body.


def metrics(raw):
    """Do not forward arbitrary curl output, even when malformed/truncated."""
    values = raw.decode("ascii", errors="replace").strip().split("\t")
    result = dict.fromkeys(FIELDS, "unavailable")
    result["http_code"] = "000"
    if len(values) != len(FIELDS):
        return result
    for field, value in zip(FIELDS[:5], values[:5]):
        if re.fullmatch(r"[0-9]{1,12}\.[0-9]{1,9}", value):
            result[field] = value
    try:
        if "%" not in values[5]:  # A zone identifier is not an IP address.
            result["remote_ip"] = str(ipaddress.ip_address(values[5]))
    except ValueError:
        pass
    if re.fullmatch(r"[0-9]{3}", values[6]):
        result["http_code"] = values[6]
    return result


def stream_curl(args, config, destination, max_bytes, timeout):
    """Bound stdout before disk writes, including chunked bodies on older curl."""
    deadline = time.monotonic() + timeout
    # The private, unnamed files hold input config and timing output only.
    with tempfile.TemporaryFile() as source, tempfile.TemporaryFile() as timing, open(destination, "wb") as body:
        source.write(config)
        source.seek(0)
        with subprocess.Popen(["curl", "-q", "-s", *args, "-o", "-", "-w", "%{stderr}" + WRITE_OUT],
                              stdin=source, stdout=subprocess.PIPE, stderr=timing) as process:
            received, forced_exit = 0, None
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0 or not selector.select(remaining):
                            forced_exit = 28
                            break
                        chunk = process.stdout.read1(1024 * 1024)
                        if not chunk:
                            break
                        received += len(chunk)
                        if received > max_bytes:
                            forced_exit = 63
                            break
                        body.write(chunk)
                if forced_exit is not None:
                    process.kill()
                try:
                    process.wait(timeout=max(0.001, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    forced_exit = 28
                    process.kill()
                    process.wait()
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
            timing.seek(0)
            return subprocess.CompletedProcess(args, forced_exit or process.returncode, timing.read(16384))


def run(curl_args, *, config=b"", label, policy=Policy(), destination=None, max_bytes=None):
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,47}", label):
        raise ValueError("invalid network diagnostic label")
    deadline = time.monotonic() + policy.budget
    result = Result("000", 28)
    for attempt in range(1, policy.attempts + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        limit = min(policy.attempt_time, remaining)
        try:
            # -q must be first: ~/.curlrc must not enable verbose output,
            # extra retries or credential forwarding. Explicit bounds win over
            # caller arguments, and the subprocess deadline covers curl too.
            if max_bytes is not None:
                process = stream_curl([*curl_args, "--max-time", str(limit)], config,
                                      destination, max_bytes, remaining)
            else:
                process = subprocess.run(
                    ["curl", "-q", "-sS", *curl_args, "--max-time", str(limit),
                     "--write-out", WRITE_OUT], input=config, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, timeout=remaining)
            diagnostic = metrics(process.stdout)
            result = Result(diagnostic["http_code"], process.returncode)
        except subprocess.TimeoutExpired:
            diagnostic = metrics(b"")
            result = Result("000", 28)
        if result.returncode == 0 and result.code != "000" and int(result.code) < 400:
            return result
        # A witnessed permanent refusal wins even if its body then stalls.
        retryable = result.code not in {"401", "403", "404"} and (
            result.returncode in TRANSIENT if result.returncode else
            result.code == "429" or 500 <= int(result.code) <= 599)
        remaining = deadline - time.monotonic()
        stop = ("permanent" if not retryable else "attempts-exhausted"
                if attempt == policy.attempts else "budget-exhausted" if remaining <= 0 else "retry")
        delay = min(policy.base_delay * 2 ** (attempt - 1) * random.uniform(1, 1.25),
                    max(0, remaining)) if stop == "retry" else 0
        if delay >= remaining and stop == "retry":
            stop, delay = "budget-exhausted", 0
        detail = " ".join(f"{field}={diagnostic[field]}" for field in FIELDS)
        print(f"::warning::network {label}: attempt={attempt}/{policy.attempts} "
              f"curl_exit={result.returncode} {detail} stop={stop} retry_in={delay:.3f}s",
              file=sys.stderr, flush=True)
        if stop != "retry":
            return result
        time.sleep(delay)
    return result


def quote(value):
    if any(c in value for c in "\r\n\0"):
        raise ValueError("invalid network config value")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def request(url, destination, *, label, headers=(), curl_args=(), policy=Policy(), max_bytes=None):
    config = "url = " + quote(url) + "\n"
    config += "".join("header = " + quote(header) + "\n" for header in headers)
    output = ["-o", str(destination)] if max_bytes is None else []
    return run(["-K", "-", *output, *curl_args], config=config.encode(), label=label,
               policy=policy, destination=destination, max_bytes=max_bytes)


def read(url, *, label, headers=(), curl_args=(), policy=Policy()):
    with tempfile.TemporaryDirectory(prefix="network-read-") as work:
        path = Path(work) / "body"
        result = request(url, path, label=label, headers=headers, curl_args=curl_args, policy=policy)
        result.require_success(label)
        return path.read_bytes()


@contextmanager
def download(url, *, label, max_bytes, redirect, policy=Policy()):
    """Disk-buffered archive read; validate each redirect before contacting it."""
    deadline = time.monotonic() + policy.budget
    with tempfile.TemporaryDirectory(prefix="network-download-") as work:
        body, headers = Path(work) / "body", Path(work) / "headers"
        for _ in range(6):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OSError(f"{label}: network budget exhausted")
            result = request(url, body, label=label, max_bytes=max_bytes,
                             curl_args=("--max-filesize", str(max_bytes), "-D", str(headers)),
                             policy=Policy(policy.attempts, remaining, policy.attempt_time, policy.base_delay))
            if result.returncode == 63:
                raise ValueError("archive exceeds size limit")
            if result.code in ("301", "302", "303", "307", "308") and not result.returncode:
                # Only headers affect routing, never diagnostic output. Curl
                # may prefix an HTTPS proxy's CONNECT response; use the last block.
                blocks = headers.read_bytes().replace(b"\r\n", b"\n").strip().split(b"\n\n")
                parsed = BytesParser().parsebytes(blocks[-1].split(b"\n", 1)[-1])
                location = parsed.get("Location")
                if not location:
                    raise ValueError("archive redirect lacks a destination")
                target = urllib.parse.urljoin(url, location)
                redirected = redirect.redirect_request(urllib.request.Request(url), None,
                                                       int(result.code), "", parsed, target)
                if redirected is None:
                    raise ValueError("archive redirect refused")
                url = redirected.full_url
                continue
            result.require_success(label)
            with body.open("rb") as stream:
                yield stream
            return
        raise ValueError("too many archive redirects")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True)
    parser.add_argument("--budget", type=float, default=120)
    parser.add_argument("--max-time", type=float, default=30)
    parser.add_argument("--base-delay", type=float, default=2)
    parser.add_argument("--fail-http", action="store_true")
    parser.add_argument("curl_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    curl_args = args.curl_args[1:] if args.curl_args[:1] == ["--"] else args.curl_args
    # All CLI consumers use -K - so a replay can reread the same private input.
    config = sys.stdin.buffer.read() if "-K" in curl_args else b""
    result = run(curl_args, config=config, label=args.label,
                 policy=Policy(budget=args.budget, attempt_time=args.max_time, base_delay=args.base_delay))
    print(result.code)
    return result.returncode or (22 if args.fail_http and int(result.code) >= 400 else 0)


if __name__ == "__main__":
    sys.exit(main())
