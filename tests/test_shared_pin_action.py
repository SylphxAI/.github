"""Run the installed shared-only action shell against a read-only GitHub fixture."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / ".github/actions/workflow-pin-check"
MAIN = "a" * 40
FEATURE = "b" * 40
PREFIX = "repos/SylphxAI/.github/"


class SharedOnlyAction(unittest.TestCase):
    def test_opt_in_input_reaches_the_shell_and_template(self):
        document = yaml.safe_load((ACTION / "action.yml").read_text())
        self.assertEqual(document["inputs"]["shared-only"]["default"], "false")
        step = next(step for step in document["runs"]["steps"] if step.get("name") == "Check the workflow pins")
        self.assertEqual(step["env"]["SHARED_ONLY"], "${{ inputs.shared-only }}")
        template = yaml.load((ROOT / "workflow-templates/shared-pin-check.yml").read_text(), Loader=yaml.BaseLoader)
        self.assertIn("pull_request", template["on"])
        self.assertIn("merge_group", template["on"])
        self.assertNotIn("paths", template["on"]["pull_request"] or {})
        call = template["jobs"]["workflow-pin-check"]["steps"][-1]
        self.assertEqual(call["with"], {"token": "${{ github.token }}", "shared-only": "true"})

    def run_action(self, use, *, nested=None, other="", unavailable=False):
        sources = {".github/actions/example/action.yml": "name: Example\n",
                   ".github/actions/yaml/action.yaml": "name: Example\n",
                   ".github/workflows/wrapper.yml": nested or "name: Wrapper\n"}
        blobs = {path: hashlib.sha1(path.encode()).hexdigest() for path in sources}
        replies = {
            PREFIX + "commits/main": {"sha": MAIN, "commit": {
                "committer": {"date": "2026-10-30T00:00:00Z"}, "tree": {"sha": MAIN}}},
            PREFIX + f"compare/{MAIN}...{FEATURE}": {"status": "ahead"},
            PREFIX + f"git/trees/{MAIN}?recursive=1": {"truncated": False, "tree": [
                {"path": path, "sha": blobs[path], "type": "blob", "mode": "100644"} for path in sources]},
            **{PREFIX + "git/blobs/" + blobs[path]: {
                "encoding": "base64", "content": base64.b64encode(text.encode()).decode()}
               for path, text in sources.items()},
        }
        if unavailable:
            replies.pop(PREFIX + "commits/main")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = root / ".github/actions/shared-pin-fixture/action.yml"
            fixture.parent.mkdir(parents=True)
            fixture.write_text("name: Fixture\nruns:\n  using: composite\n  steps:\n"
                               f"    - uses: {use}\n" + other)
            answers = root / "responses.json"
            answers.write_text(json.dumps(replies))
            fake = root / "bin"
            fake.mkdir()
            gh = fake / "gh"
            gh.write_text("#!/usr/bin/env python3\nimport json, os, sys\n"
                          "data = json.load(open(os.environ['PIN_FIXTURE_RESPONSES']))\n"
                          "key = sys.argv[2]\n"
                          "if key not in data:\n"
                          "    print('fixture endpoint unavailable: ' + key, file=sys.stderr)\n"
                          "    sys.exit(1)\n"
                          "print(json.dumps(data[key]))\n")
            gh.chmod(0o755)
            document = yaml.safe_load((ACTION / "action.yml").read_text())
            script = next(step["run"] for step in document["runs"]["steps"]
                          if step.get("name") == "Check the workflow pins")
            return subprocess.run(["bash", "--posix", "-euc", script], capture_output=True, text=True,
                                  env={**os.environ, "PATH": str(fake) + os.pathsep + os.environ["PATH"],
                                       "ACTION_PATH": str(ACTION), "GITHUB_WORKSPACE": str(root),
                                       "GITHUB_REPOSITORY": "example/private", "GH_TOKEN": "fixture-token",
                                       "OWNERS": "Cubeage", "SHARED_ONLY": "true",
                                       "PIN_FIXTURE_RESPONSES": str(answers)})

    def test_clean_action_yaml_and_unrelated_private_reference_pass(self):
        result = self.run_action(f"SylphxAI/.github/.github/actions/yaml@{MAIN} # main",
                                 other=f"    - uses: SylphxAI/private/action@{FEATURE}\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(MAIN, result.stdout)

    def test_branch_tag_and_short_sha_fail(self):
        for ref in ("", "main", "feature", "v1", "abc123"):
            with self.subTest(ref=ref):
                result = self.run_action(f"SylphxAI/.github/.github/actions/example@{ref} # main")
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("full 40-hex", result.stdout)

    def test_missing_main_comment_fails(self):
        result = self.run_action(f"SylphxAI/.github/.github/actions/example@{MAIN}")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("missing # main", result.stdout)

    def test_missing_action_on_main_fails(self):
        result = self.run_action(f"SylphxAI/.github/.github/actions/missing@{MAIN} # main")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("target is absent", result.stdout)
        self.assertIn("file=.github/actions/shared-pin-fixture/action.yml,line=5", result.stdout)

    def test_feature_commit_with_existing_action_fails(self):
        result = self.run_action(f"SylphxAI/.github/.github/actions/example@{FEATURE} # main")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("not on", result.stdout)

    def test_nested_feature_pin_fails(self):
        result = self.run_action(f"SylphxAI/.github/.github/workflows/wrapper.yml@{MAIN} # main",
                                 nested=f"steps:\n  - uses: SylphxAI/.github/.github/actions/example@{FEATURE}\n")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("dependency .github/workflows/wrapper.yml", result.stdout)

    def test_nested_missing_action_fails(self):
        result = self.run_action(f"SylphxAI/.github/.github/workflows/wrapper.yml@{MAIN} # main",
                                 nested=f"steps:\n  - uses: SylphxAI/.github/.github/actions/missing@{MAIN}\n")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("target is absent", result.stdout)
        self.assertIn("dependency .github/workflows/wrapper.yml", result.stdout)

    def test_clean_nested_chain_passes(self):
        result = self.run_action(f"SylphxAI/.github/.github/workflows/wrapper.yml@{MAIN} # main",
                                 nested=f"steps:\n  - uses: SylphxAI/.github/.github/actions/example@{MAIN}\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_source_acquisition_failure_fails_closed(self):
        result = self.run_action(f"SylphxAI/.github/.github/actions/example@{MAIN} # main", unavailable=True)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("shared pin proof unavailable", result.stdout)


if __name__ == "__main__":
    unittest.main()
