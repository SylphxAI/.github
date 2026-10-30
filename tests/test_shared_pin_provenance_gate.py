"""Published caller entrypoint with complete local source history and failing acquisition."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / ".github/actions/stack-conformance"
TARGET = ".github/actions/example/action.yaml"


class CallerGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.caller = self.root / "caller"
        for repo in (self.source, self.caller):
            repo.mkdir()
            self.git(repo, "init", "-q", "-b", "main")
            self.git(repo, "config", "user.name", "Test")
            self.git(repo, "config", "user.email", "test@example.invalid")
        self.good = self.commit(self.source, {TARGET: "name: Example\n"})
        self.git(self.source, "update-ref", "refs/remotes/origin/main", self.good)
        self.git(self.source, "checkout", "-qb", "feature")
        self.feature = self.commit(self.source, {"new.txt": "unmerged"})
        self.git(self.source, "checkout", "-q", "main")

    def git(self, repo, *args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True, stderr=subprocess.PIPE).strip()

    def commit(self, repo, files):
        for name, text in files.items():
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        self.git(repo, "add", ".")
        self.git(repo, "commit", "-qm", "Fixture")
        return self.git(repo, "rev-parse", "HEAD")

    def check(self, ref=None, path=".github/actions/example", marker=" # main", repository="example/private", offline=True):
        ref = self.good if ref is None else ref
        self.commit(self.caller, {".github/workflows/ci.yml":
                    f"jobs:\n  gate:\n    steps:\n      - uses: SylphxAI/.github/{path}@{ref}{marker}\n"})
        command = ["python3", str(ACTION / "stack_conformance.py"), "check", "--repo", str(self.caller),
                   "--repository", repository, "--report", str(self.root / "report.json")]
        if offline:
            command += ["--shared-source", str(self.source)]
        fake = self.root / "bin"
        fake.mkdir(exist_ok=True)
        gh = fake / "gh"
        gh.write_text("#!/bin/sh\necho 'source token refused' >&2\nexit 1\n")
        gh.chmod(0o755)
        result = subprocess.run(command, capture_output=True, text=True,
                                env={**os.environ, "PATH": str(fake) + os.pathsep + os.environ["PATH"]})
        return result, json.loads((self.root / "report.json").read_text())

    def test_private_caller_offline_reports_exact_anchor_and_passes_yaml_target(self):
        result, report = self.check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(report["shared_pin_provenance"]["head"], self.good)
        self.assertEqual(report["shared_pin_provenance"]["mode"], "offline origin/main snapshot")
        self.assertIn(self.good, result.stdout)

    def test_feature_sha_with_existing_target_fails_at_real_entrypoint(self):
        result, _ = self.check(self.feature)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not on", result.stdout)

    def test_missing_target_at_reachable_sha_fails_at_real_entrypoint(self):
        result, _ = self.check(path=".github/actions/missing")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("target is absent", result.stdout)

    def wrapper(self, text, extra=None):
        files = {".github/workflows/wrapper.yml": text, **(extra or {})}
        commit = self.commit(self.source, files)
        self.git(self.source, "update-ref", "refs/remotes/origin/main", commit)
        return commit

    def test_reachable_workflow_with_off_main_nested_action_fails(self):
        ref = self.wrapper(f"steps:\n  - uses: SylphxAI/.github/.github/actions/example@{self.feature}\n")
        result, report = self.check(ref, path=".github/workflows/wrapper.yml")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("dependency .github/workflows/wrapper.yml", result.stdout)
        self.assertIn("not on", result.stdout)
        self.assertTrue(report["shared_pin_provenance"]["offenders"][0]["dependency_chain"])

    def test_reachable_workflow_with_missing_nested_action_fails(self):
        ref = self.wrapper(f"steps:\n  - uses: SylphxAI/.github/.github/actions/absent@{self.good}\n")
        result, _ = self.check(ref, path=".github/workflows/wrapper.yml")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("target is absent", result.stdout)

    def test_valid_nested_pin_passes_without_source_version_comment(self):
        ref = self.wrapper(f"steps:\n  - uses: SylphxAI/.github/.github/actions/example@{self.good}\n")
        result, _ = self.check(ref, path=".github/workflows/wrapper.yml")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_same_commit_local_reusable_workflow_is_followed(self):
        ref = self.wrapper("jobs:\n  nested:\n    uses: ./.github/workflows/internal.yml\n", {
            ".github/workflows/internal.yml":
                f"steps:\n  - uses: SylphxAI/.github/.github/actions/example@{self.feature}\n"})
        result, _ = self.check(ref, path=".github/workflows/wrapper.yml")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("dependency .github/workflows/internal.yml", result.stdout)

    def test_self_repository_action_is_followed(self):
        ref = self.wrapper("steps:\n  - uses: $/.github/actions/internal\n", {
            ".github/actions/internal/action.yml":
                f"steps:\n  - uses: SylphxAI/.github/.github/actions/example@{self.feature}\n"})
        result, _ = self.check(ref, path=".github/workflows/wrapper.yml")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("dependency .github/actions/internal/action.yml", result.stdout)

    def test_workspace_action_is_not_read_from_remote_source(self):
        ref = self.wrapper("steps:\n  - uses: ./.github/actions/caller-owned\n")
        result, _ = self.check(ref, path=".github/workflows/wrapper.yml")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_repeated_source_dependencies_terminate(self):
        ref = self.wrapper("steps:\n  - uses: $/.github/actions/internal\n", {
            ".github/actions/internal/action.yml": "steps:\n  - uses: $/.github/actions/internal\n"})
        result, _ = self.check(ref, path=".github/workflows/wrapper.yml")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_source_comment_exemption_does_not_exempt_target(self):
        result, _ = self.check(path=".github/actions/missing", marker="", repository="SylphxAI/.github")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("target is absent", result.stdout)
        self.assertNotIn("missing # main", result.stdout)

    def test_source_comment_exemption_does_not_exempt_ancestry(self):
        result, _ = self.check(self.feature, marker="", repository="SylphxAI/.github")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not on", result.stdout)

    def test_source_main_resolves_anchor_but_still_requires_target(self):
        result, report = self.check("main", marker="", repository="SylphxAI/.github")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(report["shared_pin_provenance"]["head"], self.good)
        result, _ = self.check("main", path=".github/actions/absent", marker="", repository="SylphxAI/.github")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("target is absent", result.stdout)

    def test_live_acquisition_failure_is_recorded_and_fails(self):
        result, report = self.check(offline=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("source token refused", result.stdout)
        self.assertFalse(report["passed"])
        self.assertIn("error", report["shared_pin_provenance"])

    def test_offline_missing_origin_main_is_not_a_fallback_to_head(self):
        self.git(self.source, "update-ref", "-d", "refs/remotes/origin/main")
        result, report = self.check()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(report["passed"])
        self.assertIsNone(report["shared_pin_provenance"]["head"])

    def test_action_missing_token_fails_before_property_or_source_reads(self):
        document = yaml.safe_load((ACTION / "action.yml").read_text())
        script = document["runs"]["steps"][0]["run"]
        result = subprocess.run(["bash", "-e", "-c", script], capture_output=True, text=True,
                                env={**os.environ, "GH_TOKEN": ""})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("read-only GitHub token is unavailable", result.stderr)


if __name__ == "__main__":
    unittest.main()
