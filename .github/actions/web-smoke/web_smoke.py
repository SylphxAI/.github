#!/usr/bin/env python3
"""web-smoke: boot a Keel title's packed web build in headless Chrome and require a splash and a drawn frame.

usage: web_smoke.py ref    [--ref REF] [--pin-file FILE]
       web_smoke.py keel   --ref REF --dest DIR          (Keel's scripts/ at REF; token in KEEL_TOKEN)
       web_smoke.py chrome --version V --dest DIR        (Chrome for Testing, headless shell + chromedriver)
       web_smoke.py run    --keel DIR --pack DIR --chrome DIR [options]

`run` serves the pack the way a static host does (Keel's scripts/static_host.py, so the pack's
`_headers` apply) and runs two checks from Keel's scripts/browser_smoke.py against it:

  1. `--slow-3g-splash`: a cold load on Chrome's "Slow 3G" paints the title's splash within the
     budget (1 s) and its bar moves, before any script or wasm has arrived;
  2. `--live`: the title draws a ready frame within the budget with no boot failure and a quiet
     console (a black screen never gets a ready frame), and, with --tap, takes one touch tap.

A title's boot may call an endpoint the smoke cannot reach: the page is served from 127.0.0.1, an origin
the title's API does not allow, so its guest sign-in is CORS-blocked. Those are the *offline
dependencies* (`--offline-dependency`, default: the Cubeage auth endpoints): a network or CORS failure
that names one is skipped and listed in the output. Every other failed request, a console error, a
panic and a missing frame still fail the check.

Both always run, then a third check reads the pack's wasm modules the way a host sends them with
`Accept-Encoding: br`: a module that has a precompressed `<file>.br` beside it is sent as that file, so
its size over brotli quality 11 of the module (served/q11) must stay within the bound (1.03). A module
with no `.br` is compressed on the fly by the host (about +25 %); that fails only with
`--require-precompressed`. The exit status is 1 when any check fails. Self-hosted runners only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

CFT_BASE = "https://storage.googleapis.com/chrome-for-testing-public/%s/linux64"
SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
LIBS = (
    "libglib2.0-0t64 libnss3 libnspr4 libdbus-1-3 libatk1.0-0t64 libatk-bridge2.0-0t64 libcups2t64 "
    "libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2t64 "
    "libatspi2.0-0t64 libudev1 libx11-6 libxcb1 libxext6 libdrm2 libexpat1 fontconfig-config fonts-dejavu-core"
).split()


class Refused(Exception):
    pass


def refuse_hosted() -> None:
    """GitHub-hosted runners cost money: this check runs on our own runners only."""
    if os.environ.get("RUNNER_ENVIRONMENT", "") == "github-hosted":
        raise Refused("web-smoke runs on our own runners only: this job is on a GitHub-hosted runner")


def resolve_ref(ref: str, pin_file: str) -> str:
    """The Keel revision whose scripts judge the pack: the input, else the title's own KEEL_PIN."""
    value = ref.strip()
    if not value:
        path = Path(pin_file)
        if not path.is_file():
            raise Refused("no keel-ref given and %s does not exist: say which Keel revision's smoke script to use" % pin_file)
        value = path.read_text().strip()
    if not value:
        raise Refused("%s is empty" % pin_file)
    if not SAFE_REF.match(value) or ".." in value or value.startswith("-"):
        raise Refused("not a usable Keel revision: %r" % value)
    return value


def set_output(name: str, value: str) -> None:
    """A step output for the action (or stdout when run by hand)."""
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a") as f:
            f.write("%s=%s\n" % (name, value))
    else:
        print("%s=%s" % (name, value))


def run(cmd, **kw):
    return subprocess.run(cmd, text=True, capture_output=True, **kw)


def fetch_keel_scripts(ref: str, dest: str, token_env: str = "KEEL_TOKEN") -> None:
    """Keel's scripts/ at REF into DEST, read through the read-only token in the environment (never in argv)."""
    token = os.environ.get(token_env, "")
    if not token:
        raise Refused("no read token for the Keel repository (%s is empty)" % token_env)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0",
           "GIT_CONFIG_COUNT": "1",
           "GIT_CONFIG_KEY_0": "url.https://x-access-token:%s@github.com/SylphxAI/keel.insteadOf" % token,
           "GIT_CONFIG_VALUE_0": "https://github.com/SylphxAI/keel"}
    Path(dest).mkdir(parents=True, exist_ok=True)
    steps = [["git", "init", "-q"],
             ["git", "remote", "add", "origin", "https://github.com/SylphxAI/keel"],
             ["git", "fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", ref],
             ["git", "checkout", "-q", "FETCH_HEAD", "--", "scripts"]]
    for cmd in steps:
        p = run(cmd, cwd=dest, env=env)
        if p.returncode != 0:
            # The token sits only in the environment, but scrub it anyway before showing git's words.
            raise Refused("`%s` failed: %s" % (" ".join(cmd[:3]), p.stderr.replace(token, "***").strip()))
    sha = run(["git", "rev-parse", "HEAD"], cwd=dest).stdout.strip()
    print("Keel scripts at %s (%s)" % (ref, sha))


def chrome_binaries(root: str) -> list[str]:
    return [os.path.join(root, "chromedriver-linux64", "chromedriver"),
            os.path.join(root, "chrome-headless-shell-linux64", "chrome-headless-shell")]


def libs_missing(binaries: list[str]) -> bool:
    out = run(["ldd", *binaries]).stdout
    return "not found" in out or not Path("/etc/fonts/fonts.conf").is_file() \
        or not Path("/usr/share/fonts/truetype/dejavu").is_dir()


def install_chrome(version: str, dest: str) -> str:
    """Chrome for Testing VERSION (headless shell and chromedriver) under DEST; returns the directory."""
    if not re.match(r"^\d+\.\d+\.\d+\.\d+$", version):
        raise Refused("not a Chrome for Testing version: %r" % version)
    root = os.path.join(dest, "chrome-for-testing-" + version)
    if not all(os.access(b, os.X_OK) for b in chrome_binaries(root)):
        shutil.rmtree(root, ignore_errors=True)
        Path(root).mkdir(parents=True)
        for name in ("chromedriver-linux64", "chrome-headless-shell-linux64"):
            url = "%s/%s.zip" % (CFT_BASE % version, name)
            archive = os.path.join(root, name + ".zip")
            with urllib.request.urlopen(url, timeout=180) as r, open(archive, "wb") as f:
                shutil.copyfileobj(r, f)
            with zipfile.ZipFile(archive) as z:
                z.extractall(root)
            os.unlink(archive)
        for b in chrome_binaries(root):
            os.chmod(b, 0o755)
    bins = chrome_binaries(root)
    if libs_missing(bins):
        # The runner image carries no desktop libraries or fonts by default.
        subprocess.run(["sudo", "apt-get", "update", "-qq"], check=False)
        subprocess.run(["sudo", "apt-get", "install", "-y", "-qq", "--no-install-recommends", *LIBS], check=False)
    missing = [l for l in run(["ldd", *bins]).stdout.splitlines() if "not found" in l]
    if missing:
        raise Refused("headless Chrome is missing libraries:\n" + "\n".join(missing))
    return root


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_host(keel: str, pack: str, port: int) -> subprocess.Popen:
    host = os.path.join(keel, "scripts", "static_host.py")
    proc = subprocess.Popen([sys.executable, host, "--dir", pack, str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = "http://127.0.0.1:%d/index.html" % port
    for _ in range(100):
        if proc.poll() is not None:
            raise Refused("the static host exited with status %s" % proc.returncode)
        try:
            urllib.request.urlopen(url, timeout=2).close()
            return proc
        except Exception:
            time.sleep(0.2)
    proc.terminate()
    raise Refused("the static host did not serve %s in 20 s" % url)


def supports(keel: str, flag: str) -> bool:
    p = run([sys.executable, os.path.join(keel, "scripts", "browser_smoke.py"), "--help"])
    return flag in (p.stdout + p.stderr)


DEFAULT_OFFLINE = [r"api\.cubeage\.com/cubeage\.v1\.AuthService/"]
# What a browser logs when a request fails before an answer: the CORS refusal and the bare network line.
NETWORK_FAILURE = r"blocked by CORS policy|net::ERR_"


def offline_ignores(dependencies) -> list[str]:
    """Console-ignore regexes for a network or CORS failure naming a known offline dependency.

    A line is skipped only when it names the dependency AND is a network or CORS failure, so a 500 or
    an error the title itself logs for the same endpoint still fails.
    """
    out = []
    for dep in dependencies:
        dep = dep.strip()
        if dep:
            re.compile(dep)  # a bad pattern is the title's mistake: fail loudly, not silently pass
            out.append(r"(?s)^(?=.*(?:%s))(?=.*(?:%s))" % (dep, NETWORK_FAILURE))
    return out


def smoke_commands(keel: str, chrome: str, url: str, opts) -> list[tuple[str, list[str]]]:
    script = os.path.join(keel, "scripts", "browser_smoke.py")
    commands = []
    if opts.slow_3g_splash:
        commands.append(("slow-3g-splash", [sys.executable, script, "--chrome", chrome, "--slow-3g-splash",
                                            "--splash-budget-ms", str(opts.splash_budget_ms),
                                            "--budget-s", str(opts.budget_s), url]))
    live = [sys.executable, script, "--chrome", chrome, "--live", "--viewport", opts.viewport, "--dpr", "1",
            "--budget-s", str(opts.budget_s)]
    if opts.tap:
        live += ["--tap", opts.tap]
    for pattern in [*opts.ignore_console, *offline_ignores(opts.offline_dependency)]:
        live += ["--ignore-console", pattern]
    if opts.report:
        live += ["--report", opts.report]
    commands.append(("boot", live + [url]))
    return commands


def brotli_module():
    """The `brotli` Python module; installed for the runner's user when it is missing."""
    try:
        import brotli  # type: ignore
        return brotli
    except ImportError:
        pass
    p = run([sys.executable, "-m", "pip", "install", "--quiet", "--user", "--break-system-packages", "brotli==1.1.0"])
    if p.returncode != 0:
        raise Refused("the served-size check needs the Python brotli module and pip could not install it:\n" + p.stderr[-400:])
    import importlib
    import site
    importlib.invalidate_caches()
    site.addsitedir(site.getusersitepackages())
    import brotli  # type: ignore
    return brotli


def q11_size(brotli, data: bytes) -> int:
    return len(brotli.compress(data, quality=11, lgwin=24, mode=brotli.MODE_GENERIC))


def served_q11(pack: Path, max_ratio: float, require_precompressed: bool, brotli=None) -> tuple[bool, str]:
    """served/q11 of every wasm module in the pack, as a host that sends `<file>.br` serves it."""
    modules = sorted(f for f in pack.rglob("*.wasm") if f.relative_to(pack).parts[0] != "assets")
    if not modules:
        return True, "served-q11: the pack holds no wasm module"
    lines, failed = [], False
    for f in modules:
        rel = f.relative_to(pack).as_posix()
        sibling = Path(str(f) + ".br")
        if not sibling.is_file() and not require_precompressed:
            # Nothing to measure, and the brotli module is only fetched when something is.
            lines.append("note %s: no precompressed %s.br in the pack; served/q11 not measured" % (rel, rel))
            continue
        brotli = brotli or brotli_module()
        best = q11_size(brotli, f.read_bytes())
        if sibling.is_file():
            wire = sibling.stat().st_size
            if brotli.decompress(sibling.read_bytes()) != f.read_bytes():
                lines.append("FAIL %s: %s.br does not decode to the module" % (rel, rel))
                failed = True
                continue
            ratio = wire / best
            verdict = "ok"
            if ratio > max_ratio:
                verdict = "FAIL"
                failed = True
            lines.append("%s %s: served/q11 %.3f (%d B served, %d B at q11)%s"
                         % (verdict, rel, ratio, wire, best,
                            "" if verdict == "ok" else ", over the bound of %.2f" % max_ratio))
        else:
            lines.append("FAIL %s: no precompressed %s.br; a host compresses it on the fly at about +25 %% over q11 (%d B)"
                         % (rel, rel, best))
            failed = True
    return not failed, "\n".join(lines)


def summary_text(results: list[tuple[str, bool, str]], url: str, ref: str, offline=()) -> str:
    lines = ["### web-smoke", "", "Pack served from `%s`, checked with Keel `%s`." % (url, ref), "",
             "| Check | Result |", "| --- | --- |"]
    for name, ok, _ in results:
        lines.append("| %s | %s |" % (name, "pass" if ok else "FAIL"))
    for name, ok, out in results:
        if not ok:
            fails = [l for l in out.splitlines() if l.startswith("FAIL")] or out.strip().splitlines()[-5:]
            lines += ["", "`%s`:" % name, "", "```", *fails, "```"]
    if offline:
        lines += ["", "Offline dependencies (a network or CORS failure naming one is skipped): %s."
                  % ", ".join("`%s`" % d for d in offline)]
    lines += ["", "A desk-class browser on a software GPU, not a phone: it proves the title shows a splash and starts, "
              "not how fast it draws."]
    return "\n".join(lines) + "\n"


def cmd_run(opts) -> int:
    refuse_hosted()
    pack = Path(opts.pack)
    index = pack / "index.html"
    if not index.is_file() or index.stat().st_size == 0:
        raise Refused("%s has no index.html: pass the directory `keel pack --profile web` wrote" % opts.pack)
    if opts.slow_3g_splash and not supports(opts.keel, "--slow-3g-splash"):
        raise Refused("the Keel scripts used here (%s) have no --slow-3g-splash: they predate the web boot standard. "
                      "Repin the title, or pass a newer keel-ref." % opts.ref)
    deps = [d.strip() for d in opts.offline_dependency if d.strip()]
    try:
        offline_ignores(deps)
    except re.error as e:
        raise Refused("offline-dependencies holds a bad regular expression: %s" % e)
    print("offline dependencies (network or CORS failures skipped, anything else fails): %s"
          % (", ".join(deps) or "none"))
    port = free_port()
    served_ok, served_out = served_q11(pack, opts.max_served_ratio, opts.require_precompressed)
    print("::group::served-q11")
    print(served_out)
    print("::endgroup::")
    if not served_ok:
        print("::error title=web-smoke served-q11::%s" % next((l for l in served_out.splitlines() if l.startswith("FAIL")), "failed"))
    host = start_host(opts.keel, str(pack), port)
    url = "http://127.0.0.1:%d/index.html" % port
    results = [("served-q11", served_ok, served_out)]
    try:
        for name, cmd in smoke_commands(opts.keel, opts.chrome, url, opts):
            print("::group::%s" % name)
            try:
                p = run(cmd, timeout=opts.timeout_s)
                out, ok = p.stdout + p.stderr, p.returncode == 0
            except subprocess.TimeoutExpired:
                out, ok = "FAIL: %s did not finish in %d s" % (name, opts.timeout_s), False
            print(out.rstrip())
            print("::endgroup::")
            results.append((name, ok, out))
            if not ok:
                print("::error title=web-smoke %s::%s" % (name, next((l for l in out.splitlines() if l.startswith("FAIL")), "failed")))
    finally:
        host.terminate()
        try:
            host.wait(timeout=10)
        except subprocess.TimeoutExpired:
            host.kill()
    text = summary_text(results, url, opts.ref, deps)
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a") as f:
            f.write(text)
    return 0 if all(ok for _, ok, _ in results) else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("ref")
    r.add_argument("--ref", default="")
    r.add_argument("--pin-file", default="KEEL_PIN")
    k = sub.add_parser("keel")
    k.add_argument("--ref", required=True)
    k.add_argument("--dest", required=True)
    c = sub.add_parser("chrome")
    c.add_argument("--version", required=True)
    c.add_argument("--dest", required=True)
    u = sub.add_parser("run")
    u.add_argument("--keel", required=True)
    u.add_argument("--pack", required=True)
    u.add_argument("--chrome", required=True)
    u.add_argument("--ref", default="(unknown)")
    u.add_argument("--tap", default="")
    u.add_argument("--viewport", default="390x844,touch")
    u.add_argument("--budget-s", type=float, default=10.0)
    u.add_argument("--splash-budget-ms", type=float, default=1000.0)
    u.add_argument("--slow-3g-splash", type=lambda v: v.lower() != "false", default=True)
    u.add_argument("--ignore-console", action="append", default=[])
    u.add_argument("--offline-dependency", action="append", default=None)
    u.add_argument("--report", default="")
    u.add_argument("--max-served-ratio", type=float, default=1.03)
    u.add_argument("--require-precompressed", type=lambda v: v.lower() == "true", default=False)
    u.add_argument("--timeout-s", type=int, default=300)
    opts = parser.parse_args(argv)
    if getattr(opts, "offline_dependency", 0) is None:  # by hand: the default list; the action always passes the input
        opts.offline_dependency = list(DEFAULT_OFFLINE)
    try:
        if opts.cmd == "ref":
            set_output("ref", resolve_ref(opts.ref, opts.pin_file))
        elif opts.cmd == "keel":
            refuse_hosted()
            fetch_keel_scripts(opts.ref, opts.dest)
        elif opts.cmd == "chrome":
            refuse_hosted()
            set_output("dir", install_chrome(opts.version, opts.dest))
        else:
            return cmd_run(opts)
    except Refused as e:
        print("::error title=web-smoke::%s" % str(e).splitlines()[0], file=sys.stderr)
        print(e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
