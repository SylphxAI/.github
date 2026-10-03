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
        for answer, expect in (("behind", "not ahead"), ("diverged", "not ahead")):
            with tempfile.TemporaryDirectory() as d:
                base = pathlib.Path(d)
                remote, sha = make_keel_remote(base)
                repo = make_title(base, TITLE)
                rc, out = self.poll(repo, remote, {"api repos/SylphxAI/keel/compare/" + OLD + "..." + sha: answer, "pr list": "0"})
                self.assertEqual(out["needed"], "false")
                self.assertIn(expect, out["reason"])
        with tempfile.TemporaryDirectory() as d:  # a closed or merged pull request for the tag is never reopened
            base = pathlib.Path(d)
            remote, sha = make_keel_remote(base)
            repo = make_title(base, TITLE)
            rc, out = self.poll(repo, remote, {"pr list": "1"})
            self.assertEqual((out["needed"], "exists" in out["reason"]), ("false", True))

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


class ActionContractTest(unittest.TestCase):
    def test_manifest_and_guards(self):
        import yaml
        action = yaml.safe_load((ACTION / "action.yml").read_text())
        self.assertEqual(action["runs"]["using"], "composite")
        text = (ACTION / "action.yml").read_text()
        self.assertNotRegex(text, r"(?m)^\s*runs-on:")  # a composite action takes its runner from the caller
        self.assertNotRegex(text, r"(ubuntu|windows|macos)-(latest|\d)")
        self.assertNotRegex(text, r"gh pr merge|--auto\b|enableAutoMerge")
        for need in ("mode", "tag", "reader-app-id", "reader-app-key", "dry-run", "check-command", "ci-workflows"):
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
            self.assertNotRegex(text, r"gh pr merge|enableAutoMerge|--auto\b", path.name)
        wf = yaml.safe_load((ROOT / "workflow-templates" / "keel-repin.yml").read_text())
        self.assertEqual(wf["permissions"], {})
        allowed = {"contents": {"read", "write"}, "pull-requests": {"read", "write"}, "actions": {"write"}}
        for name, job in wf["jobs"].items():
            for scope, level in job["permissions"].items():
                self.assertIn(level, allowed.get(scope, set()), f"{name}: {scope}: {level}")
        self.assertEqual(wf["jobs"]["poll"]["permissions"], {"contents": "read", "pull-requests": "read"})
        self.assertEqual(wf["jobs"]["repin"]["permissions"], {"contents": "write", "pull-requests": "write", "actions": "write"})

    def test_template_runs_on_our_runners_only(self):
        text = (ROOT / "workflow-templates" / "keel-repin.yml").read_text()
        labels = re.findall(r"(?m)^\s*runs-on:\s*(.+)$", text)
        self.assertEqual(labels, ["sylphx-linux-standard"] * 2)
        self.assertNotRegex(text, r"(ubuntu|windows|macos)-(latest|slim|\d)")

    def test_the_script_has_no_merge_call(self):
        calls = re.findall(r'"pr",\s*"(\w+)"', (ACTION / "keel_repin.py").read_text())
        self.assertEqual(sorted(set(calls)), ["close", "create", "list"])


if __name__ == "__main__":
    unittest.main()
