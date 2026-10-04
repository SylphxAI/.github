#!/usr/bin/env python3
"""Tests for the keel-pin-check action, run against a fixture Keel repository and fixture titles (no network)."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
ACTION = ROOT / ".github" / "actions" / "keel-pin-check"
SPEC = importlib.util.spec_from_file_location("keel_pin_check", ACTION / "keel_pin_check.py")
kpc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(kpc)

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
URL = "https://github.com/SylphxAI/keel"


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, env=ENV, check=True, text=True, capture_output=True).stdout.strip()


def commit_file(repo, msg):
    path = pathlib.Path(repo) / "f"
    path.write_text(path.read_text() + msg + "\n" if path.exists() else msg + "\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


class Fixture:
    """A Keel history: c1 c2 c3(tag keel-verified-2026-10-03-1) c4 c5 on main, and x1 on a side branch off c2."""

    def __init__(self, base: pathlib.Path):
        self.base = base
        work = base / "keel-work"
        work.mkdir()
        git(work, "init", "-q", "-b", "main")
        self.c1 = commit_file(work, "c1")
        self.c2 = commit_file(work, "c2")
        git(work, "checkout", "-q", "-b", "side")
        self.x1 = commit_file(work, "x1")
        git(work, "checkout", "-q", "main")
        self.c3 = commit_file(work, "c3")
        git(work, "tag", "-a", "keel-verified-2026-10-03-1", "-m", "t")  # annotated: the tag object is not the commit
        self.c4 = commit_file(work, "c4")
        git(work, "tag", "keel-weekly-2026-09-28", self.c1)  # lightweight, older
        self.c5 = commit_file(work, "c5")
        self.remote = base / "keel.git"
        git(base, "clone", "-q", "--bare", str(work), str(self.remote))
        # The fetch the action does: main and the tags only, so the side branch is never in the clone.
        self.keel_dir = str(base / "keel-clone")
        kpc.ensure_keel(self.keel_dir, str(self.remote), "refs/heads/main")
        self.keel = kpc.Keel(self.keel_dir, "refs/heads/main")

    def title(self, name, files, parent_files=None):
        """A title repository: an optional base commit, then a merge commit like a pull request's merge ref."""
        repo = self.base / name
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        if parent_files is not None:
            self.write(repo, parent_files)
            git(repo, "add", "-A")
            git(repo, "commit", "-q", "-m", "base")
            git(repo, "checkout", "-q", "-b", "pr")
            self.write(repo, files)
            git(repo, "add", "-A")
            git(repo, "commit", "-q", "--allow-empty", "-m", "pr")
            git(repo, "checkout", "-q", "main")
            # Move main so the merge is a true merge commit (the first parent is the base).
            (repo / "other").write_text("o")
            git(repo, "add", "-A")
            git(repo, "commit", "-q", "-m", "main moves")
            git(repo, "merge", "-q", "--no-ff", "-m", "merge", "pr")
        else:
            self.write(repo, files)
            git(repo, "add", "-A")
            git(repo, "commit", "-q", "-m", "init")
        return repo

    @staticmethod
    def write(repo, files):
        for name, text in files.items():
            p = pathlib.Path(repo) / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)


def lock(*shas):
    return "".join(
        f'[[package]]\nname = "keel-{i}"\nversion = "0.1.0"\n'
        f'source = "git+{URL}?rev={s}#{s}"\n\n' for i, s in enumerate(shas))


def run_check(fx, repo, base="", head="HEAD"):
    findings, notes, changed = kpc.check(str(repo), head, base, fx.keel)
    errors = [m for lvl, _, m in findings if lvl == "error"]
    warnings = [m for lvl, _, m in findings if lvl == "warning"]
    return errors, warnings, notes


class PinCheckTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.fx = Fixture(pathlib.Path(cls._tmp.name))
        cls.n = 0

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def title(self, files, parent_files=None):
        type(self).n += 1
        return self.fx.title(f"t{self.n}", files, parent_files)

    # ---- the acceptance cases

    def test_off_main_commit_fails_naming_the_commit(self):
        errors, _, _ = run_check(self.fx, self.title({"KEEL_PIN": self.fx.x1 + "\n"}))
        self.assertEqual(len(errors), 1)
        self.assertIn(self.fx.x1, errors[0])
        self.assertIn("not on Keel main", errors[0])

    def test_short_off_main_commit_fails_naming_it(self):
        errors, _, _ = run_check(self.fx, self.title({"KEEL_PIN": self.fx.x1[:9] + "\n"}))
        self.assertIn(self.fx.x1[:9], errors[0])

    def test_verified_tag_commit_passes_clean(self):
        errors, warnings, _ = run_check(self.fx, self.title({"KEEL_PIN": self.fx.c3 + "\n"}))
        self.assertEqual((errors, warnings), ([], []))

    def test_untagged_main_commit_passes_with_a_warning(self):
        errors, warnings, _ = run_check(self.fx, self.title({"KEEL_PIN": self.fx.c5 + "\n"}))
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn(self.fx.c5[:9], warnings[0])
        self.assertIn("keel-verified-2026-10-03-1", warnings[0])  # names the newest tag to move to

    def test_main_commit_older_than_a_tag_is_still_untagged(self):
        # c2 is contained in the verified tag's history but is not a tag's own commit.
        _, warnings, _ = run_check(self.fx, self.title({"KEEL_PIN": self.fx.c2 + "\n"}))
        self.assertEqual(len(warnings), 1)

    def test_weekly_tag_commit_passes_clean(self):
        errors, warnings, _ = run_check(self.fx, self.title({"KEEL_PIN": self.fx.c1 + "\n"}))
        self.assertEqual((errors, warnings), ([], []))

    def test_two_revs_in_one_lockfile_fail(self):
        repo = self.title({"Cargo.lock": lock(self.fx.c3, self.fx.c4)})
        errors, _, _ = run_check(self.fx, repo)
        self.assertEqual(len(errors), 1)
        self.assertIn("2 different Keel commits", errors[0])
        self.assertIn(self.fx.c3[:9], errors[0])
        self.assertIn(self.fx.c4[:9], errors[0])

    # ---- where pins live

    def test_cargo_toml_rev_lines_and_lock_must_agree(self):
        toml = (f'[dependencies]\nkeel-engine = {{ git = "{URL}", rev = "{self.fx.c3}", default-features = false }}\n'
                f'cubeage-kit = {{ git = "https://github.com/Cubeage/cubeage-kit", rev = "{self.fx.c4}" }}\n')
        errors, warnings, _ = run_check(self.fx, self.title({"Cargo.toml": toml, "Cargo.lock": lock(self.fx.c3)}))
        self.assertEqual((errors, warnings), ([], []))  # the kit's rev is another repository's, not a Keel pin
        errors, _, _ = run_check(self.fx, self.title({"Cargo.toml": toml, "Cargo.lock": lock(self.fx.c4)}))
        self.assertIn("2 different Keel commits", errors[0])

    def test_short_and_full_forms_of_one_commit_are_one_commit(self):
        files = {"KEEL_PIN": self.fx.c3[:10] + "\n", "deps/keel.rev": self.fx.c3 + "\n"}
        self.assertEqual(run_check(self.fx, self.title(files))[:2], ([], []))

    def test_per_dependency_table(self):
        toml = f'[dependencies.keel-ui]\ngit = "{URL}"\nrev = "{self.fx.x1}"\n\n[dependencies.serde]\nversion = "1"\n'
        errors, _, _ = run_check(self.fx, self.title({"Cargo.toml": toml}))
        self.assertIn(self.fx.x1, errors[0])

    def test_tag_pin_in_cargo_toml_resolves_through_keel_tags(self):
        good = f'keel-ui = {{ git = "{URL}", tag = "keel-verified-2026-10-03-1" }}\n'
        self.assertEqual(run_check(self.fx, self.title({"Cargo.toml": "[dependencies]\n" + good}))[:2], ([], []))
        bad = f'keel-ui = {{ git = "{URL}", tag = "keel-verified-2099-01-01-1" }}\n'
        errors, _, _ = run_check(self.fx, self.title({"Cargo.toml": "[dependencies]\n" + bad}))
        self.assertIn("2099", errors[0])

    def test_moving_branch_and_unpinned_dependency_fail(self):
        toml = f'[dependencies]\nkeel-ui = {{ git = "{URL}", branch = "main" }}\nkeel-net = {{ git = "{URL}" }}\n'
        errors, _, _ = run_check(self.fx, self.title({"Cargo.toml": toml}))
        self.assertEqual(len(errors), 2)
        self.assertIn("default branch", " ".join(errors))
        self.assertIn("branch main", " ".join(errors))

    def test_package_url_mention_is_not_a_pin(self):
        toml = f'[package]\nname = "x"\nrepository = "{URL}"\n'
        errors, warnings, notes = run_check(self.fx, self.title({"Cargo.toml": toml}))
        self.assertEqual((errors, warnings), ([], []))
        self.assertIn("no Keel pin found", notes[0])

    def test_cargo_config_source_and_vendor_pins(self):
        cfg = f'[source."git+{URL}?rev={self.fx.c3}"]\ngit = "{URL}"\nrev = "{self.fx.c3}"\nreplace-with = "vendored-sources"\n'
        files = {".cargo/config.toml": cfg, "vendor/kit/kit-0.1.0/deps/keel.rev": self.fx.c4 + "\n"}
        errors, _, _ = run_check(self.fx, self.title(files))
        self.assertIn("2 different Keel commits", errors[0])
        self.assertIn("vendor/kit/kit-0.1.0/deps/keel.rev", errors[0])

    def test_pin_file_with_a_branch_name_fails(self):
        errors, _, _ = run_check(self.fx, self.title({"KEEL_PIN": "main\n"}))
        self.assertIn("neither a commit nor a keel tag", errors[0])

    def test_target_and_node_modules_are_ignored(self):
        files = {"KEEL_PIN": self.fx.c3 + "\n", "target/x/KEEL_PIN": self.fx.x1 + "\n"}
        self.assertEqual(run_check(self.fx, self.title(files))[:2], ([], []))

    # ---- the ratchet

    def test_unmoved_off_main_pin_is_a_warning_not_a_failure(self):
        files = {"KEEL_PIN": self.fx.x1 + "\n"}
        repo = self.title({**files, "src.rs": "// pr\n"}, parent_files=files)
        errors, warnings, notes = run_check(self.fx, repo, base="auto")
        self.assertEqual(errors, [])
        self.assertIn(self.fx.x1, warnings[0])
        self.assertIn("[existing]", warnings[0])
        self.assertIn("ratchet", notes[0])

    def test_moving_the_pin_to_an_off_main_commit_fails(self):
        repo = self.title({"KEEL_PIN": self.fx.x1 + "\n"}, parent_files={"KEEL_PIN": self.fx.c3 + "\n"})
        errors, _, _ = run_check(self.fx, repo, base="auto")
        self.assertIn(self.fx.x1, errors[0])

    def test_moving_one_pin_off_a_fork_still_checks_the_rest(self):
        repo = self.title({"KEEL_PIN": self.fx.c3 + "\n", "Cargo.lock": lock(self.fx.c4)},
                          parent_files={"KEEL_PIN": self.fx.c4 + "\n", "Cargo.lock": lock(self.fx.c4)})
        errors, _, _ = run_check(self.fx, repo, base="auto")
        self.assertIn("2 different Keel commits", errors[0])

    def test_fixing_a_fork_passes(self):
        repo = self.title({"KEEL_PIN": self.fx.c3 + "\n", "Cargo.lock": lock(self.fx.c3)},
                          parent_files={"KEEL_PIN": self.fx.x1 + "\n", "Cargo.lock": lock(self.fx.x1)})
        self.assertEqual(run_check(self.fx, repo, base="auto")[:2], ([], []))

    def test_auto_base_on_a_plain_commit_enforces(self):
        errors, _, _ = run_check(self.fx, self.title({"KEEL_PIN": self.fx.x1 + "\n"}), base="auto")
        self.assertEqual(len(errors), 1)

    def test_missing_base_enforces_with_a_note(self):
        errors, _, notes = run_check(self.fx, self.title({"KEEL_PIN": self.fx.x1 + "\n"}), base="0" * 40)
        self.assertEqual(len(errors), 1)
        self.assertIn("fetch-depth", notes[0])

    def test_explicit_base_revision(self):
        repo = self.title({"KEEL_PIN": self.fx.x1 + "\n"}, parent_files={"KEEL_PIN": self.fx.x1 + "\n"})
        base = git(repo, "rev-parse", "HEAD^1")
        self.assertEqual(run_check(self.fx, repo, base=base)[0], [])

    # ---- the command line and the fetch

    def run_main(self, repo, *extra):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = kpc.main(["check", "--root", str(repo), "--keel-dir", self.fx.keel_dir, "--summary", "", *extra])
        return rc, out.getvalue()

    def test_exit_codes_and_annotations(self):
        rc, out = self.run_main(self.title({"KEEL_PIN": self.fx.x1 + "\n"}))
        self.assertEqual(rc, 1)
        self.assertIn("::error file=KEEL_PIN,line=1::", out)
        self.assertIn("1 failure(s), 0 warning(s)", out)
        rc, out = self.run_main(self.title({"KEEL_PIN": self.fx.c5 + "\n"}))
        self.assertEqual(rc, 0)
        self.assertIn("::warning file=KEEL_PIN,line=1::", out)
        rc, out = self.run_main(self.title({"KEEL_PIN": self.fx.c3 + "\n"}))
        self.assertEqual((rc, "0 failure(s), 0 warning(s)" in out), (0, True))

    def test_summary_file_is_appended(self):
        repo = self.title({"KEEL_PIN": self.fx.c5 + "\n"})
        summary = pathlib.Path(self._tmp.name) / "summary.md"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            kpc.main(["check", "--root", str(repo), "--keel-dir", self.fx.keel_dir, "--summary", str(summary)])
        self.assertIn("**warning**", summary.read_text())

    def test_unusable_keel_clone_exits_2(self):
        empty = pathlib.Path(self._tmp.name) / "empty.git"
        git(self._tmp.name, "init", "-q", "--bare", str(empty))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = kpc.main(["check", "--root", str(self.title({"KEEL_PIN": self.fx.c3})), "--keel-dir", str(empty), "--summary", ""])
        self.assertEqual(rc, 2)
        self.assertIn("could not run", out.getvalue())

    def test_fetch_brings_main_and_tags_but_not_other_branches(self):
        keel = self.fx.keel
        self.assertTrue(keel.on_main(self.fx.c5))
        self.assertEqual(sorted(keel.tag_commit), ["keel-verified-2026-10-03-1", "keel-weekly-2026-09-28"])
        self.assertEqual(keel.tag_commit["keel-verified-2026-10-03-1"], self.fx.c3)  # peeled
        self.assertIsNone(keel.resolve(self.fx.x1)[0])

    def test_fetch_is_repeatable_and_follows_new_commits(self):
        work = self.fx.base / "keel-work"
        c6 = commit_file(work, "c6")
        git(work, "push", "-q", str(self.fx.remote), "main")
        kpc.ensure_keel(self.fx.keel_dir, str(self.fx.remote), "refs/heads/main")
        self.assertTrue(kpc.Keel(self.fx.keel_dir, "refs/heads/main").on_main(c6))

    def test_tag_ordering(self):
        keys = [kpc.tag_key(t) for t in ("keel-weekly-2026-09-28", "keel-verified-2026-10-03-2", "keel-verified-2026-10-03-10")]
        self.assertLess(keys[0], keys[1])
        self.assertLess(keys[1], keys[2])
        self.assertIsNone(kpc.tag_key("keel-verified-latest"))


class ManifestTest(unittest.TestCase):
    def test_action_runs_on_our_runners_and_reads_secrets_through_env(self):
        text = (ACTION / "action.yml").read_text()
        self.assertIn("using: composite", text)
        self.assertNotIn("ubuntu-latest", text)
        for line in text.splitlines():
            if line.strip().startswith("run:"):
                self.assertNotIn("${{", line)  # no expression in a run line: inputs travel through env

    def test_starter_has_no_hosted_runner(self):
        text = (ROOT / "workflow-templates" / "keel-pin-check.yml").read_text()
        self.assertIn("runs-on: sylphx-linux-standard", text)
        self.assertNotIn("ubuntu-latest", text)


if __name__ == "__main__":
    unittest.main()
