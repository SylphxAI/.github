"""Exercise the composite action's cache paths with the runner's shell."""

import os
from pathlib import Path
import subprocess

import pytest
import yaml


ACTION = Path(__file__).resolve().parents[1] / ".github/actions/cache-toolchain/action.yml"


def resolve(tmp_path, toolchain, cargo_home):
    steps = yaml.safe_load(ACTION.read_text())["runs"]["steps"]
    spec = next(step for step in steps if step.get("id") == "spec")
    output = tmp_path / "step-output"
    home = tmp_path / "home"
    env = dict(os.environ, HOME=str(home), GITHUB_OUTPUT=str(output), TOOLCHAIN=toolchain, WD="crates")
    env.pop("CARGO_HOME", None)
    if cargo_home is not None:
        env["CARGO_HOME"] = cargo_home
    result = subprocess.run(["bash", "-c", spec["run"]], env=env, capture_output=True, text=True)
    return result, output.read_text() if output.exists() else ""


@pytest.mark.parametrize("custom_home", [None, "", "scratch volume/cargo"])
def test_cargo_caches_only_dependency_paths_in_effective_home(tmp_path, custom_home):
    cargo_home = str(tmp_path / custom_home) if custom_home else custom_home
    expected = Path(cargo_home) if cargo_home else tmp_path / "home/.cargo"
    result, output = resolve(tmp_path, "cargo", cargo_home)
    assert result.returncode == 0, result.stderr
    assert output.splitlines() == [
        "paths<<EOF",
        str(expected / "registry/index"),
        str(expected / "registry/cache"),
        str(expected / "git/db"),
        "EOF",
        "hash<<EOF",
        "crates/**/Cargo.lock",
        "EOF",
    ]


@pytest.mark.parametrize("separator", ["\n", "\r"])
def test_cargo_rejects_paths_that_cannot_be_encoded_as_a_cache_path(tmp_path, separator):
    result, output = resolve(tmp_path, "cargo", f"scratch{separator}other")
    assert result.returncode != 0
    assert "CARGO_HOME must be a single line" in result.stderr
    assert output == ""


def test_other_toolchains_ignore_cargo_home(tmp_path):
    result, output = resolve(tmp_path, "bun", str(tmp_path / "scratch"))
    assert result.returncode == 0, result.stderr
    assert output == "paths<<EOF\n~/.bun/install/cache\nEOF\nhash<<EOF\ncrates/**/bun.lock*\nEOF\n"
