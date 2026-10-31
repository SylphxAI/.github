import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / ".github/actions/unity-compile-scripts"
spec = importlib.util.spec_from_file_location("unity_compile_prepare", ACTION / "prepare.py")
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


class UnityCompileScripts(unittest.TestCase):
    def fixture(self, root):
        project = root / "project with spaces"
        (project / "Assets").mkdir(parents=True)
        (project / "ProjectSettings").mkdir()
        (project / "ProjectSettings/ProjectVersion.txt").write_text("m_EditorVersion: 2020.3.49f1\n")
        return project

    def test_prepares_idempotently_without_changing_consumer_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self.fixture(root)
            for _ in range(2):
                plan = prepare.prepare(root, project.name, "Builds/compile", "Android")
                self.assertEqual(plan["method"], "SylphxCompilePlayerScripts.Run")
            self.assertTrue((project / "Assets/Editor/SylphxCompilePlayerScripts.cs").read_text().startswith(prepare.MARKER))
            self.assertEqual((project / "ProjectSettings/ProjectVersion.txt").read_text(), "m_EditorVersion: 2020.3.49f1\n")

    def test_rejects_path_escape_and_unknown_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self.fixture(root)
            for project_name, output, target in (("../outside", "Builds/compile", "Android"), (project.name, "../outside", "Android"), (project.name, "/tmp/output", "Android"), (project.name, "Builds/compile", "WebGL"), (project.name, "Temp/compile", "Android")):
                with self.assertRaises((ValueError, OSError)):
                    prepare.prepare(root, project_name, output, target)

    def test_does_not_overwrite_consumer_editor_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self.fixture(root)
            (project / "Assets/Editor").mkdir()
            entry = project / "Assets/Editor/SylphxCompilePlayerScripts.cs"
            entry.write_text("consumer work")
            with self.assertRaises(ValueError):
                prepare.prepare(root, project.name, "Builds/compile", "Android")
            self.assertEqual(entry.read_text(), "consumer work")

    def test_rejects_linked_editor_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self.fixture(root)
            outside = root / "other project"
            outside.mkdir()
            (project / "Assets/Editor").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                prepare.prepare(root, project.name, "Builds/compile", "Android")
            self.assertEqual(list(outside.iterdir()), [])

    def test_compiler_uses_real_store_target_without_native_build_or_signing(self):
        source = (ACTION / "SylphxCompilePlayerScripts.cs").read_text()
        self.assertIn("PlayerBuildInterface.CompilePlayerScripts", source)
        self.assertIn("ScriptCompilationOptions.None", source)
        self.assertIn("activeBuildTarget != target", source)
        self.assertIn("errors.Count != 0", source)
        self.assertIn("result.assemblies.Count == 0", source)
        for forbidden in ("BuildPipeline.BuildPlayer", "keystore", "BuildOptions.Development", "upload", "InstallApp"):
            self.assertNotIn(forbidden, source)

    def test_shell_propagates_compiler_failure_and_refuses_stale_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self.fixture(root)
            license_home = root / "license"
            license_home.mkdir()
            output = project / "Builds/compile"
            output.mkdir(parents=True)
            receipt = output / "compile-receipt.json"
            receipt.write_text('{"target":"Android","assemblies":[{}]}')
            fake = root / "bin"
            fake.mkdir()
            (fake / "docker").write_text("#!/bin/sh\nexit 42\n")
            (fake / "docker").chmod(0o755)
            env = os.environ | {"PATH": str(fake) + os.pathsep + os.environ["PATH"], "GITHUB_WORKSPACE": str(root), "GITHUB_ACTION_PATH": str(ACTION), "PROJECT_PATH": project.name, "COMPILE_OUTPUT": "Builds/compile", "COMPILE_TARGET": "Android", "LICENSE_HOME": str(license_home), "UNITY_IMAGE": "consumer/unity:pin", "GITHUB_OUTPUT": str(root / "out")}
            result = subprocess.run(["bash", str(ACTION / "compile.sh")], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 42)
            self.assertFalse(receipt.exists())

    def test_success_uses_absolute_project_output_and_validates_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self.fixture(root)
            license_home = root / "license"
            license_home.mkdir()
            fake = root / "bin"
            fake.mkdir()
            (fake / "docker").write_text("""#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
assert args[args.index('--ulimit') + 1] == 'nofile=65536:65536'
output = next(x.split('=',1)[1] for x in args if x.startswith('SYLPHX_COMPILE_OUTPUT='))
expected = '/workspace/' + os.environ['PROJECT_PATH'] + '/' + os.environ['COMPILE_OUTPUT']
assert output == expected, (output, expected)
target = pathlib.Path(os.environ['GITHUB_WORKSPACE']) / os.environ['PROJECT_PATH'] / os.environ['COMPILE_OUTPUT']
target.mkdir(parents=True, exist_ok=True)
(target / 'compile-receipt.json').write_text(json.dumps({'target':'Android','unityVersion':'2020.3.49f1','assemblies':[{'file':'Assembly-CSharp.dll','sha256':'a'*64}]}))
""")
            (fake / "docker").chmod(0o755)
            env = os.environ | {"PATH": str(fake) + os.pathsep + os.environ["PATH"], "GITHUB_WORKSPACE": str(root), "GITHUB_ACTION_PATH": str(ACTION), "PROJECT_PATH": project.name, "COMPILE_OUTPUT": "Builds/compile", "COMPILE_TARGET": "Android", "LICENSE_HOME": str(license_home), "UNITY_IMAGE": "consumer/unity:pin", "GITHUB_OUTPUT": str(root / "out")}
            result = subprocess.run(["bash", str(ACTION / "compile.sh")], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Verified player C# receipt: Android", result.stdout)
            self.assertIn(str(project / "Builds/compile/compile-receipt.json"), (root / "out").read_text())


if __name__ == "__main__":
    unittest.main()
