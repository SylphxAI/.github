"""Offline source-root boundaries: mixed repositories, real git and fixture-only CLI/PRs."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location("source_root_fixtures", Path(__file__).with_name("test_keel_repin.py"))
fixtures = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixtures)
repin = fixtures.keel_repin
git = fixtures.git
OLD = fixtures.OLD


class SourceRootTest(fixtures.PublicReleaseFixture):
    def mixed_title(self, base):
        files = {
            "Cargo.toml": 'k={package="sylphx-keel-engine",version="=9.9.9999"}\n',
            "Cargo.lock": '# original lock stays byte-identical\n',
            "deps/keel.rev": "c" * 40 + "\n",
            "src/lib.rs": "// original Rust\n",
            ".github/workflows/ci.yml": f"# manual pin {OLD}\n",
            "keel/Cargo.toml": f'k={{git="https://github.com/SylphxAI/keel",rev="{OLD}"}}\n',
            "keel/Cargo.lock": '# fixture lock\n',
            "keel/deps/keel.rev": OLD + "\n",
            "keel/src/lib.rs": "// remaster Rust\n",
            "keel/app/README": "check here\n",
            "keel/Dockerfile": "FROM rust AS pack\nRUN keel pack --profile web\n",
        }
        return fixtures.make_title(base, files)

    def run_scoped(self, base, repo, remote, *extra):
        report = base / "report.json"
        args = ["run", "--root", str(repo), "--source-root", "keel", "--remote", remote,
                "--tag", fixtures.NEW_TAG, "--check-command", "true", "--report", str(report), *extra]
        with mock.patch.object(repin, "refresh_registry_locks", return_value=None) as locks:
            rc = repin.main(args)
        return rc, json.loads(report.read_text()) if report.exists() else None, locks

    def test_scoped_migration_rewrite_lock_pack_check_status_and_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            remote, sha = fixtures.make_keel_remote(base, 'for f in $(find . -name "*.rs"); do echo "// migrated" >> "$f"; done\n')
            repo = self.mixed_title(base)
            originals = {name: (repo / name).read_bytes() for name in (
                "Cargo.toml", "Cargo.lock", "deps/keel.rev", "src/lib.rs")}
            # Unstaged original changes and build output must not enter the scoped report/commit.
            (repo / ".github/workflows/ci.yml").write_text(f"# person-owned workflow {OLD}\n")
            (repo / "target").mkdir()
            (repo / "target/original-output").write_text("outside\n")
            rc, report, locks = self.run_scoped(base, repo, remote, "--check-dir", "app",
                                               "--check-command", "pwd > cwd.txt")
            self.assertEqual(rc, 0)
            self.assertEqual((report["status"], report["source_root"], report["old"]), ("ok", "keel", [OLD]))
            self.assertTrue(report["changed"])
            self.assertTrue(all(name.startswith("keel/") for name in report["changed"]))
            self.assertEqual(report["manual"], [".github/workflows/ci.yml"])
            self.assertEqual((repo / "keel/app/cwd.txt").read_text().strip(), str(repo / "keel/app"))
            self.assertIn("// migrated", (repo / "keel/src/lib.rs").read_text())
            self.assertEqual((repo / "keel/deps/keel.rev").read_text().strip(), sha)
            self.assertIn("ARG SYLPHX_GIT_COMMIT_SHA", (repo / "keel/Dockerfile").read_text())
            self.assertEqual(locks.call_args.args[0], repo / "keel")
            for name, content in originals.items():
                self.assertEqual((repo / name).read_bytes(), content, name)
            self.assertIn("person-owned", (repo / ".github/workflows/ci.yml").read_text())
            gh = fixtures.fake_gh(base, {"pr create": "https://example.invalid/pull/1\n"})
            with mock.patch.dict(os.environ, {"KEEL_REPIN_GH": str(gh), "GH_TOKEN": "fixture"}):
                self.assertEqual(repin.main(["pr", "--root", str(repo), "--source-root", "keel",
                                             "--report", str(base / "report.json")]), 0)
            committed = git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines()
            self.assertEqual(sorted(committed), report["changed"])
            self.assertIn(".github/workflows/ci.yml", git(repo, "diff", "--name-only"))
            self.assertIn("target/original-output", git(repo, "ls-files", "--others", "--exclude-standard"))

    def test_poll_ignores_original_higher_version_and_pin(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            remote, sha = fixtures.make_keel_remote(base)
            repo = self.mixed_title(base)
            with mock.patch.object(repin, "pr_exists", return_value=False), \
                 mock.patch.object(repin, "compare_status", return_value="ahead") as compare, \
                 mock.patch.object(repin, "write_outputs") as output:
                rc = repin.main(["poll", "--root", str(repo), "--source-root", "keel", "--remote", remote])
            self.assertEqual(rc, 0)
            self.assertEqual(output.call_args.args[0]["needed"], "true")
            self.assertEqual(compare.call_args.args[:2], (OLD, sha))

    def test_invalid_roots_refused_before_tag_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = self.mixed_title(base)
            (repo / "alias").symlink_to(repo / "keel", target_is_directory=True)
            for source_root in ("", "../title/keel", "keel/../src", str(repo / "keel"), "absent", "Cargo.toml", "alias", "alias/app", ".git"):
                for command in ("poll", "run", "pr"):
                    with self.subTest(source_root=source_root, command=command), \
                         mock.patch.object(repin, "resolve_tag") as resolve:
                        args = [command, "--root", str(repo), "--source-root", source_root]
                        if command != "poll":
                            args += ["--report", str(base / "report.json")]
                        self.assertEqual(repin.main(args), 1)
                        resolve.assert_not_called()

    def test_invalid_check_dirs_refused_before_tag_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = self.mixed_title(base)
            (repo / "keel/link").symlink_to(repo, target_is_directory=True)
            for check_dir in ("../src", str(repo), "missing", "link", "link/src"):
                with self.subTest(check_dir=check_dir), mock.patch.object(repin, "resolve_tag") as resolve:
                    self.assertEqual(repin.main(["run", "--root", str(repo), "--source-root", "keel",
                        "--check-dir", check_dir, "--report", str(base / "report.json")]), 1)
                    resolve.assert_not_called()

    def test_source_file_symlinks_are_not_discovered_or_migrated(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = self.mixed_title(base)
            (repo / "keel/linked").mkdir()
            (repo / "keel/linked/Cargo.toml").symlink_to(repo / "Cargo.toml")
            (repo / "keel/linked/original.rs").symlink_to(repo / "src/lib.rs")
            git(repo, "add", "keel/linked")
            self.assertNotIn("linked/Cargo.toml", repin.tracked_files(repo / "keel"))
            self.assertEqual(repin.find_pins(repo / "keel"), [OLD])
            self.assertNotIn(Path("linked/original.rs"), list(repin.migration_sources(repo / "keel")))

    def test_generated_migration_output_cannot_copy_through_existing_link(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            remote, _ = fixtures.make_keel_remote(base, 'mkdir -p generated; echo changed > generated/original.rs\n')
            repo = self.mixed_title(base)
            outside = base / "outside"
            outside.mkdir()
            (outside / "original.rs").write_text("untouched\n")
            (repo / "keel/generated").symlink_to(outside, target_is_directory=True)
            rc, _, _ = self.run_scoped(base, repo, remote)
            self.assertEqual(rc, 1)
            self.assertEqual((outside / "original.rs").read_text(), "untouched\n")

    def test_pr_rejects_outside_staged_change_without_branch_or_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            remote, _ = fixtures.make_keel_remote(base)
            repo = self.mixed_title(base)
            self.assertEqual(self.run_scoped(base, repo, remote)[0], 0)
            (repo / "src/lib.rs").write_text("person-owned staged original\n")
            git(repo, "add", "src/lib.rs")
            before = git(repo, "rev-parse", "HEAD")
            self.assertEqual(repin.main(["pr", "--root", str(repo), "--source-root", "keel",
                                         "--report", str(base / "report.json")]), 1)
            self.assertEqual(git(repo, "rev-parse", "HEAD"), before)
            self.assertEqual(git(repo, "branch", "--show-current"), "main")
            self.assertEqual(git(repo, "diff", "--cached", "--name-only"), "src/lib.rs")

    def test_pr_cannot_silently_widen_a_scoped_report_to_default_root(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            remote, _ = fixtures.make_keel_remote(base)
            repo = self.mixed_title(base)
            self.assertEqual(self.run_scoped(base, repo, remote)[0], 0)
            self.assertEqual(repin.main(["pr", "--root", str(repo), "--report", str(base / "report.json")]), 1)
            self.assertEqual(git(repo, "branch", "--show-current"), "main")

    def test_status_handles_literal_names_untracked_files_and_cross_scope_rename(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = self.mixed_title(base)
            git(repo, "mv", "src/lib.rs", "keel/app/from-original.rs")
            (repo / "keel/app/new file.rs").write_text("untracked\n")
            names = repin.source_changes(repo, repo / "keel")
            self.assertEqual(names, ["keel/app/from-original.rs", "keel/app/new file.rs"])
            with self.assertRaises(repin.Refused):
                repin.check_staged_scope(repo, repo / "keel")
            literal = repo / "source[1]"
            literal.mkdir()
            (literal / "one.rs").write_text("literal pathspec\n")
            self.assertEqual(repin.source_changes(repo, literal), ["source[1]/one.rs"])

    def test_scoped_build_output_is_still_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            remote, _ = fixtures.make_keel_remote(base)
            repo = self.mixed_title(base)
            self.assertEqual(self.run_scoped(base, repo, remote, "--check-command",
                "mkdir -p target; echo output > target/file")[0], 1)

    def test_registry_lock_commands_only_target_scoped_manifests(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = self.mixed_title(Path(directory))
            calls = []
            real_run = repin.run
            def run(command, **kwargs):
                if command[:2] == ["cargo", "metadata"]:
                    calls.append((command, kwargs["cwd"]))
                    return fixtures.subprocess.CompletedProcess(command, 0, "{}", "")
                return real_run(command, **kwargs)
            with mock.patch.object(repin, "run", side_effect=run):
                self.assertIsNone(repin.refresh_registry_locks(repo / "keel", "0.3.1234", []))
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0][calls[0][0].index("--manifest-path") + 1], str(repo / "keel/Cargo.toml"))
            self.assertEqual(calls[0][1], repo / "keel")

    def test_lock_manifest_symlink_cannot_select_original_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = self.mixed_title(Path(directory))
            (repo / "keel/Cargo.toml").unlink()
            (repo / "keel/Cargo.toml").symlink_to(repo / "Cargo.toml")
            with mock.patch.object(repin, "run", wraps=repin.run) as run:
                with self.assertRaises(repin.Refused):
                    repin.refresh_registry_locks(repo / "keel", "0.3.1234", [])
            self.assertFalse(any(call.args[0][0] == "cargo" for call in run.call_args_list))

    def test_layer_poll_and_run_keep_original_engine_pins_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = self.mixed_title(base)
            (repo / "ENGINE_REV").write_text("c" * 40 + "\n")
            (repo / "keel/ENGINE_REV").write_text(OLD + "\n")
            git(repo, "add", "ENGINE_REV", "keel/ENGINE_REV")
            git(repo, "commit", "-qm", "layer pins")
            target = "b" * 40
            args = ["--layer", "engine", "--tag", target, "--root", str(repo), "--source-root", "keel"]
            with mock.patch.object(repin, "pr_exists", return_value=False), \
                 mock.patch.object(repin, "remote_branch_exists", return_value=False) as branch, \
                 mock.patch.object(repin, "layer_ahead", return_value=True), \
                 mock.patch.object(repin, "write_outputs") as output:
                self.assertEqual(repin.main(["poll", *args]), 0)
                self.assertEqual(output.call_args.args[0]["needed"], "true")
                self.assertEqual(branch.call_args.args[0], repo)
            with mock.patch.object(repin, "checkout_layer"), \
                 mock.patch.object(repin, "remote_branch_exists", return_value=False), \
                 mock.patch.object(repin, "refresh_layer_locks", return_value=None):
                self.assertEqual(repin.main(["run", *args, "--check-command", "pwd > layer-cwd",
                    "--check-dir", "app", "--report", str(base / "report.json")]), 0)
            report = json.loads((base / "report.json").read_text())
            self.assertEqual((report["source_root"], report["old"]), ("keel", [OLD]))
            self.assertTrue(all(name.startswith("keel/") for name in report["changed"]))
            self.assertEqual((repo / "ENGINE_REV").read_text().strip(), "c" * 40)
            self.assertEqual((repo / "keel/ENGINE_REV").read_text().strip(), target)
            self.assertEqual((repo / "keel/app/layer-cwd").read_text().strip(), str(repo / "keel/app"))

    def test_action_passes_source_root_only_to_source_modes(self):
        action = (fixtures.ACTION / "action.yml").read_text()
        self.assertIn('source-root:\n', action)
        self.assertEqual(action.count('SOURCE_ROOT: ${{ inputs.source-root }}'), 3)
        self.assertEqual(action.count('--source-root "$SOURCE_ROOT"'), 3)
        self.assertIn('relative to source-root', action)
        self.assertNotIn('dispatch --source-root', action)
        self.assertNotIn('settle --source-root', action)


if __name__ == "__main__":
    unittest.main()
