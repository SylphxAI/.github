#!/usr/bin/env python3
"""Tests for the keel-repin action: pin discovery, rewrite, the build check and the pull request."""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import re
import stat
import subprocess
import tempfile
import unittest
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
        title, body = keel_repin.pr_text(self.report(), writer=True)
        self.assertEqual(title, f"chore(keel): repin Keel to {NEW_TAG} ({'b' * 9})")
        self.assertIn("build check passed", body)
        self.assertNotIn("does not start other workflows", body)
        title, body = keel_repin.pr_text(self.report("failed", "check"), writer=False)
        self.assertIn("Draft: the build check fails", body)
        self.assertIn("SavePort", body)
        self.assertEqual(body.count("```"), 2)  # the log cannot close its own fence
        self.assertIn("close and reopen", body)

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
            env = {"KEEL_REPIN_GH": str(gh), "GH_TOKEN": "tok", "KEEL_REPIN_WRITER": "app"}
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


class ActionContractTest(unittest.TestCase):
    def test_manifest_and_guards(self):
        import yaml
        action = yaml.safe_load((ACTION / "action.yml").read_text())
        self.assertEqual(action["runs"]["using"], "composite")
        text = (ACTION / "action.yml").read_text()
        self.assertNotRegex(text, r"(?m)^\s*runs-on:")  # a composite action takes its runner from the caller
        self.assertNotRegex(text, r"(ubuntu|windows|macos)-(latest|\d)")
        self.assertNotRegex(text, r"gh pr merge|--auto\b|enableAutoMerge")
        for need in ("tag", "reader-app-id", "reader-app-key", "writer-app-id", "writer-app-key", "dry-run", "check-command"):
            self.assertIn(need, action["inputs"])
        for step in action["runs"]["steps"]:
            uses = step.get("uses", "")
            if uses:
                self.assertRegex(uses, r"@[0-9a-f]{40}", uses)  # actions pin by commit

    def test_template_runs_on_our_runners_only(self):
        text = (ROOT / "workflow-templates" / "keel-repin.yml").read_text()
        labels = re.findall(r"(?m)^\s*runs-on:\s*(.+)$", text)
        self.assertEqual(labels, ["sylphx-linux-standard"])
        self.assertNotRegex(text, r"(ubuntu|windows|macos)-(latest|slim|\d)")

    def test_the_script_has_no_merge_call(self):
        calls = re.findall(r'"pr",\s*"(\w+)"', (ACTION / "keel_repin.py").read_text())
        self.assertEqual(sorted(set(calls)), ["close", "create", "list"])


if __name__ == "__main__":
    unittest.main()
