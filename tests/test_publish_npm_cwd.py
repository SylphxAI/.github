"""The release action and shell commands must select the same package root."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
PUBLISHER = ROOT / ".github/actions/setup-changesets-publisher/changesets-publish.mjs"


class PublishCwdTest(unittest.TestCase):
    def test_changesets_receives_the_shell_working_directory(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/publish-npm.yml").read_text())
        steps = workflow["jobs"]["publish"]["steps"]
        action = next(s for s in steps if s.get("uses", "").startswith("changesets/action@"))
        self.assertEqual(action["with"]["cwd"], "${{ inputs.working-directory }}")
        self.assertTrue(any(s.get("working-directory") == action["with"]["cwd"] for s in steps))

    def test_publisher_scope_is_the_selected_root_not_its_parent_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(json.dumps({"private": True, "workspaces": ["packages/*"]}))
            for name in ("selected", "sibling"):
                folder = root / "packages" / name
                folder.mkdir(parents=True)
                (folder / "package.json").write_text(json.dumps({"name": name, "version": "1.0.0"}))
            # All versions/tags already exist: reject any unexpected external command.
            fakebin = root / "bin"
            fakebin.mkdir()
            for name, condition in (("npm", '[ "$1" = view ]'), ("git", '[ "$1" = ls-remote ]')):
                script = fakebin / name
                script.write_text("#!/bin/sh\n" + condition + " || exit 99\nexit 0\n")
                script.chmod(0o755)
            env = {**os.environ, "PATH": str(fakebin) + os.pathsep + os.environ["PATH"]}
            for cwd, expected in ((root, "selected@1.0.0 (packages/selected), sibling@1.0.0 (packages/sibling)"),
                                  (root / "packages/selected", "selected@1.0.0 (.)")):
                with self.subTest(cwd=cwd):
                    result = subprocess.run(["node", str(PUBLISHER), "--dry-run"], cwd=cwd,
                                            env=env, text=True, capture_output=True, timeout=30)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    line = next(line for line in result.stdout.splitlines() if "Publishable packages:" in line)
                    self.assertEqual(line, "[sylphx-publish] Publishable packages: " + expected)


if __name__ == "__main__":
    unittest.main()
