"""Execute the action's credential setup with and without earlier job config."""

import os
from pathlib import Path
import subprocess

import pytest
import yaml


ACTION = Path(__file__).resolve().parents[1] / ".github/actions/keel-repin/action.yml"


def credential_script():
    action = yaml.safe_load(ACTION.read_text())
    return next(step["run"] for step in action["runs"]["steps"]
                if step.get("name") == "Point git at the read tokens")


def job_env(tmp_path):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("GIT_CONFIG_")}
    env.update(
        GITHUB_ENV=str(tmp_path / "github-env"),
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        KEEL_TOKEN="fake-keel-token",
        LAYER_TOKEN="fake-layer-token",
        EXTRA_TOKEN="fake-extra-token",
        EXTRA_OWNER="example",
        EXTRA_REPOS="first, second\nthird",
    )
    return env


@pytest.mark.parametrize("prior_count", [None, 0, 3])
@pytest.mark.parametrize("layer", ["keel", "kit", "engine"])
def test_read_tokens_append_without_changing_existing_config(tmp_path, prior_count, layer):
    env = job_env(tmp_path)
    env["LAYER"] = layer
    prior = [
        ("credential.helper", ""),
        ("http.https://github.com/example/registry.git.extraheader", "test-registry-header"),
        ("test.preserved", "caller value"),
    ] if prior_count else []
    if prior_count is not None:
        env["GIT_CONFIG_COUNT"] = str(prior_count)
    for index, (key, value) in enumerate(prior):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value

    subprocess.run(["bash", "-e", "-o", "pipefail", "-c", credential_script()],
                   env=env, check=True, capture_output=True, text=True)
    exported = dict(line.split("=", 1) for line in Path(env["GITHUB_ENV"]).read_text().splitlines())
    # Earlier indices must not even be rewritten to GITHUB_ENV.
    for index in range(len(prior)):
        assert f"GIT_CONFIG_KEY_{index}" not in exported
        assert f"GIT_CONFIG_VALUE_{index}" not in exported
    env.update(exported)

    expected = prior + [
        ("url.https://x-access-token:fake-keel-token@github.com/SylphxAI/keel.insteadOf",
         "https://github.com/SylphxAI/keel"),
        ("url.https://x-access-token:fake-keel-token@github.com/SylphxAI/keel.insteadOf",
         "ssh://git@github.com/SylphxAI/keel"),
    ]
    if layer != "keel":
        repo = "cubeage-kit" if layer == "kit" else "tycoon-engine"
        expected.append((f"url.https://x-access-token:fake-layer-token@github.com/Cubeage/{repo}.insteadOf",
                         f"https://github.com/Cubeage/{repo}"))
    expected.extend((f"url.https://x-access-token:fake-extra-token@github.com/example/{repo}.insteadOf",
                     f"https://github.com/example/{repo}") for repo in ["first", "second", "third"])
    assert env["GIT_CONFIG_COUNT"] == str(len(expected))
    assert [(env[f"GIT_CONFIG_KEY_{index}"], env[f"GIT_CONFIG_VALUE_{index}"])
            for index in range(len(expected))] == expected
    # Git itself sees the full configuration, including the earlier registry header.
    result = subprocess.run(["git", "config", "--null", "--list"], cwd=tmp_path,
                            env=env, check=True, capture_output=True, text=True)
    effective = result.stdout.split("\0")[:-1]
    assert effective[:len(expected)] == [f"{key.replace('.insteadOf', '.insteadof')}\n{value}"
                                        for key, value in expected]
    assert exported["CARGO_NET_GIT_FETCH_WITH_CLI"] == "true"
    assert exported["GIT_TERMINAL_PROMPT"] == "0"


@pytest.mark.parametrize("count", ["-1", "invalid", "1+2"])
def test_invalid_existing_count_is_rejected_before_writing(tmp_path, count):
    env = job_env(tmp_path)
    env.update(GIT_CONFIG_COUNT=count, LAYER="keel")
    result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", credential_script()],
                            env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert "invalid existing Git configuration count" in result.stderr
    assert not Path(env["GITHUB_ENV"]).exists()
