"""The shared required stack check refuses lost web pack source identity."""

import contextlib
import importlib.util
import io
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "stack_conformance", ROOT / ".github/actions/stack-conformance/stack_conformance.py")
sc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sc)
MANIFEST = '[package]\nname="example-game"\n[dependencies]\nkeel="0.1"\n'
MISSING = "FROM rust AS pack\nRUN bash scripts/web.sh\nFROM nginx\n"
CLEAN = MISSING.replace("RUN bash", "ARG SYLPHX_GIT_COMMIT_SHA\nRUN bash")
WRAPPER = "keel pack --profile web --release\n"


class KeelPackGuardTest(unittest.TestCase):
    def test_non_keel_repository_passes_and_keel_consumer_names_the_remedy(self):
        files = {"Dockerfile.web": MISSING, "scripts/web.sh": WRAPPER}
        self.assertEqual(sc.keel_departures(sc.DictTree(files)), [])
        files["Cargo.toml"] = MANIFEST
        found = sc.keel_departures(sc.DictTree(files))
        self.assertEqual(found, ["keel-pack-source Dockerfile.web:2 pack"])
        self.assertIn("ARG SYLPHX_GIT_COMMIT_SHA", sc.decide_keel(found)[0])
        files["Dockerfile.web"] = CLEAN
        self.assertEqual(sc.keel_departures(sc.DictTree(files)), [])

    def test_required_cli_refuses_deletion_and_local_baseline_then_passes_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)

            def git(*arguments):
                return subprocess.run(["git", "-C", directory, *arguments], check=True,
                                      capture_output=True, text=True).stdout.strip()

            def commit(files):
                for name, text in files.items():
                    path = root / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(text)
                git("add", ".")
                git("commit", "-qm", "fixture")
                return git("rev-parse", "HEAD")

            def check(base, head):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = sc.main(["check", "--repo", directory, "--base", base, "--head", head,
                                    "--repository", "example/game", "--shared-source", str(ROOT),
                                    "--report", str(root / "report.json")])
                return code, output.getvalue()

            git("init", "-q", "-b", "main")
            git("config", "user.name", "Example")
            git("config", "user.email", "example@example.com")
            base = commit({"Cargo.toml": MANIFEST, "Dockerfile.web": CLEAN, "scripts/web.sh": WRAPPER})
            self.assertEqual(check(base, base)[0], 0)
            missing = commit({"Dockerfile.web": MISSING})
            code, output = check(base, missing)
            self.assertEqual(code, 1)
            self.assertIn("keel-pack-source Dockerfile.web:2 pack", output)
            self.assertIn("ARG SYLPHX_GIT_COMMIT_SHA", output)
            recorded = commit({sc.BASELINE: "keel-pack-source Dockerfile.web:2 pack\n"})
            self.assertEqual(check(missing, recorded)[0], 1)
            repaired = commit({"Dockerfile.web": CLEAN, sc.BASELINE: ""})
            self.assertEqual(check(base, repaired)[0], 0)


if __name__ == "__main__":
    unittest.main()
