import os
from pathlib import Path
import stat
import subprocess

import yaml


ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / ".github" / "actions" / "git-app-credentials" / "action.yml"


def test_job_credential_wins_over_inherited_helpers_and_preserves_command_config(tmp_path):
    action = yaml.safe_load(ACTION.read_text())
    step = next(
        step
        for step in action["runs"]["steps"]
        if step.get("name") == "Configure job-scoped git credentials"
    )
    runner_temp = tmp_path / "runner"
    home = tmp_path / "home"
    runner_temp.mkdir()
    home.mkdir()
    github_env = tmp_path / "github-env"
    github_output = tmp_path / "github-output"
    env = os.environ.copy()
    env.update(
        {
            "APP_TOKEN": "fresh-test-token",
            "RUNNER_TEMP": str(runner_temp),
            "HOME": str(home),
            "GITHUB_ENV": str(github_env),
            "GITHUB_OUTPUT": str(github_output),
            "GITHUB_REPOSITORY": "SylphxAI/example",
            # A caller's existing command-scoped setting must survive the action.
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.sslVerify",
            "GIT_CONFIG_VALUE_0": "false",
        }
    )
    subprocess.run(["bash", "-c", step["run"]], env=env, check=True, capture_output=True, text=True)

    credential_file = runner_temp / "git-app-credentials"
    assert stat.S_IMODE(credential_file.stat().st_mode) == 0o600
    assert credential_file.read_text() == "https://x-access-token:fresh-test-token@github.com\n"

    stale_file = tmp_path / "stale-credentials"
    stale_file.write_text("https://x-access-token:stale-test-token@github.com\n")
    stale_file.chmod(0o600)
    subprocess.run(
        ["git", "config", "--global", "credential.helper", f"store --file={stale_file}"],
        env=env,
        check=True,
    )

    exported = dict(line.split("=", 1) for line in github_env.read_text().splitlines())
    env.update(exported)
    answer = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n",
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    fields = dict(line.split("=", 1) for line in answer.splitlines())

    assert fields["password"] == "fresh-test-token"
    assert exported["CARGO_NET_GIT_FETCH_WITH_CLI"] == "true"
    assert exported["GIT_TERMINAL_PROMPT"] == "0"
    assert exported["GIT_CONFIG_COUNT"] == "5"
    assert exported["GIT_CONFIG_KEY_1"] == "credential.helper"
    assert exported["GIT_CONFIG_VALUE_1"] == ""
    assert exported["GIT_CONFIG_KEY_3"] == "credential.https://github.com.helper"
    assert exported["GIT_CONFIG_VALUE_3"] == ""
    assert "insteadOf" not in (home / ".gitconfig").read_text()
    assert "fresh-test-token" not in (home / ".gitconfig").read_text()
