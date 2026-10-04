#!/usr/bin/env python3
"""Tests for the web-smoke action: Keel revision choice, the runner guard, the run with a stand-in Keel scripts/ directory."""

from __future__ import annotations

import importlib.util
import io
import os
import pathlib
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
ACTION = ROOT / ".github" / "actions" / "web-smoke"
SPEC = importlib.util.spec_from_file_location("web_smoke", ACTION / "web_smoke.py")
web_smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(web_smoke)

SHA = "a" * 40

# Keel's static host, reduced to what the action needs: serve DIR on PORT.
STATIC_HOST = textwrap.dedent("""
    import argparse, functools, http.server
    p = argparse.ArgumentParser()
    p.add_argument("--dir"); p.add_argument("port", type=int)
    a = p.parse_args()
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=a.dir)
    http.server.ThreadingHTTPServer(("127.0.0.1", a.port), handler).serve_forever()
""")

# browser_smoke.py reduced to its command line: it records how it was called and exits as the
# pack says (a pack whose index.html holds "BLACK" is a black boot: both modes fail on it).
BROWSER_SMOKE = textwrap.dedent("""
    import sys, urllib.request, os
    argv = sys.argv[1:]
    if "--help" in argv:
        print("usage: browser_smoke.py ... " + os.environ.get("STUB_FLAGS", "--slow-3g-splash --live"))
        sys.exit(0)
    mode = "slow-3g-splash" if "--slow-3g-splash" in argv else "live"
    open(os.environ["STUB_LOG"], "a").write(mode + " " + " ".join(argv) + "\\n")
    body = urllib.request.urlopen(argv[-1]).read().decode()
    if "BLACK" in body:
        print("FAIL: nothing painted on Slow 3G within 10 s" if mode == "slow-3g-splash" else "FAIL: no ready frame within 10 s")
        sys.exit(1)
    print("%s ok" % mode)
""")


def make_keel(base: pathlib.Path) -> pathlib.Path:
    scripts = base / "keel" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "static_host.py").write_text(STATIC_HOST)
    (scripts / "browser_smoke.py").write_text(BROWSER_SMOKE)
    return base / "keel"


def make_pack(base: pathlib.Path, body: str) -> pathlib.Path:
    pack = base / "dist"
    pack.mkdir()
    (pack / "index.html").write_text(body)
    return pack


class Options:
    def __init__(self, **kw):
        self.__dict__.update(dict(tap="", viewport="390x844,touch", budget_s=10.0, splash_budget_ms=1000.0,
                                  slow_3g_splash=True, ignore_console=["blocked by CORS policy"],
                                  report="", timeout_s=60, ref=SHA, chrome="/chrome",
                                  max_served_ratio=1.03, require_precompressed=False))
        self.__dict__.update(kw)


class ResolveRef(unittest.TestCase):
    def test_input_wins_over_the_pin_file(self):
        with tempfile.TemporaryDirectory() as d:
            pin = pathlib.Path(d, "KEEL_PIN")
            pin.write_text("b" * 40 + "\n")
            self.assertEqual(web_smoke.resolve_ref(SHA, str(pin)), SHA)

    def test_pin_file_is_the_default(self):
        with tempfile.TemporaryDirectory() as d:
            pin = pathlib.Path(d, "KEEL_PIN")
            pin.write_text(" " + SHA + "\n")
            self.assertEqual(web_smoke.resolve_ref("", str(pin)), SHA)

    def test_nothing_to_go_on_is_refused(self):
        with self.assertRaises(web_smoke.Refused):
            web_smoke.resolve_ref("", "/nonexistent/KEEL_PIN")
        with tempfile.TemporaryDirectory() as d:
            pin = pathlib.Path(d, "KEEL_PIN")
            pin.write_text("\n")
            with self.assertRaises(web_smoke.Refused):
                web_smoke.resolve_ref("", str(pin))

    def test_unsafe_refs_are_refused(self):
        for bad in ("--upload-pack=x", "a b", "../x", "x;rm", "$(id)"):
            with self.assertRaises(web_smoke.Refused, msg=bad):
                web_smoke.resolve_ref(bad, "KEEL_PIN")


class RunnerGuard(unittest.TestCase):
    def test_github_hosted_is_refused(self):
        with mock.patch.dict(os.environ, {"RUNNER_ENVIRONMENT": "github-hosted"}):
            with self.assertRaises(web_smoke.Refused) as e:
                web_smoke.refuse_hosted()
            self.assertIn("own runners only", str(e.exception))

    def test_self_hosted_and_local_pass(self):
        with mock.patch.dict(os.environ, {"RUNNER_ENVIRONMENT": "self-hosted"}):
            web_smoke.refuse_hosted()
        env = {k: v for k, v in os.environ.items() if k != "RUNNER_ENVIRONMENT"}
        with mock.patch.dict(os.environ, env, clear=True):
            web_smoke.refuse_hosted()

    def test_every_entry_point_checks_the_runner(self):
        with mock.patch.dict(os.environ, {"RUNNER_ENVIRONMENT": "github-hosted"}):
            for argv in (["keel", "--ref", SHA, "--dest", "/tmp/x"],
                         ["chrome", "--version", "1.2.3.4", "--dest", "/tmp/x"],
                         ["run", "--keel", "/k", "--pack", "/p", "--chrome", "/c"]):
                with redirect_stderr(io.StringIO()):
                    self.assertEqual(web_smoke.main(argv), 1, argv)


class Commands(unittest.TestCase):
    def test_both_checks_with_the_options(self):
        cmds = dict(web_smoke.smoke_commands("/k", "/c", "http://h/index.html",
                                             Options(tap="begin", report="/r.json")))
        slow, live = cmds["slow-3g-splash"], cmds["boot"]
        self.assertIn("--slow-3g-splash", slow)
        self.assertEqual(slow[slow.index("--splash-budget-ms") + 1], "1000.0")
        self.assertIn("--live", live)
        self.assertEqual(live[live.index("--tap") + 1], "begin")
        self.assertEqual(live[live.index("--ignore-console") + 1], "blocked by CORS policy")
        self.assertEqual(live[live.index("--viewport") + 1], "390x844,touch")
        self.assertEqual(live[-1], "http://h/index.html")

    def test_slow_3g_can_be_switched_off(self):
        names = [n for n, _ in web_smoke.smoke_commands("/k", "/c", "u", Options(slow_3g_splash=False))]
        self.assertEqual(names, ["boot"])


class Fetch(unittest.TestCase):
    def test_no_token_is_refused(self):
        with mock.patch.dict(os.environ, {"KEEL_TOKEN": ""}):
            with self.assertRaises(web_smoke.Refused):
                web_smoke.fetch_keel_scripts(SHA, tempfile.mkdtemp())

    def test_token_goes_through_the_environment_and_is_scrubbed_from_errors(self):
        seen = []

        def fake_run(cmd, **kw):
            seen.append((cmd, kw.get("env", {})))
            return mock.Mock(returncode=1, stdout="", stderr="fatal: bad https://x-access-token:SECRET@github.com/x")

        with mock.patch.dict(os.environ, {"KEEL_TOKEN": "SECRET"}), mock.patch.object(web_smoke, "run", fake_run):
            with tempfile.TemporaryDirectory() as d:
                with self.assertRaises(web_smoke.Refused) as e:
                    web_smoke.fetch_keel_scripts(SHA, d)
        self.assertNotIn("SECRET", str(e.exception))
        self.assertTrue(all("SECRET" not in " ".join(cmd) for cmd, _ in seen))
        self.assertIn("SECRET", seen[0][1]["GIT_CONFIG_KEY_0"])


class RunAgainstAStandIn(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name)
        self.keel = make_keel(self.base)
        self.log = self.base / "calls.log"
        self.env = mock.patch.dict(os.environ, {"STUB_LOG": str(self.log), "STUB_FLAGS": "--slow-3g-splash --live",
                                                "GITHUB_STEP_SUMMARY": str(self.base / "summary.md")})
        self.env.start()
        os.environ.pop("RUNNER_ENVIRONMENT", None)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def run_it(self, body, **kw):
        pack = make_pack(self.base, body)
        out = io.StringIO()
        with redirect_stdout(out):
            rc = web_smoke.cmd_run(Options(keel=str(self.keel), pack=str(pack), **kw))
        return rc, out.getvalue()

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_a_title_that_boots_passes_and_both_checks_ran(self):
        rc, out = self.run_it("<html>splash</html>")
        self.assertEqual(rc, 0, out)
        modes = [c.split()[0] for c in self.calls()]
        self.assertEqual(modes, ["slow-3g-splash", "live"])
        summary = (self.base / "summary.md").read_text()
        self.assertIn("| slow-3g-splash | pass |", summary)
        self.assertIn("| boot | pass |", summary)

    def test_a_black_boot_fails_and_says_which_check(self):
        rc, out = self.run_it("<html>BLACK</html>")
        self.assertEqual(rc, 1)
        self.assertEqual([c.split()[0] for c in self.calls()], ["slow-3g-splash", "live"])  # no short circuit
        summary = (self.base / "summary.md").read_text()
        self.assertIn("| slow-3g-splash | FAIL |", summary)
        self.assertIn("| boot | FAIL |", summary)
        self.assertIn("FAIL: nothing painted on Slow 3G", summary)
        self.assertIn("::error title=web-smoke slow-3g-splash::", out)

    def test_the_server_is_stopped_afterwards(self):
        started = []
        real = web_smoke.start_host

        def spy(*a, **k):
            proc = real(*a, **k)
            started.append(proc)
            return proc

        with mock.patch.object(web_smoke, "start_host", spy):
            self.run_it("<html>ok</html>")
        self.assertIsNotNone(started[0].poll())

    def test_a_directory_without_a_page_is_refused(self):
        (self.base / "dist").mkdir()
        with self.assertRaises(web_smoke.Refused) as e:
            web_smoke.cmd_run(Options(keel=str(self.keel), pack=str(self.base / "dist")))
        self.assertIn("index.html", str(e.exception))

    def test_keel_scripts_older_than_the_standard_are_refused_not_skipped(self):
        os.environ["STUB_FLAGS"] = "--live"
        with self.assertRaises(web_smoke.Refused) as e:
            self.run_it("<html>ok</html>")
        self.assertIn("--slow-3g-splash", str(e.exception))
        self.assertEqual(self.calls(), [])

    def test_slow_3g_off_runs_only_the_boot_check(self):
        os.environ["STUB_FLAGS"] = "--live"
        rc, _ = self.run_it("<html>ok</html>", slow_3g_splash=False)
        self.assertEqual(rc, 0)
        self.assertEqual([c.split()[0] for c in self.calls()], ["live"])

    def test_a_hung_check_fails_instead_of_hanging(self):
        (self.keel / "scripts" / "browser_smoke.py").write_text(
            "import sys, time\nif '--help' in sys.argv:\n    print('--slow-3g-splash'); sys.exit(0)\ntime.sleep(30)\n")
        rc, out = self.run_it("<html>ok</html>", timeout_s=1)
        self.assertEqual(rc, 1)
        self.assertIn("did not finish", out)


try:
    import brotli
except ImportError:  # the check installs it on a runner; the tests need it here
    brotli = None


def wasm_bytes() -> bytes:
    """Compressible but not trivial, so q11 and a lower quality differ clearly."""
    import random
    rnd = random.Random(7)
    words = [bytes(rnd.randrange(256) for _ in range(rnd.randrange(3, 12))) for _ in range(400)]
    return b"\0asm" + b"".join(rnd.choice(words) for _ in range(40000))


@unittest.skipIf(brotli is None, "needs the brotli module")
class ServedQ11(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pack = pathlib.Path(self.tmp.name)
        self.wasm = wasm_bytes()
        (self.pack / "title_bg.wasm").write_bytes(self.wasm)

    def tearDown(self):
        self.tmp.cleanup()

    def precompress(self, quality):
        (self.pack / "title_bg.wasm.br").write_bytes(brotli.compress(self.wasm, quality=quality, lgwin=24))

    def test_a_q11_copy_passes(self):
        self.precompress(11)
        ok, out = web_smoke.served_q11(self.pack, 1.03, True)
        self.assertTrue(ok, out)
        self.assertIn("served/q11 1.000", out)

    def test_a_weaker_copy_fails_over_the_bound(self):
        self.precompress(4)
        ok, out = web_smoke.served_q11(self.pack, 1.03, False)
        self.assertFalse(ok)
        self.assertIn("FAIL title_bg.wasm: served/q11", out)

    def test_a_copy_that_does_not_decode_to_the_module_fails(self):
        (self.pack / "title_bg.wasm.br").write_bytes(brotli.compress(b"something else", quality=11))
        ok, out = web_smoke.served_q11(self.pack, 1.03, False)
        self.assertFalse(ok)
        self.assertIn("does not decode", out)

    def test_no_copy_is_a_note_unless_precompressed_is_required(self):
        ok, out = web_smoke.served_q11(self.pack, 1.03, False)
        self.assertTrue(ok)
        self.assertIn("note title_bg.wasm", out)
        ok, out = web_smoke.served_q11(self.pack, 1.03, True)
        self.assertFalse(ok)
        self.assertIn("compresses it on the fly", out)

    def test_assets_and_a_pack_without_wasm_are_left_alone(self):
        (self.pack / "title_bg.wasm").unlink()
        (self.pack / "assets").mkdir()
        (self.pack / "assets" / "model.wasm").write_bytes(self.wasm)
        ok, out = web_smoke.served_q11(self.pack, 1.03, True)
        self.assertTrue(ok)
        self.assertIn("no wasm module", out)


@unittest.skipIf(brotli is None, "needs the brotli module")
class ServedQ11InTheRun(RunAgainstAStandIn):
    def run_with(self, quality, **kw):
        pack = make_pack(self.base, "<html>splash</html>")
        wasm = wasm_bytes()
        (pack / "title_bg.wasm").write_bytes(wasm)
        if quality:
            (pack / "title_bg.wasm.br").write_bytes(brotli.compress(wasm, quality=quality, lgwin=24))
        out = io.StringIO()
        with redirect_stdout(out):
            rc = web_smoke.cmd_run(Options(keel=str(self.keel), pack=str(pack), **kw))
        return rc, out.getvalue()

    def test_a_precompressed_q11_title_passes_and_the_row_is_in_the_summary(self):
        rc, out = self.run_with(11, require_precompressed=True)
        self.assertEqual(rc, 0, out)
        self.assertIn("| served-q11 | pass |", (self.base / "summary.md").read_text())

    def test_an_on_the_fly_title_fails_when_precompressed_is_required(self):
        rc, out = self.run_with(None, require_precompressed=True)
        self.assertEqual(rc, 1)
        self.assertIn("::error title=web-smoke served-q11::", out)
        self.assertEqual([c.split()[0] for c in self.calls()], ["slow-3g-splash", "live"])  # the browser checks still ran

    def test_the_default_does_not_fail_a_pack_with_no_copy(self):
        rc, out = self.run_with(None)
        self.assertEqual(rc, 0, out)


if __name__ == "__main__":
    unittest.main()
