#!/usr/bin/env python3
"""Tests for the keel-repin action: pin discovery, rewrite, the build check and the pull request."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
import urllib.error
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
ACTION = ROOT / ".github" / "actions" / "keel-repin"
SPEC = importlib.util.spec_from_file_location("keel_repin", ACTION / "keel_repin.py")
keel_repin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(keel_repin)

OLD = "a" * 40
NEW_TAG = "keel-verified-2026-10-03-1"
OLD_TAG = "keel-weekly-2026-09-28"


def git(cwd, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, text=True, capture_output=True).stdout.strip()


def make_keel_remote(base: pathlib.Path) -> tuple[str, str]:
    """A bare repository standing in for the Keel repository, with one annotated tag."""
    work = base / "keel-work"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    (work / "f").write_text("keel")
    git(work, "add", "f")
    git(work, "commit", "-q", "-m", "keel")
    sha = git(work, "rev-parse", "HEAD")
    git(work, "tag", "-a", NEW_TAG, "-m", "tag")
    bare = base / "keel.git"
    git(base, "clone", "-q", "--bare", str(work), str(bare))
    return str(bare), sha


def make_title(base: pathlib.Path, files: dict[str, str]) -> pathlib.Path:
    repo = base / "title"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    for name, text in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    # An origin with no branches, so the branch-exists check has something to ask.
    origin = base / "origin.git"
    git(base, "init", "-q", "--bare", str(origin))
    git(repo, "remote", "add", "origin", str(origin))
    return repo


TITLE = {
    "Cargo.toml": (
        "[dependencies]\n"
        f'# Keel tag: {OLD_TAG}\n'
        f'keel-net = {{ git = "https://github.com/SylphxAI/keel", rev = "{OLD}" }}\n'
        f'keel-ui = {{ git = "https://github.com/SylphxAI/keel.git", rev = "{OLD}", features = ["x"] }}\n'
        'serde = { git = "https://github.com/other/serde", rev = "' + "c" * 40 + '" }\n'
        'keel-pinned = { git = "https://github.com/SylphxAI/keel-website", rev = "' + "d" * 40 + '" }\n'
    ),
    "deps/keel.rev": OLD + "\n",
    "Dockerfile": f"# built at keel {OLD[:7]}\nRUN echo {OLD}\n",
    ".cargo/config.toml": f'rev = "{OLD}"\n',
    "vendor/x/Cargo.toml": f'keel-net = {{ git = "https://github.com/SylphxAI/keel", rev = "{OLD}" }}\n',
    "CHANGELOG.md": f"moved from {OLD}\n",
    "src/lib.rs": "fn main() {}\n",
}


class PinsTest(unittest.TestCase):
    def test_tag_order_and_shape(self):
        self.assertLess(keel_repin.tag_key(OLD_TAG), keel_repin.tag_key(NEW_TAG))
        self.assertLess(keel_repin.tag_key("keel-verified-2026-10-03-1"), keel_repin.tag_key("keel-verified-2026-10-03-2"))
        self.assertLess(keel_repin.tag_key("keel-weekly-2026-10-03"), keel_repin.tag_key("keel-verified-2026-10-03-1"))
        self.assertIsNone(keel_repin.tag_key("v1.2.3"))
        self.assertIsNone(keel_repin.tag_key("keel-verified-latest"))

    def test_resolve_peels_annotated_tag(self):
        with tempfile.TemporaryDirectory() as d:
            remote, sha = make_keel_remote(pathlib.Path(d))
            self.assertEqual(keel_repin.resolve_tag(NEW_TAG, remote), sha)
            with self.assertRaises(keel_repin.Refused):
                keel_repin.resolve_tag("keel-verified-2020-01-01-1", remote)
            with self.assertRaises(keel_repin.Refused):
                keel_repin.resolve_tag("main", remote)

    def test_latest_picks_the_newest_verified_tag(self):
        with tempfile.TemporaryDirectory() as d:
            remote, sha = make_keel_remote(pathlib.Path(d))
            work = pathlib.Path(d) / "keel-work"
            git(work, "tag", "keel-verified-2026-09-30-2")
            git(work, "tag", "keel-verified-2026-10-03-10")
            git(work, "tag", "keel-weekly-2026-10-05")
            git(work, "push", "-q", remote, "--tags")
            self.assertEqual(keel_repin.newest_verified_tag(remote), "keel-verified-2026-10-03-10")

    def test_find_and_rewrite(self):
        with tempfile.TemporaryDirectory() as d:
            repo = make_title(pathlib.Path(d), TITLE)
            self.assertEqual(keel_repin.find_pins(repo), [OLD])  # not the other repos, not vendor/
            new = "b" * 40
            changed = keel_repin.rewrite(repo, [OLD], new, NEW_TAG)
            self.assertEqual(sorted(changed), [".cargo/config.toml", "Cargo.toml", "Dockerfile", "deps/keel.rev"])
            cargo = (repo / "Cargo.toml").read_text()
            self.assertEqual(cargo.count(new), 2)
            self.assertIn(f"# Keel tag: {NEW_TAG}", cargo)
            self.assertIn("c" * 40, cargo)  # other dependencies untouched
            self.assertIn("d" * 40, cargo)  # keel-website is not Keel
            self.assertEqual((repo / "deps/keel.rev").read_text(), new + "\n")
            self.assertEqual((repo / "Dockerfile").read_text(), f"# built at keel {new[:7]}\nRUN echo {new}\n")
            self.assertIn(OLD, (repo / "vendor/x/Cargo.toml").read_text())  # vendor/ is the hook's
            self.assertIn(OLD, (repo / "CHANGELOG.md").read_text())

    def test_tag_pin_moves(self):
        with tempfile.TemporaryDirectory() as d:
            repo = make_title(pathlib.Path(d), {
                "Cargo.toml": f'keel-net = {{ git = "https://github.com/SylphxAI/keel", tag = "{OLD_TAG}" }}\n'})
            self.assertEqual(keel_repin.find_pins(repo), [])
            self.assertTrue(keel_repin.has_tag_pins(repo))
            keel_repin.rewrite(repo, [], "b" * 40, NEW_TAG)
            self.assertIn(f'tag = "{NEW_TAG}"', (repo / "Cargo.toml").read_text())

    def test_lock_packages(self):
        lock = (
            '[[package]]\nname = "keel-net"\nsource = "git+https://github.com/SylphxAI/keel?rev=%s#%s"\n\n'
            '[[package]]\nname = "serde"\nsource = "registry+https://github.com/rust-lang/crates.io-index"\n' % (OLD, OLD)
        )
        self.assertEqual(keel_repin.lock_packages(lock), ["keel-net"])

    def test_a_name_locked_at_two_keel_commits_gets_its_full_package_id(self):
        # The title pins one Keel commit and a dependency (a title kit) pins another, so the lock
        # holds keel-ai twice and `cargo update -p keel-ai` is ambiguous.
        kit = "e" * 40
        src = 'source = "git+https://github.com/SylphxAI/keel?rev=%s#%s"\n'
        lock = (
            '[[package]]\nname = "keel-ai"\nversion = "0.1.0"\n' + src % (kit, kit) + "\n"
            '[[package]]\nname = "keel-ai"\nversion = "0.1.0"\n' + src % (OLD, OLD) + "\n"
            '[[package]]\nname = "keel-net"\nversion = "0.2.0"\n' + src % (OLD, OLD) + "\n"
            '[[package]]\nname = "serde"\nversion = "1.0.0"\nsource = "registry+https://github.com/rust-lang/crates.io-index"\n'
        )
        self.assertEqual(keel_repin.lock_update_specs(lock, [OLD]),
                         [f"git+https://github.com/SylphxAI/keel?rev={OLD}#keel-ai@0.1.0", "keel-net"])
        # No moved commit among them: every entry of the ambiguous name is named exactly.
        self.assertEqual(keel_repin.lock_update_specs(lock, []), [
            f"git+https://github.com/SylphxAI/keel?rev={kit}#keel-ai@0.1.0",
            f"git+https://github.com/SylphxAI/keel?rev={OLD}#keel-ai@0.1.0", "keel-net"])

    @unittest.skipUnless(shutil.which("cargo") and shutil.which("git"), "needs cargo")
    def test_the_full_package_id_is_one_cargo_accepts(self):
        with tempfile.TemporaryDirectory() as d:
            base = pathlib.Path(d)
            revs = {}
            for lib in ("kit-keel", "title-keel"):
                w = base / lib
                (w / "src").mkdir(parents=True)
                (w / "Cargo.toml").write_text('[package]\nname = "keel-ai"\nversion = "0.1.0"\nedition = "2021"\n')
                (w / "src" / "lib.rs").write_text("")
                git(w, "init", "-q", "-b", "main")
                git(w, "add", "-A")
                git(w, "commit", "-q", "-m", "one")
                revs[lib] = [git(w, "rev-parse", "HEAD")]
                (w / "src" / "lib.rs").write_text("// two\n")
                git(w, "commit", "-q", "-am", "two")
                revs[lib].append(git(w, "rev-parse", "HEAD"))
            def manifest(name, deps):
                (base / name / "src").mkdir(parents=True, exist_ok=True)
                (base / name / "src" / "lib.rs").write_text("")
                (base / name / "Cargo.toml").write_text(
                    f'[package]\nname = "{name}"\nversion = "0.1.0"\nedition = "2021"\n[dependencies]\n' + deps)
            manifest("kit", f'keel-ai = {{ git = "file://{base}/kit-keel", rev = "{revs["kit-keel"][0]}" }}\n')
            manifest("app", f'keel-ai = {{ git = "file://{base}/title-keel", rev = "{revs["title-keel"][0]}" }}\n'
                            'kit = { path = "../kit" }\n')
            env = {**os.environ, "CARGO_HOME": str(base / "cargo-home"), "CARGO_NET_OFFLINE": "false"}
            app = base / "app"
            subprocess.run(["cargo", "generate-lockfile", "-q"], cwd=app, env=env, check=True, capture_output=True)
            lock = (app / "Cargo.lock").read_text()
            # lock_entries keys on the Keel URL; stand the local remotes in for it.
            shown = lock.replace(f"file://{base}/title-keel", "https://github.com/SylphxAI/keel")
            self.assertEqual(len([e for e in keel_repin.lock_entries(shown) if e[0] == "keel-ai"]), 1)
            old, new = revs["title-keel"]
            (app / "Cargo.toml").write_text((app / "Cargo.toml").read_text().replace(old, new))
            specs = [f"git+file://{base}/title-keel?rev={old}#keel-ai@0.1.0"]
            p = subprocess.run(["cargo", "update", "-p", "keel-ai"], cwd=app, env=env, capture_output=True, text=True)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn("ambiguous", p.stderr)
            subprocess.run(["cargo", "update", *sum((["-p", x] for x in specs), [])], cwd=app, env=env, check=True,
                           capture_output=True)
            lock = (app / "Cargo.lock").read_text()
            self.assertIn(f"title-keel?rev={new}", lock)
            self.assertIn(f"kit-keel?rev={revs['kit-keel'][0]}", lock)  # the kit's own Keel commit is left as it is


class RunTest(unittest.TestCase):
    def run_cmd(self, repo, remote, check, extra=()):
        report = repo.parent / "report.json"
        rc = keel_repin.main(["run", "--root", str(repo), "--tag", NEW_TAG, "--remote", remote,
                              "--check-command", check, "--report", str(report), *extra])
        return rc, json.loads(report.read_text())

    def test_ok_and_failed_check(self):
        with tempfile.TemporaryDirectory() as d:
            base = pathlib.Path(d)
            remote, sha = make_keel_remote(base)
            repo = make_title(base, TITLE)
            rc, rep = self.run_cmd(repo, remote, "echo fine")
            self.assertEqual((rc, rep["status"], rep["sha"]), (0, "ok", sha))
            self.assertEqual(rep["old"], [OLD])
            self.assertIn("Cargo.toml", rep["changed"])
            git(repo, "checkout", "-q", "--", ".")
            rc, rep = self.run_cmd(repo, remote, "echo 'error[E0432]: unresolved import SavePort' >&2; exit 101")
            self.assertEqual((rc, rep["status"], rep["stage"]), (0, "failed", "check"))
            self.assertIn("SavePort", rep["log"])
            self.assertIn("Cargo.toml", rep["changed"])  # the pin still moves; the break is the content of the draft

    def test_already_pinned_and_branch_exists_and_no_pin(self):
        with tempfile.TemporaryDirectory() as d:
            base = pathlib.Path(d)
            remote, sha = make_keel_remote(base)
            pinned = make_title(base, {"deps/keel.rev": sha + "\n", "Cargo.toml": f'k = {{ git = "https://github.com/SylphxAI/keel", rev = "{sha}" }}\n'})
            rc, rep = self.run_cmd(pinned, remote, "true")
            self.assertEqual((rc, rep["status"]), (0, "unchanged"))
        with tempfile.TemporaryDirectory() as d:
            base = pathlib.Path(d)
            remote, _ = make_keel_remote(base)
            repo = make_title(base, TITLE)
            git(repo, "push", "-q", "origin", f"HEAD:refs/heads/{keel_repin.BRANCH_PREFIX}{NEW_TAG}")
            rc, rep = self.run_cmd(repo, remote, "true")
            self.assertEqual((rc, rep["status"]), (0, "branch-exists"))
            self.assertIn(OLD, (repo / "deps/keel.rev").read_text())  # untouched
            rc, rep = self.run_cmd(repo, remote, "true", ["--ignore-branch"])
            self.assertEqual(rep["status"], "ok")
        with tempfile.TemporaryDirectory() as d:
            base = pathlib.Path(d)
            remote, _ = make_keel_remote(base)
            repo = make_title(base, {"src/lib.rs": "fn main() {}\n"})
            report = base / "r.json"
            self.assertEqual(keel_repin.main(["run", "--root", str(repo), "--tag", NEW_TAG, "--remote", remote,
                                              "--report", str(report)]), 1)

    def test_title_hook_replaces_the_rewrite(self):
        with tempfile.TemporaryDirectory() as d:
            base = pathlib.Path(d)
            remote, sha = make_keel_remote(base)
            files = dict(TITLE)
            files["tools/repin_keel.sh"] = '#!/usr/bin/env bash\necho "$1" > hook-ran.txt\n'
            repo = make_title(base, files)
            rc, rep = self.run_cmd(repo, remote, "true")
            self.assertEqual((rc, rep["status"]), (0, "ok"))
            self.assertEqual((repo / "hook-ran.txt").read_text().strip(), NEW_TAG)
            self.assertIn(OLD, (repo / "deps/keel.rev").read_text())  # the generic rewrite did not run
            self.assertEqual(rep["changed"], ["hook-ran.txt"])

    def test_build_output_that_git_does_not_ignore_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            base = pathlib.Path(d)
            remote, _ = make_keel_remote(base)
            repo = make_title(base, TITLE)
            report = base / "r.json"
            rc = keel_repin.main(["run", "--root", str(repo), "--tag", NEW_TAG, "--remote", remote,
                                  "--check-command", "mkdir -p target && echo x > target/o", "--report", str(report)])
            self.assertEqual(rc, 1)


class PullRequestTest(unittest.TestCase):
    def report(self, status="ok", stage=""):
        return {"tag": NEW_TAG, "sha": "b" * 40, "branch": keel_repin.BRANCH_PREFIX + NEW_TAG, "status": status,
                "stage": stage, "changed": ["Cargo.toml", "deps/keel.rev"], "old": [OLD],
                "log": "line\n" * 100 + "error[E0432]: unresolved import `SavePort` ```"}

    def test_text(self):
        title, body = keel_repin.pr_text(self.report())
        self.assertEqual(title, f"chore(keel): repin Keel to {NEW_TAG} ({'b' * 9})")
        self.assertIn("build check passed", body)
        self.assertIn("workflow_dispatch", body)  # CI is started on the branch
        title, body = keel_repin.pr_text({**self.report("failed", "check"), "manual": [".github/workflows/ci.yml"]})
        self.assertIn("Draft: the build check fails", body)
        self.assertIn("SavePort", body)
        self.assertEqual(body.count("```"), 2)  # the log cannot close its own fence
        self.assertIn("CI was not started by the bot", body)
        self.assertIn("`.github/workflows/ci.yml`", body)  # a workflow file the token cannot edit is named, not edited

    def test_a_private_repository_the_build_could_not_read_is_named(self):
        log = ("\x1b[1m\x1b[92m    Updating\x1b[0m git repository `https://github.com/Cubeage/warden-keel`\n"
               "fatal: could not read Username for 'https://github.com': terminal prompts disabled\n"
               "\x1b[1m\x1b[33mwarning\x1b[0m: spurious network error (3 tries remaining): process didn't exit successfully: "
               "`git fetch --no-tags --force --update-head-ok 'https://github.com/Cubeage/warden-keel' '+65b2:refs/commit/65b2'` (exit status: 128)\n")
        _, body = keel_repin.pr_text({**self.report("failed", "lock"), "log": log})
        self.assertIn("could not read `Cubeage/warden-keel`", body)
        self.assertIn("extra-read-repos", body)
        self.assertNotIn("\x1b", body)  # cargo's colour codes are stripped from the quoted output
        _, body = keel_repin.pr_text(self.report("failed", "check"))
        self.assertNotIn("could not read", body)

    def fake_gh(self, base: pathlib.Path) -> pathlib.Path:
        calls = base / "gh-calls.txt"
        gh = base / "gh"
        gh.write_text(
            "#!/usr/bin/env bash\n"
            f'printf \'%s\\n\' "$*" >> {calls}\n'
            'case "$1 $2" in\n'
            '  "label list") printf "owner:thing\\nbug\\n" ;;\n'
            '  "pr list") printf "7 chore/keel-repin-keel-verified-2026-10-01-1\\n8 chore/keel-repin-keel-verified-2026-10-04-1\\n9 feature/x\\n" ;;\n'
            '  "pr create") echo https://example.invalid/pull/10 ;;\n'
            "esac\n"
        )
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
        return gh

    def write_report(self, base, **kw):
        path = base / "report.json"
        path.write_text(json.dumps(self.report(**kw)))
        return path

    def test_dry_run_prints_and_pushes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            base = pathlib.Path(d)
            repo = make_title(base, TITLE)
            gh = self.fake_gh(base)
            with mock.patch.dict(os.environ, {"KEEL_REPIN_GH": str(gh)}), mock.patch("builtins.print") as out:
                rc = keel_repin.main(["pr", "--root", str(repo), "--report", str(self.write_report(base, status="failed", stage="check")), "--dry-run"])
            text = "\n".join(str(c.args[0]) for c in out.call_args_list)
            self.assertEqual(rc, 0)
            self.assertIn("DRY RUN", text)
            self.assertIn("draft: True", text)
            self.assertIn("label: owner:thing", text)
            self.assertEqual(git(base / "origin.git", "branch", "--list"), "")
            self.assertNotIn("pr create", (base / "gh-calls.txt").read_text())

    def test_opens_draft_pr_and_closes_only_older_ones(self):
        with tempfile.TemporaryDirectory() as d:
            base = pathlib.Path(d)
            repo = make_title(base, TITLE)
            (repo / "deps/keel.rev").write_text("b" * 40 + "\n")
            gh = self.fake_gh(base)
            env = {"KEEL_REPIN_GH": str(gh), "GH_TOKEN": "tok", }
            with mock.patch.dict(os.environ, env):
                rc = keel_repin.main(["pr", "--root", str(repo), "--base", "main",
                                      "--report", str(self.write_report(base, status="failed", stage="check"))])
            self.assertEqual(rc, 0)
            calls = (base / "gh-calls.txt").read_text()
            self.assertIn("pr create", calls)
            self.assertIn("--draft", calls)  # the body spans lines, so read the whole record
            self.assertIn("--label owner:thing", calls)
            self.assertIn("pr close 7 --delete-branch", calls)
            self.assertNotIn("pr close 8", calls)  # a newer tag's pull request is never closed
            self.assertNotIn("pr close 9", calls)
            self.assertNotRegex(calls, r"(?m)^pr merge")
            self.assertIn(keel_repin.BRANCH_PREFIX + NEW_TAG, git(base / "origin.git", "branch", "--list"))


def fake_gh(base: pathlib.Path, answers: dict[str, str]) -> pathlib.Path:
    """A gh that records its arguments and answers by its first two words."""
    gh = base / "gh"
    cases = "".join(f'  "{k}") printf "%s" {json.dumps(v)} ;;\n' for k, v in answers.items())
    gh.write_text(f'#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >> {base}/gh-calls.txt\ncase "$1 $2" in\n{cases}esac\n')
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    return gh


class PollTest(unittest.TestCase):
    def poll(self, repo, remote, answers, extra_env=None):
        gh = fake_gh(repo.parent, answers)
        out = repo.parent / "out.txt"
        env = {"KEEL_REPIN_GH": str(gh), "GITHUB_OUTPUT": str(out), "KEEL_API_TOKEN": "t"}
        with mock.patch.dict(os.environ, {**env, **(extra_env or {})}):
            rc = keel_repin.main(["poll", "--root", str(repo), "--tag", "latest", "--remote", remote])
        return rc, dict(l.split("=", 1) for l in out.read_text().splitlines()) if out.exists() else {}

    def test_needed_when_the_tag_is_ahead(self):
        with tempfile.TemporaryDirectory() as d:
            base = pathlib.Path(d)
            remote, sha = make_keel_remote(base)
            repo = make_title(base, TITLE)
            rc, out = self.poll(repo, remote, {"api repos/SylphxAI/keel/compare/aaaa": "x", "pr list": "0", "api repos/SylphxAI/keel/compare/" + OLD + "..." + sha: "ahead"})
            self.assertEqual((rc, out["needed"], out["tag"]), (0, "true", NEW_TAG))

    def test_not_needed_when_pinned_behind_or_a_pull_request_exists(self):
        with tempfile.TemporaryDirectory() as d:
            base = pathlib.Path(d)
            remote, sha = make_keel_remote(base)
            pinned = make_title(base, {"deps/keel.rev": sha + "\n", "Cargo.toml": f'k = {{ git = "https://github.com/SylphxAI/keel", rev = "{sha}" }}\n'})
            self.assertEqual(self.poll(pinned, remote, {})[1]["needed"], "false")
        for answer, expect in (("behind", "not ahead"),):
            with tempfile.TemporaryDirectory() as d:
                base = pathlib.Path(d)
                remote, sha = make_keel_remote(base)
                repo = make_title(base, TITLE)
                rc, out = self.poll(repo, remote, {"api repos/SylphxAI/keel/compare/" + OLD + "..." + sha: answer, "pr list": "0"})
                self.assertEqual(out["needed"], "false")
                self.assertIn(expect, out["reason"])
        for states in ("OPEN\n", "MERGED\n", "CLOSED\nOPEN\n"):  # an open or merged pull request for the tag stops the poll
            with tempfile.TemporaryDirectory() as d:
                base = pathlib.Path(d)
                remote, sha = make_keel_remote(base)
                repo = make_title(base, TITLE)
                rc, out = self.poll(repo, remote, {"pr list": states})
                self.assertEqual((out["needed"], "exists" in out["reason"]), ("false", True), states)

    def test_a_closed_pull_request_stops_the_poll_only_while_its_branch_exists(self):
        """Closing a repin pull request and keeping its branch stops that tag; deleting the branch too rebuilds it."""
        with tempfile.TemporaryDirectory() as d:
            base = pathlib.Path(d)
            remote, sha = make_keel_remote(base)
            repo = make_title(base, TITLE)
            answers = {"pr list": "CLOSED\n", "api repos/SylphxAI/keel/compare/" + OLD + "..." + sha: "ahead"}
            rc, out = self.poll(repo, remote, answers)
            self.assertEqual((rc, out["needed"]), (0, "true"))
            git(repo, "push", "-q", "origin", "HEAD:refs/heads/" + keel_repin.BRANCH_PREFIX + NEW_TAG)
            (base / "out.txt").unlink()
            rc, out = self.poll(repo, remote, answers)
            self.assertEqual((out["needed"], "exists" in out["reason"]), ("false", True))

    def diverged_remote(self, base, pin_commit_file):
        """A Keel stand-in where the tag holds a backport's patch as a new commit (same change, other id) and the pin sits on a backport branch."""
        work = base / "keel-work"
        work.mkdir()
        git(work, "init", "-q", "-b", "main")
        (work / "f").write_text("keel\n")
        git(work, "add", "f")
        git(work, "commit", "-q", "-m", "base")
        git(work, "checkout", "-q", "-b", "backport/x")
        (work / pin_commit_file).write_text("patch\n")
        git(work, "add", pin_commit_file)
        git(work, "commit", "-q", "-m", "backport")
        pin = git(work, "rev-parse", "HEAD")
        git(work, "checkout", "-q", "main")
        (work / "other").write_text("main moved\n")
        git(work, "add", "other")
        git(work, "commit", "-q", "-m", "main moves")
        if pin_commit_file == "p":  # the same change lands on main as a different commit
            (work / "p").write_text("patch\n")
            git(work, "add", "p")
            git(work, "commit", "-q", "-m", "patch on main")
        git(work, "tag", "-a", NEW_TAG, "-m", "tag")
        bare = base / "keel.git"
        git(base, "clone", "-q", "--bare", str(work), str(bare))
        return str(bare), git(work, "rev-parse", "HEAD"), pin

    def diverged_title(self, base, pin):
        return make_title(base, {"deps/keel.rev": pin + "\n", "Cargo.toml": f'k = {{ git = "https://github.com/SylphxAI/keel", rev = "{pin}" }}\n'})

    def test_a_pin_whose_commits_are_all_on_the_tag_is_repinned(self):
        with tempfile.TemporaryDirectory() as d:
            base = pathlib.Path(d)
            remote, sha, pin = self.diverged_remote(base, "p")
            repo = self.diverged_title(base, pin)
            rc, out = self.poll(repo, remote, {"pr list": "0", "api repos/SylphxAI/keel/compare/" + pin + "..." + sha: "diverged"})
            self.assertEqual((rc, out["needed"]), (0, "true"))

    def test_a_pin_with_a_commit_the_tag_lacks_fails_the_poll(self):
        with tempfile.TemporaryDirectory() as d:
            base = pathlib.Path(d)
            remote, sha, pin = self.diverged_remote(base, "q")
            repo = self.diverged_title(base, pin)
            rc, out = self.poll(repo, remote, {"pr list": "0", "api repos/SylphxAI/keel/compare/" + pin + "..." + sha: "diverged"})
            self.assertEqual(rc, 1)
            self.assertNotIn("needed", out)

    def test_a_failed_compare_is_an_error_not_a_silent_skip(self):
        with tempfile.TemporaryDirectory() as d:
            base = pathlib.Path(d)
            remote, _ = make_keel_remote(base)
            repo = make_title(base, TITLE)
            gh = base / "gh"
            gh.write_text('#!/usr/bin/env bash\n[ "$1" = pr ] && { echo 0; exit 0; }\necho "HTTP 404" >&2; exit 1\n')
            gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
            with mock.patch.dict(os.environ, {"KEEL_REPIN_GH": str(gh)}):
                rc = keel_repin.main(["poll", "--root", str(repo), "--remote", remote])
            self.assertEqual(rc, 1)


class DispatchTest(unittest.TestCase):
    def test_dispatch_in_order_and_wait_for_the_run(self):
        with tempfile.TemporaryDirectory() as d:
            base = pathlib.Path(d)
            head = "c" * 40
            report = base / "r.json"
            report.write_text(json.dumps({"status": "ok", "branch": "chore/keel-repin-x", "head_sha": head}))
            gh = fake_gh(base, {"run list": f"{head} 123\n"})
            with mock.patch.dict(os.environ, {"KEEL_REPIN_GH": str(gh)}):
                rc = keel_repin.main(["dispatch", "--report", str(report), "--workflows", "ci.yml ci-ok.yml", "--interval", "0"])
            self.assertEqual(rc, 0)
            calls = (base / "gh-calls.txt").read_text().splitlines()
            runs = [c for c in calls if c.startswith("workflow run")]
            self.assertEqual(runs, ["workflow run ci.yml --ref chore/keel-repin-x", "workflow run ci-ok.yml --ref chore/keel-repin-x"])
            self.assertLess(calls.index(runs[0]), calls.index(next(c for c in calls if c.startswith("run list"))))

    def test_no_run_for_the_head_fails_and_a_draft_starts_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            base = pathlib.Path(d)
            report = base / "r.json"
            report.write_text(json.dumps({"status": "ok", "branch": "b", "head_sha": "c" * 40}))
            gh = fake_gh(base, {"run list": "dddddddddddddddddddddddddddddddddddddddd 1\n"})
            with mock.patch.dict(os.environ, {"KEEL_REPIN_GH": str(gh)}):
                rc = keel_repin.main(["dispatch", "--report", str(report), "--wait-seconds", "0", "--interval", "0"])
            self.assertEqual(rc, 1)
            report.write_text(json.dumps({"status": "failed", "branch": "b", "head_sha": "c" * 40}))
            (base / "gh-calls.txt").unlink()
            with mock.patch.dict(os.environ, {"KEEL_REPIN_GH": str(gh)}):
                self.assertEqual(keel_repin.main(["dispatch", "--report", str(report)]), 0)
            self.assertFalse((base / "gh-calls.txt").exists())


class WorkflowFilesTest(unittest.TestCase):
    def test_workflow_files_are_named_not_edited(self):
        with tempfile.TemporaryDirectory() as d:
            base = pathlib.Path(d)
            remote, _ = make_keel_remote(base)
            files = {**TITLE, ".github/workflows/ci.yml": f"# keel {OLD[:8]}\non: push\n"}
            repo = make_title(base, files)
            report = base / "r.json"
            keel_repin.main(["run", "--root", str(repo), "--tag", NEW_TAG, "--remote", remote, "--check-command", "true", "--report", str(report)])
            rep = json.loads(report.read_text())
            self.assertEqual(rep["manual"], [".github/workflows/ci.yml"])
            self.assertNotIn(".github/workflows/ci.yml", rep["changed"])
            self.assertIn(OLD[:8], (repo / ".github/workflows/ci.yml").read_text())

    def test_a_hook_edit_of_a_workflow_file_is_reverted(self):
        with tempfile.TemporaryDirectory() as d:
            base = pathlib.Path(d)
            remote, _ = make_keel_remote(base)
            files = {**TITLE, ".github/workflows/ci.yml": "on: push\n",
                     "tools/repin_keel.sh": "#!/usr/bin/env bash\necho edited >> .github/workflows/ci.yml\necho x > moved.txt\n"}
            repo = make_title(base, files)
            report = base / "r.json"
            keel_repin.main(["run", "--root", str(repo), "--tag", NEW_TAG, "--remote", remote, "--check-command", "true", "--report", str(report)])
            self.assertEqual(json.loads(report.read_text())["changed"], ["moved.txt"])
            self.assertEqual((repo / ".github/workflows/ci.yml").read_text(), "on: push\n")


FAKE_SETTLE_GH = r"""#!/usr/bin/env python3
import json, os, sys
d = os.environ["FAKE_DIR"]
sc = json.load(open(d + "/scenario.json"))
a = sys.argv[1:]
open(d + "/gh-calls.txt", "a").write(" ".join(a) + "\n")
if a[:2] == ["pr", "list"] and "merged" in a:
    if sc.get("merged"):
        print(json.dumps(sc["merged"]))
elif a[:2] == ["pr", "list"]:
    print("\n".join("\t".join(map(str, pr)) for pr in sc["prs"]))
elif a[:1] == ["api"] and "check-runs" in a[-1] + " ".join(a):
    print("\n".join("\t".join(map(str, r)) for r in sc["runs"]))
elif a[:1] == ["api"] and "/commits" in " ".join(a):
    print("\n".join(sc["emails"]))
elif a[:2] == ["pr", "view"] and "comments" in a:
    try:
        print(open(d + "/comments.txt").read())
    except FileNotFoundError:
        pass
elif a[:2] == ["pr", "view"] and "state" in a:
    print(sc.get("state", "OPEN"))
elif a[:2] == ["pr", "comment"]:
    open(d + "/comments.txt", "a").write(a[a.index("--body") + 1] + "\n")
elif a[:2] == ["pr", "merge"]:
    if "--auto" not in a and sc.get("merge") == "needs-auto":
        sys.stderr.write("the merge queue is on: use --auto\n"); sys.exit(1)
    if sc.get("merge") == "refuse":
        sys.stderr.write("Pull request is not mergeable: required status check\n"); sys.exit(1)
"""

HEAD = "e" * 40
BOT = keel_repin.BOT_EMAIL
TAG_BRANCH = keel_repin.BRANCH_PREFIX + NEW_TAG
NOW = 1_800_000_000


def check(name, status="completed", conclusion="success", app="github-actions", id=None):
    return [id or abs(hash(name + status + conclusion)) % 10_000, name, status, conclusion if status == "completed" else "", app, f"https://example.invalid/{name}"]


class SettleTest(unittest.TestCase):
    def settle(self, scenario, extra=None, repeat=1):
        with tempfile.TemporaryDirectory() as d:
            base = pathlib.Path(d)
            gh = base / "gh"
            gh.write_text(FAKE_SETTLE_GH)
            gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
            sc = {"prs": [[5, TAG_BRANCH, HEAD, "false", "2027-01-15T07:00:00Z"]], "emails": [BOT], "runs": [], **scenario}
            (base / "scenario.json").write_text(json.dumps(sc))
            created = keel_repin.calendar.timegm((2027, 1, 15, 7, 0, 0))
            env = {"KEEL_REPIN_GH": str(gh), "FAKE_DIR": str(base), "GITHUB_REPOSITORY": "o/title"}
            rcs = []
            with mock.patch.dict(os.environ, env), mock.patch("builtins.print"):
                for _ in range(repeat):
                    rcs.append(keel_repin.main(["settle", "--owner", "@o/team", "--now", str(created + 600), *(extra or [])]))
            calls = (base / "gh-calls.txt").read_text() if (base / "gh-calls.txt").exists() else ""
            comments = (base / "comments.txt").read_text() if (base / "comments.txt").exists() else ""
            return rcs, calls, comments

    def merges(self, calls):
        return [c for c in calls.splitlines() if c.startswith("pr merge")]

    def test_green_on_the_exact_head_merges_pinned_to_that_head(self):
        rcs, calls, comments = self.settle({"runs": [check("ci-ok"), check("web-smoke"), check("rust")]})
        self.assertEqual(rcs, [0])
        self.assertEqual(self.merges(calls), [f"pr merge 5 --squash --match-head-commit {HEAD}"])
        self.assertIn(f"commits/{HEAD}/check-runs", calls)  # judged on the head the merge is pinned to
        self.assertEqual(comments, "")

    def test_a_missing_or_unfinished_required_check_waits(self):
        for runs in ([check("ci-ok")], [check("ci-ok"), check("web-smoke", "in_progress")], []):
            rcs, calls, comments = self.settle({"runs": runs})
            self.assertEqual((self.merges(calls), comments), ([], ""), runs)

    def test_a_red_check_is_never_merged_and_the_owner_hears_once(self):
        runs = [check("ci-ok"), check("web-smoke", conclusion="failure")]
        rcs, calls, comments = self.settle({"runs": runs}, repeat=1)
        self.assertEqual(self.merges(calls), [])
        self.assertIn("@o/team", comments)
        self.assertIn("`web-smoke` failure", comments)
        self.assertIn(f"keel-repin-red:{HEAD} ", comments)
        # The same condition seen again produces no second comment.
        with tempfile.TemporaryDirectory() as d:
            base = pathlib.Path(d)
            gh = base / "gh"
            gh.write_text(FAKE_SETTLE_GH)
            gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
            (base / "scenario.json").write_text(json.dumps({"prs": [[5, TAG_BRANCH, HEAD, "false", "2027-01-15T07:00:00Z"]], "emails": [BOT], "runs": runs}))
            env = {"KEEL_REPIN_GH": str(gh), "FAKE_DIR": str(base), "GITHUB_REPOSITORY": "o/title"}
            now = str(keel_repin.calendar.timegm((2027, 1, 15, 7, 10, 0)))
            with mock.patch.dict(os.environ, env), mock.patch("builtins.print"):
                for _ in range(3):
                    keel_repin.main(["settle", "--now", now])
            self.assertEqual((base / "comments.txt").read_text().count(keel_repin.RED_MARKER), 1)

    def test_any_failed_check_on_the_head_is_red_even_when_not_required(self):
        rcs, calls, comments = self.settle({"runs": [check("ci-ok"), check("web-smoke"), check("lint", conclusion="failure")]})
        self.assertEqual((self.merges(calls), "`lint` failure" in comments), ([], True))

    def test_a_check_posted_by_another_app_does_not_count(self):
        rcs, calls, comments = self.settle({"runs": [check("ci-ok"), check("web-smoke", app="some-other-app")]})
        self.assertEqual((self.merges(calls), comments), ([], ""))

    def test_the_newest_run_of_a_name_decides(self):
        runs = [check("ci-ok"), check("web-smoke", conclusion="failure", id=1), check("web-smoke", id=2)]
        self.assertEqual(len(self.merges(self.settle({"runs": runs})[1])), 1)
        runs = [check("ci-ok"), check("web-smoke", id=1), check("web-smoke", conclusion="failure", id=2)]
        self.assertEqual(self.merges(self.settle({"runs": runs})[1]), [])

    def test_a_pending_check_that_never_finishes_turns_red(self):
        rcs, calls, comments = self.settle({"runs": [check("ci-ok")]}, extra=["--max-wait-minutes", "5"])
        self.assertEqual(self.merges(calls), [])
        self.assertIn("still waiting after 10 minutes", comments)
        self.assertIn("`web-smoke` has not run", comments)

    def test_only_the_bots_own_commits_are_merged(self):
        runs = [check("ci-ok"), check("web-smoke")]
        rcs, calls, comments = self.settle({"runs": runs, "emails": [BOT, "person@example.com"]})
        self.assertEqual((self.merges(calls), comments), ([], ""))

    def test_a_draft_is_never_merged_and_told_once(self):
        draft = [[5, TAG_BRANCH, HEAD, "true", "2027-01-15T07:00:00Z"]]
        rcs, calls, comments = self.settle({"prs": draft, "runs": [check("ci-ok"), check("web-smoke")]})
        self.assertEqual(self.merges(calls), [])
        self.assertIn("is a draft", comments)

    def test_other_pull_requests_are_not_listed_for_merge(self):
        other = [[6, "feature/x", HEAD, "false", "2027-01-15T07:00:00Z"]]
        # The branch filter is in the gh --jq expression; the fake answers its rows as given, so check the filter text.
        rcs, calls, comments = self.settle({"prs": [], "runs": []})
        self.assertIn('startswith("chore/keel-repin-")', calls)

    def test_a_queue_repository_retries_with_auto_and_a_refusal_is_told(self):
        runs = [check("ci-ok"), check("web-smoke")]
        rcs, calls, comments = self.settle({"runs": runs, "merge": "needs-auto"})
        self.assertEqual(len(self.merges(calls)), 2)
        self.assertTrue(self.merges(calls)[1].endswith("--auto"))
        rcs, calls, comments = self.settle({"runs": runs, "merge": "refuse"})
        self.assertIn("the merge was refused", comments)

    def test_after_merge_workflows_start_only_when_the_merge_landed(self):
        runs = [check("ci-ok"), check("web-smoke")]
        rcs, calls, _ = self.settle({"runs": runs, "state": "MERGED"}, extra=["--after-merge", "pages.yml deploy.yml", "--base", "main"])
        self.assertIn("workflow run pages.yml --ref main", calls)
        self.assertIn("workflow run deploy.yml --ref main", calls)
        rcs, calls, _ = self.settle({"runs": runs, "state": "OPEN"}, extra=["--after-merge", "pages.yml"])
        self.assertNotIn("workflow run", calls)

    def test_dry_run_merges_nothing(self):
        rcs, calls, _ = self.settle({"runs": [check("ci-ok"), check("web-smoke")]}, extra=["--dry-run"])
        self.assertEqual(self.merges(calls), [])


LIVE_URL = "https://title.example.invalid"
PINNED = "c" * 40
MERGED_PR = {"number": 4, "headRefName": TAG_BRANCH, "mergedAt": "2027-01-15T05:00:00Z",
             "body": f"Moves the Keel pin to `{NEW_TAG}`, commit `{PINNED}`.\n"}


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def serving(body):
    """An urlopen stand-in for the live host: VERSION.json answers BODY (bytes), or the request fails."""
    def opener(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req
        assert url == LIVE_URL + "/VERSION.json", url
        if isinstance(body, Exception):
            raise body
        return FakeResponse(body)
    return opener


class SettleLiveTest(unittest.TestCase):
    """A merged repin is settled only when the live VERSION.json keel field is the merged commit."""

    def settle(self, body, merged=MERGED_PR, repeat=1, grace="60", minutes_after_merge=120):
        with tempfile.TemporaryDirectory() as d:
            base = pathlib.Path(d)
            gh = base / "gh"
            gh.write_text(FAKE_SETTLE_GH)
            gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
            (base / "scenario.json").write_text(json.dumps({"prs": [], "emails": [BOT], "runs": [], "merged": merged}))
            env = {"KEEL_REPIN_GH": str(gh), "FAKE_DIR": str(base), "GITHUB_REPOSITORY": "o/title"}
            now = keel_repin.calendar.timegm((2027, 1, 15, 5, 0, 0)) + minutes_after_merge * 60
            ws = keel_repin.web_smoke()
            real = ws.live_keel
            ws.live_keel = lambda url: real(url, opener=serving(body))
            rcs = []
            with mock.patch.dict(os.environ, env), mock.patch("builtins.print"), \
                    mock.patch.object(keel_repin, "web_smoke", return_value=ws):
                for _ in range(repeat):
                    rcs.append(keel_repin.main(["settle", "--owner", "@o/team", "--now", str(now),
                                                "--live-url", LIVE_URL, "--live-grace-minutes", grace]))
            comments = (base / "comments.txt").read_text() if (base / "comments.txt").exists() else ""
            return rcs, comments

    def version(self, **fields):
        return json.dumps({"version": "1.0.0", "commit": "f" * 40, "built_at": "2027-01-15T05:10:00Z", **fields}).encode()

    def test_the_live_title_on_the_merged_commit_is_settled(self):
        rcs, comments = self.settle(self.version(keel=PINNED))
        self.assertEqual((rcs, comments), ([0], ""))

    def test_a_mismatching_keel_field_fails_and_the_owner_hears_once(self):
        rcs, comments = self.settle(self.version(keel="d" * 40), repeat=3)
        self.assertEqual(rcs, [1, 1, 1])
        self.assertEqual(comments.count(keel_repin.LIVE_MARKER), 1)
        self.assertIn("@o/team", comments)
        self.assertIn("runs Keel dddddddddddd, not the pinned cccccccccccc", comments)

    def test_a_missing_keel_field_fails(self):
        for body in (self.version(), self.version(keel="unknown"), b"<html>not json</html>",
                     urllib.error.URLError("connection refused")):
            rcs, comments = self.settle(body)
            self.assertEqual(rcs, [1], body)
            self.assertIn("does not say which Keel it runs", comments)

    def test_within_the_deploy_grace_a_mismatch_waits(self):
        rcs, comments = self.settle(self.version(keel="d" * 40), minutes_after_merge=10)
        self.assertEqual((rcs, comments), ([0], ""))

    def test_no_merged_repin_compares_nothing(self):
        rcs, comments = self.settle(self.version(), merged=None)
        self.assertEqual((rcs, comments), ([0], ""))

    def test_no_live_url_reads_no_merged_pull_request(self):
        with tempfile.TemporaryDirectory() as d:
            base = pathlib.Path(d)
            gh = base / "gh"
            gh.write_text(FAKE_SETTLE_GH)
            gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
            (base / "scenario.json").write_text(json.dumps({"prs": [], "emails": [BOT], "runs": [], "merged": MERGED_PR}))
            env = {"KEEL_REPIN_GH": str(gh), "FAKE_DIR": str(base), "GITHUB_REPOSITORY": "o/title"}
            with mock.patch.dict(os.environ, env), mock.patch("builtins.print"):
                self.assertEqual(keel_repin.main(["settle"]), 0)
            self.assertNotIn("merged", (base / "gh-calls.txt").read_text())


class ActionContractTest(unittest.TestCase):
    def test_manifest_and_guards(self):
        import yaml
        action = yaml.safe_load((ACTION / "action.yml").read_text())
        self.assertEqual(action["runs"]["using"], "composite")
        text = (ACTION / "action.yml").read_text()
        self.assertNotRegex(text, r"(?m)^\s*runs-on:")  # a composite action takes its runner from the caller
        self.assertNotRegex(text, r"(ubuntu|windows|macos)-(latest|\d)")
        for need in ("mode", "tag", "reader-app-id", "reader-app-key", "dry-run", "check-command", "ci-workflows",
                     "required-checks", "owner", "max-wait-minutes", "after-merge-workflows", "live-url",
                     "live-grace-minutes"):
            self.assertIn(need, action["inputs"])
        for step in action["runs"]["steps"]:
            uses = step.get("uses", "")
            if uses:
                self.assertRegex(uses, r"@[0-9a-f]{40}", uses)  # actions pin by commit

    def test_no_approve_or_review_call_and_minimal_permissions(self):
        import yaml
        sources = [ACTION / "action.yml", ACTION / "keel_repin.py", ROOT / "workflow-templates" / "keel-repin.yml"]
        for path in sources:
            text = path.read_text()
            self.assertNotRegex(text, r"(?i)approv|review|/reviews|pr review", path.name)
        wf = yaml.safe_load((ROOT / "workflow-templates" / "keel-repin.yml").read_text())
        self.assertEqual(wf["permissions"], {})
        allowed = {"contents": {"read", "write"}, "pull-requests": {"read", "write"}, "actions": {"write"}, "checks": {"read"}}
        for name, job in wf["jobs"].items():
            for scope, level in job["permissions"].items():
                self.assertIn(level, allowed.get(scope, set()), f"{name}: {scope}: {level}")
        self.assertEqual(wf["jobs"]["poll"]["permissions"], {"contents": "read", "pull-requests": "read"})
        self.assertEqual(wf["jobs"]["repin"]["permissions"], {"contents": "write", "pull-requests": "write", "actions": "write"})
        self.assertEqual(wf["jobs"]["settle"]["permissions"], {"contents": "write", "pull-requests": "write", "checks": "read", "actions": "write"})
        # A cadence that meets "a repin pull request within 15 minutes of a tag".
        self.assertEqual(wf[True]["schedule"], [{"cron": "*/10 * * * *"}])

    def test_template_runs_on_our_runners_only(self):
        text = (ROOT / "workflow-templates" / "keel-repin.yml").read_text()
        labels = re.findall(r"(?m)^\s*runs-on:\s*(.+)$", text)
        self.assertEqual(labels, ["sylphx-linux-standard"] * 3)
        self.assertNotRegex(text, r"(ubuntu|windows|macos)-(latest|slim|\d)")

    def test_the_only_merge_is_pinned_to_the_judged_head(self):
        text = (ACTION / "keel_repin.py").read_text()
        calls = re.findall(r'"pr",\s*"(\w+)"', text)
        self.assertEqual(sorted(set(calls)), ["close", "comment", "create", "list", "merge", "view"])
        self.assertEqual(text.count('"merge"'), 1)
        self.assertIn('"--match-head-commit", sha', text)  # a push after the verdict voids the merge
        self.assertNotIn("update-branch", text)
        self.assertNotIn("--admin", text)  # no bypass merge

    def test_the_template_settles_on_the_aggregate_and_the_web_smoke(self):
        import yaml
        wf = yaml.safe_load((ROOT / "workflow-templates" / "keel-repin.yml").read_text())
        step = wf["jobs"]["settle"]["steps"][-1]
        self.assertEqual(step["with"]["mode"], "settle")
        self.assertEqual(step["with"]["required-checks"].split(), ["ci-ok", "web-smoke"])


if __name__ == "__main__":
    unittest.main()
