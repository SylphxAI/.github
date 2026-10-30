"""First-load regressions: requested assets, missing evidence and a shrinking baseline."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import zlib


ACTION = Path(__file__).resolve().parents[1] / ".github/actions/web-smoke"
SPEC = importlib.util.spec_from_file_location("first_load", ACTION / "first_load.py")
first_load = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(first_load)


class FirstLoad(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pack = self.root / "dist"
        self.pack.mkdir()
        self.baseline = self.root / ".github/web-first-load.json"
        self.baseline.parent.mkdir()
        self.report = self.root / "result/report.json"
        self.evidence = self.root / "boot.json"
        for name, content in {"index.html": b"<title>Example</title>", "app.js": b"start();",
                              "app.wasm": b"\0asm\x01\0\0\0", "style.css": b"body{color:red}",
                              "font.woff2": bytes(range(256)), "poster.webp": b"image" * 100,
                              "scene.json": b'{"scene":1}'}.items():
            (self.pack / name).write_bytes(content)
        self.urls = ["http://127.0.0.1:8000/game/" + name for name in
                     ("index.html", "app.js", "app.wasm", "style.css", "font.woff2", "poster.webp", "scene.json")]
        self.write_evidence()

    def write_evidence(self, **extra):
        data = {"ok": True, "url": self.urls[0], "requests": self.urls, "first_frame_s": 0.7}
        data.update(extra)
        self.evidence.write_text(json.dumps(data))

    def size(self):
        return first_load.measure(self.pack, json.loads(self.evidence.read_text()),
                                  lambda data: len(zlib.compress(data)))["brotli_bytes"]

    def check(self, base=""):
        return first_load.check(self.pack, self.evidence, self.baseline, base, self.report,
                                lambda data: len(zlib.compress(data)))

    def git(self, *args):
        env = {**os.environ, "GIT_AUTHOR_NAME": "Example", "GIT_COMMITTER_NAME": "Example",
               "GIT_AUTHOR_EMAIL": "example@example.com", "GIT_COMMITTER_EMAIL": "example@example.com"}
        return subprocess.run(["git", *args], cwd=self.root, env=env, text=True,
                              capture_output=True, check=True).stdout.strip()

    def record_base(self, size):
        self.git("init", "-q")
        self.baseline.write_text(first_load.baseline_text(size))
        self.git("add", ".github/web-first-load.json")
        self.git("commit", "-qm", "Record measured baseline")
        return self.git("rev-parse", "HEAD")

    def test_same_passes_and_all_requested_asset_types_count(self):
        self.baseline.write_text(first_load.baseline_text(self.size()))
        ok, _ = self.check()
        self.assertTrue(ok)
        report = json.loads(self.report.read_text())
        self.assertEqual(7, len(report["files"]))
        self.assertEqual(0.7, report["first_frame_s"])
        self.assertEqual(self.size(), sum(row["brotli_bytes"] for row in report["files"]))

    def test_growth_fails_even_when_the_pr_raises_its_baseline(self):
        base = self.record_base(self.size())
        (self.pack / "app.wasm").write_bytes(bytes(range(256)) * 100)
        self.baseline.write_text(first_load.baseline_text(self.size()))
        ok, text = self.check(base)
        self.assertFalse(ok)
        self.assertIn("baseline grew", text)
        self.assertIn("first load grew", text)

    def test_shrinking_proposes_lower_baseline_and_recording_it_passes(self):
        base = self.record_base(self.size())
        (self.pack / "font.woff2").write_bytes(b"small")
        ok, text = self.check(base)
        self.assertFalse(ok)
        self.assertIn("first load shrank", text)
        candidate = self.report.with_name("web-first-load-baseline.json").read_text()
        self.assertEqual(self.size(), first_load.baseline_bytes(candidate))
        self.baseline.write_text(candidate)
        self.assertTrue(self.check(base)[0])

    def test_unused_asset_does_not_count_until_the_page_fetches_it(self):
        before = self.size()
        (self.pack / "unused.bin").write_bytes(bytes(range(256)) * 1000)
        self.assertEqual(before, self.size())
        self.urls.append("http://127.0.0.1:8000/game/unused.bin")
        self.write_evidence()
        self.assertGreater(self.size(), before)

    def test_missing_or_failed_evidence_cannot_be_a_small_pass(self):
        for extra in ({"requests": []}, {"ok": False}, {"requests": [self.urls[0], self.urls[1] + "/missing"]}):
            with self.subTest(extra=extra):
                self.write_evidence(**extra)
                with self.assertRaises(ValueError):
                    self.size()

    def test_initial_baseline_is_a_measurement_and_cannot_be_deleted(self):
        self.git("init", "-q")
        self.git("commit", "--allow-empty", "-qm", "Before adoption")
        base = self.git("rev-parse", "HEAD")
        ok, _ = self.check(base)
        self.assertFalse(ok)
        self.baseline.write_text(self.report.with_name("web-first-load-baseline.json").read_text())
        self.assertTrue(self.check(base)[0])
        self.baseline.write_text(first_load.baseline_text(self.size() + 1))
        self.assertIn("initial baseline must equal", self.check(base)[1])
        self.baseline.write_text(first_load.baseline_text(self.size()))
        self.git("add", ".github/web-first-load.json")
        self.git("commit", "-qm", "Adopt")
        base = self.git("rev-parse", "HEAD")
        self.baseline.unlink()
        self.assertFalse(self.check(base)[0])

    def test_query_duplicates_and_external_api_responses_are_separate(self):
        before = self.size()
        self.urls += [self.urls[1] + "?v=1", "https://api.example.com/session", "data:text/plain,hello"]
        self.write_evidence()
        self.assertEqual(before, self.size())
        self.baseline.write_text(first_load.baseline_text(before))
        self.check()
        self.assertEqual(["https://api.example.com/session"], json.loads(self.report.read_text())["external_requests"])

    def test_invalid_baselines_and_missing_git_base_fail(self):
        for value in ('{}', '{"version":1,"brotli_bytes":true}',
                      '{"version":1,"brotli_bytes":10,"tolerance":99}'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                first_load.baseline_bytes(value)
        self.record_base(self.size())
        with self.assertRaises(subprocess.CalledProcessError):
            self.check("unavailable-base")

    def test_fixed_tolerance_does_not_raise_the_next_baseline(self):
        self.assertEqual(([], 1000), first_load.judge(1020, 1000, 1000))
        self.assertTrue(first_load.judge(1021, 1000, 1000)[0])


if __name__ == "__main__":
    unittest.main()
