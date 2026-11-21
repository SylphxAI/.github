"""Exercise the composite action's cache paths with the runner's shell."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


ACTION = Path(__file__).resolve().parents[1] / ".github/actions/cache-toolchain/action.yml"


def resolve(tmp_path, toolchain, cargo_home):
    steps = yaml.safe_load(ACTION.read_text())["runs"]["steps"]
    spec = next(step for step in steps if step.get("id") == "spec")
    output = tmp_path / "step-output"
    output.unlink(missing_ok=True)
    home = tmp_path / "home"
    env = dict(os.environ, HOME=str(home), GITHUB_OUTPUT=str(output), TOOLCHAIN=toolchain, WD="crates")
    env.pop("CARGO_HOME", None)
    if cargo_home is not None:
        env["CARGO_HOME"] = cargo_home
    result = subprocess.run(["bash", "-c", spec["run"]], env=env, capture_output=True, text=True)
    return result, output.read_text() if output.exists() else ""


class CacheToolchainTest(unittest.TestCase):
    def setUp(self):
        self.tmp_path = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_cargo_caches_only_dependency_paths_in_effective_home(self):
        for custom_home in (None, "", "scratch volume/cargo"):
            with self.subTest(custom_home=custom_home):
                cargo_home = str(self.tmp_path / custom_home) if custom_home else custom_home
                expected = Path(cargo_home) if cargo_home else self.tmp_path / "home/.cargo"
                result, output = resolve(self.tmp_path, "cargo", cargo_home)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(output.splitlines(), [
                    "paths<<EOF",
                    str(expected / "registry/index"),
                    str(expected / "registry/cache"),
                    str(expected / "git/db"),
                    "EOF",
                    "hash<<EOF",
                    "crates/**/Cargo.lock",
                    "EOF",
                ])

    def test_cargo_rejects_paths_that_cannot_be_encoded_as_a_cache_path(self):
        for separator in ("\n", "\r"):
            with self.subTest(separator=separator):
                result, output = resolve(self.tmp_path, "cargo", f"scratch{separator}other")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("CARGO_HOME must be a single line", result.stderr)
                self.assertEqual(output, "")

    def test_other_toolchains_ignore_cargo_home(self):
        result, output = resolve(self.tmp_path, "bun", str(self.tmp_path / "scratch"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output, "paths<<EOF\n~/.bun/install/cache\nEOF\nhash<<EOF\ncrates/**/bun.lock*\nEOF\n")
