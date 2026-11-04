import base64
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import textwrap
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / ".github" / "actions" / "git-app-credentials" / "action.yml"
TOKEN = "fake_registry_test_not_a_credential"


def _registry_script():
    text = ACTION.read_text()
    step = text.split("    - name: Configure Cargo registry credentials\n", 1)[1]
    step = step.split("\n    - name:", 1)[0]
    return textwrap.dedent(step.split("      run: |\n", 1)[1])


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
    stale_file = tmp_path / "stale-credentials"
    stale_file.write_text("https://x-access-token:stale-test-token@github.com\n")
    stale_file.chmod(0o600)
    for key, value in [
        ("credential.helper", f"store --file={stale_file}"),
        ("safe.directory", "/tmp/example-safe"),
        ("url.https://stale.invalid/.insteadOf", "https://github.com/"),
    ]:
        subprocess.run(["git", "config", "--global", key, value], env=env, check=True)

    subprocess.run(["bash", "-c", step["run"]], env=env, check=True, capture_output=True, text=True)

    credential_file = runner_temp / "git-app-credentials"
    assert stat.S_IMODE(credential_file.stat().st_mode) == 0o600
    assert credential_file.read_text() == "https://x-access-token:fresh-test-token@github.com\n"

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
    assert exported["GIT_CONFIG_NOSYSTEM"] == "1"
    assert Path(exported["GIT_CONFIG_GLOBAL"]).parent == runner_temp
    isolated_global = subprocess.run(
        ["git", "config", "--global", "--list"],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert isolated_global == "safe.directory=/tmp/example-safe\n"
    original_global = (home / ".gitconfig").read_text()
    assert "stale.invalid" in original_global
    assert "fresh-test-token" not in original_global


class RegistryCredentials(unittest.TestCase):
    def run_action(self, overrides=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "bin").mkdir()
        (root / "runner").mkdir()
        config = root / "old-global"
        config.write_text(
            "[safe]\n\tdirectory = /checkout/safe\n[credential]\n\thelper = old-helper\n[url \"https://old-token@github.com/\"]\n\tinsteadOf = https://github.com/\n"
        )
        fake = root / "bin/git"
        fake.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json,os,subprocess,sys
                if sys.argv[1]=='ls-remote':
                    fields={k:v for k,v in os.environ.items() if k.startswith(('GIT_CONFIG_','CARGO_REGISTRIES_','CARGO_NET_'))}
                    fields['argv']=sys.argv[1:]
                    open(os.environ['PROBE'],'w').write(json.dumps(fields))
                    if os.environ.get('FAIL_PROBE')=='1':sys.exit(1)
                    print('a'*40+'\\tHEAD')
                else:
                    sys.exit(subprocess.run(['/usr/bin/git',*sys.argv[1:]]).returncode)
                """
            )
        )
        fake.chmod(0o755)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "CARGO_"))}
        env.update(
            PATH=str(root / "bin") + ":" + os.environ["PATH"],
            APP_TOKEN=TOKEN,
            REGISTRY="cubeage",
            REGISTRY_OWNER="Cubeage",
            REGISTRY_REPOSITORY="cargo-index",
            RUNNER_TEMP=str(root / "runner"),
            GITHUB_ENV=str(root / "github-env"),
            GIT_CONFIG_GLOBAL=str(config),
            GIT_CONFIG_COUNT="0",
            PROBE=str(root / "probe"),
        )
        env.update(overrides or {})
        result = subprocess.run(["bash", "-c", _registry_script()], env=env, text=True, capture_output=True)
        entries = {}
        if (root / "github-env").exists():
            entries = dict(line.split("=", 1) for line in (root / "github-env").read_text().splitlines())
        probe = json.loads((root / "probe").read_text()) if (root / "probe").exists() else None
        return root, result, entries, probe

    def test_exact_git_and_http_scopes_no_credential_file(self):
        root, result, env, probe = self.run_action()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(probe["argv"], ["ls-remote", "https://github.com/Cubeage/cargo-index.git", "HEAD"])
        expected = "AUTHORIZATION: Basic " + base64.b64encode(("x-access-token:" + TOKEN).encode()).decode()
        self.assertEqual(env["GIT_CONFIG_VALUE_1"], expected)
        self.assertEqual(env["GIT_CONFIG_KEY_1"], "http.https://github.com/Cubeage/cargo-index.git.extraheader")
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "credential.helper")
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], "")
        self.assertEqual(env["CARGO_REGISTRIES_CUBEAGE_TOKEN"], "Bearer " + TOKEN)
        self.assertEqual(probe["CARGO_REGISTRIES_CUBEAGE_TOKEN"], "Bearer " + TOKEN)
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        config = Path(env["GIT_CONFIG_GLOBAL"])
        self.assertIn("/checkout/safe", config.read_text())
        self.assertNotIn("old-helper", config.read_text())
        self.assertNotIn("old-token", config.read_text())
        self.assertNotIn(TOKEN, config.read_text())
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)
        self.assertEqual([p.name for p in (root / "runner").iterdir()], ["cargo-registry-gitconfig"])
        unmasked = "\n".join(line for line in result.stdout.splitlines() if not line.startswith("::add-mask::"))
        self.assertNotIn(TOKEN, unmasked + result.stderr)
        self.assertNotIn(expected, unmasked + result.stderr)

    def test_existing_offset_is_preserved(self):
        _, result, env, probe = self.run_action(
            {
                "GIT_CONFIG_COUNT": "2",
                "GIT_CONFIG_KEY_0": "protocol.version",
                "GIT_CONFIG_VALUE_0": "2",
                "GIT_CONFIG_KEY_1": "http.version",
                "GIT_CONFIG_VALUE_1": "HTTP/1.1",
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(env["GIT_CONFIG_COUNT"], "4")
        self.assertEqual(env["GIT_CONFIG_KEY_2"], "credential.helper")
        self.assertEqual(probe["GIT_CONFIG_KEY_0"], "protocol.version")
        self.assertNotIn("GIT_CONFIG_KEY_0", env)

    def test_missing_token_stops_before_probe(self):
        root, result, env, probe = self.run_action({"APP_TOKEN": ""})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(env, {})
        self.assertIsNone(probe)
        self.assertEqual(list((root / "runner").iterdir()), [])

    def test_invalid_inputs_stop_before_probe(self):
        cases = [
            ("REGISTRY", "cubeage\nevil"),
            ("REGISTRY", ""),
            ("REGISTRY_OWNER", "Cubeage/evil"),
            ("REGISTRY_REPOSITORY", "cargo-index,cubeage-kit"),
            ("REGISTRY_REPOSITORY", "cargo-index\ncubeage-kit"),
            ("GIT_CONFIG_COUNT", "2; echo bad"),
        ]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                root, result, env, probe = self.run_action({key: value})
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(env, {})
                self.assertIsNone(probe)
                self.assertEqual(list((root / "runner").iterdir()), [])

    def test_failed_probe_cannot_claim_success(self):
        _, result, _, probe = self.run_action({"FAIL_PROBE": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNotNone(probe)

    def test_source_path_remains_opt_out(self):
        text = ACTION.read_text()
        self.assertIn("if: ${{ inputs.registry == '' }}", text)
        self.assertIn("if: ${{ inputs.registry != '' }}", text)
        source = text.split("    - name: Configure job-scoped git credentials\n", 1)[1]
        self.assertIn("credentials_file=${cred_file}", source)
        self.assertIn("permission-contents: read", text)
        self.assertIn("value: ${{ steps.app-token.outputs.token }}", text)

