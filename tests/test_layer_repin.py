"""Offline shared-layer fixtures: real git remotes, Cargo locks, and title hooks."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location("repin_fixtures", Path(__file__).with_name("test_keel_repin.py"))
fixtures = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixtures)
repin = fixtures.keel_repin
git = fixtures.git


def source(base, name, files):
    root = base / name
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "initial")
    return root, git(root, "rev-parse", "HEAD")


def advance(root, file, text):
    (root / file).write_text(text)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "advance main")
    return git(root, "rev-parse", "HEAD")


class LayerTest(unittest.TestCase):
    def poll(self, base, title, layer, remote):
        output = base / "outputs"
        output.write_text("")
        with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}):
            self.assertEqual(repin.main(["poll", "--layer", layer, "--root", str(title), "--remote", str(remote)]), 0)
        return dict(line.split("=", 1) for line in output.read_text().splitlines())

    def run_pr(self, base, title, layer, remote, check="true"):
        report = base / "report.json"
        self.assertEqual(repin.main(["run", "--layer", layer, "--root", str(title), "--remote", str(remote),
                                    "--check-command", check, "--owner", "@Cubeage/studio", "--report", str(report)]), 0)
        self.assertEqual(repin.main(["pr", "--root", str(title), "--report", str(report)]), 0)
        return json.loads(report.read_text())

    @unittest.skipUnless(shutil.which("cargo"), "needs cargo")
    def test_kit_main_moves_kit_and_keel_as_one_tuple_with_one_lock_source(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            keel, old_keel = source(base, "keel", {
                "Cargo.toml": '[package]\nname = "keel-net"\nversion = "0.1.0"\nedition = "2021"\n',
                "src/lib.rs": "// old\n"})
            new_keel = advance(keel, "src/lib.rs", "// new\n")
            kit_manifest = ('[package]\nname = "cubeage-kit"\nversion = "0.1.0"\nedition = "2021"\n'
                            '[dependencies]\nkeel-net = { git = "https://github.com/SylphxAI/keel", rev = "%s" }\n')
            kit, old_kit = source(base, "kit", {"Cargo.toml": kit_manifest % old_keel, "src/lib.rs": ""})
            new_kit = advance(kit, "Cargo.toml", kit_manifest % new_keel)
            title = fixtures.make_title(base, {
                "Cargo.toml": ('[package]\nname = "title"\nversion = "0.1.0"\nedition = "2021"\n[dependencies]\n'
                               f'cubeage-kit = {{ git = "https://github.com/Cubeage/cubeage-kit", rev = "{old_kit}" }}\n'
                               f'keel-net = {{ git = "https://github.com/SylphxAI/keel", rev = "{old_keel}" }}\n'),
                "deps/kit.rev": old_kit + "\n", "KEEL_PIN": old_keel + "\n", "src/lib.rs": "",
                "vendor/tuple": "old tuple\n",
                "scripts/vendor-private.sh": 'set -eu\nread -r kit < deps/kit.rev\nread -r keel < KEEL_PIN\nprintf "%s %s\\n" "$kit" "$keel" > vendor/tuple\n'})
            gh = fixtures.fake_gh(base, {"label list": "owner:games\n", "pr create": "https://example.invalid/pull/1\n"})
            env = {"GH_TOKEN": "fixture", "KEEL_REPIN_GH": str(gh), "CARGO_HOME": str(base / "cargo"),
                   "CARGO_NET_GIT_FETCH_WITH_CLI": "true", "GIT_CONFIG_COUNT": "2",
                   "GIT_CONFIG_KEY_0": f"url.file://{keel}.insteadOf", "GIT_CONFIG_VALUE_0": "https://github.com/SylphxAI/keel",
                   "GIT_CONFIG_KEY_1": f"url.file://{kit}.insteadOf", "GIT_CONFIG_VALUE_1": "https://github.com/Cubeage/cubeage-kit"}
            with mock.patch.dict(os.environ, env):
                repin.run(["cargo", "generate-lockfile"], cwd=title)
                git(title, "add", "Cargo.lock")
                git(title, "commit", "-qm", "lock")
                self.assertEqual(self.poll(base, title, "kit", kit)["needed"], "true")
                report = self.run_pr(base, title, "kit", kit)
                self.assertEqual(report["status"], "ok", report["log"])
                self.assertIn(new_kit, (title / "Cargo.toml").read_text())
                self.assertIn(new_keel, (title / "Cargo.toml").read_text())
                self.assertEqual((title / "KEEL_PIN").read_text().strip(), new_keel)
                self.assertEqual((title / "vendor/tuple").read_text().strip(), f"{new_kit} {new_keel}")
                sources = {entry[2].split("#")[-1] for entry in repin.lock_entries((title / "Cargo.lock").read_text())}
                self.assertEqual(sources, {new_keel})
                self.assertIn(new_kit, (title / "Cargo.lock").read_text())
                self.assertEqual(self.poll(base, title, "kit", kit)["needed"], "false")
                git(title, "checkout", "-q", "main")
                self.assertEqual(self.poll(base, title, "kit", kit)["needed"], "false")
                self.assertEqual(self.run_pr(base, title, "kit", kit)["status"], "branch-exists")
                self.assertEqual((base / "gh-calls.txt").read_text().count("pr create"), 1)
                newer_kit = advance(kit, "src/lib.rs", "// next kit main\n")
                gh = fixtures.fake_gh(base, {"label list": "owner:games\n", "pr create": "https://example.invalid/pull/3\n",
                                            "pr list": f"1 {repin.BRANCH_PREFIX}kit-{new_kit}\n2 {repin.BRANCH_PREFIX}engine-{new_kit}\n"})
                gh.write_text(gh.read_text().replace('printf "%s"', 'printf "%b"'))
                self.assertEqual(self.run_pr(base, title, "kit", kit)["sha"], newer_kit)
                calls = (base / "gh-calls.txt").read_text()
                self.assertEqual(calls.count("pr create"), 2)
                self.assertIn("pr close 1 --delete-branch", calls)
                self.assertNotIn("pr close 2", calls)

    def test_kit_hook_receives_both_commits_and_wrong_keel_lock_is_a_draft(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            keel_sha = "b" * 40
            kit, old = source(base, "kit", {
                "Cargo.toml": f'keel-net = {{ git = "https://github.com/SylphxAI/keel", rev = "{keel_sha}" }}\n'})
            new = advance(kit, "Cargo.toml", (kit / "Cargo.toml").read_text() + "# new kit\n")
            title = fixtures.make_title(base, {
                "deps/kit.rev": old + "\n",
                "Cargo.lock": ('[[package]]\nname = "keel-net"\nversion = "0.1.0"\n'
                               f'source = "git+https://github.com/SylphxAI/keel?rev={fixtures.OLD}#{fixtures.OLD}"\n'),
                "tools/repin_keel.sh": 'set -eu\nprintf "%s\\n" "$2" > deps/kit.rev\nprintf "%s %s\\n" "$1" "$2" > hook-args\n'})
            report = base / "report.json"
            self.assertEqual(repin.main(["run", "--layer", "kit", "--root", str(title), "--remote", str(kit),
                                        "--check-command", "true", "--report", str(report)]), 0)
            data = json.loads(report.read_text())
            self.assertEqual((data["status"], data["stage"]), ("failed", "lock"))
            self.assertEqual((title / "hook-args").read_text().strip(), f"{keel_sha} {new}")
            self.assertIn("expected one Keel commit", data["log"])
            self.assertEqual(repin.PIN_IN_BODY.search(repin.pr_text(data)[1]).group(1), keel_sha)

    def test_layers_share_exact_head_settle_and_one_red_comment_until_head_changes(self):
        helper = fixtures.SettleTest()
        for layer in ("kit", "engine"):
            prs = [[5, repin.BRANCH_PREFIX + layer + "-" + "a" * 40, fixtures.HEAD, "false", "2027-01-15T07:00:00Z"]]
            _, calls, _ = helper.settle({"prs": prs, "runs": [fixtures.check("ci-ok"), fixtures.check("web-smoke")]})
            self.assertEqual(helper.merges(calls), [f"pr merge 5 --squash --match-head-commit {fixtures.HEAD}"])
            _, calls, comments = helper.settle({"prs": prs, "runs": [fixtures.check("ci-ok", conclusion="failure")]}, repeat=2)
            self.assertEqual(helper.merges(calls), [])
            self.assertEqual(comments.count(f"keel-repin-red:{fixtures.HEAD} "), 1)

    def test_layer_main_behind_or_diverged_does_not_downgrade(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            engine, old = source(base, "engine", {"tree": "old\n"})
            new = advance(engine, "tree", "new\n")
            title = fixtures.make_title(base, {"ENGINE_REV": new + "\n"})
            gh = fixtures.fake_gh(base, {})
            with mock.patch.dict(os.environ, {"KEEL_REPIN_GH": str(gh)}):
                git(engine, "reset", "--hard", old)
                self.assertEqual(self.poll(base, title, "engine", engine)["needed"], "false")
                advance(engine, "tree", "other branch\n")
                self.assertEqual(self.poll(base, title, "engine", engine)["needed"], "false")

    def test_engine_hook_moves_revision_and_vendor_and_red_build_opens_owned_draft(self):
        for hook in ("tools/vendor_engine.sh", "scripts/vendor-private.sh"):
            with self.subTest(hook=hook), tempfile.TemporaryDirectory() as d:
                base = Path(d)
                engine, old = source(base, "engine", {"tree": "old\n"})
                new = advance(engine, "tree", "new\n")
                title = fixtures.make_title(base, {
                    "server/ENGINE_REV": old + "\n", "server/vendor/tycoon-engine/tree": "old\n",
                    "server/Cargo.toml": f'engine = {{ git = "https://github.com/Cubeage/tycoon-engine", rev = "{old}" }}\n',
                    hook: 'set -eu\nrev=$(tr -d "[:space:]" < server/ENGINE_REV)\n'
                          'git -C "$ENGINE_REPO" show "$rev:tree" > server/vendor/tycoon-engine/tree\n'})
                gh = fixtures.fake_gh(base, {"label list": "owner:games\n", "pr create": "https://example.invalid/pull/2\n"})
                with mock.patch.dict(os.environ, {"GH_TOKEN": "fixture", "KEEL_REPIN_GH": str(gh)}):
                    self.assertEqual(self.poll(base, title, "engine", engine)["needed"], "true")
                    report = self.run_pr(base, title, "engine", engine, "echo engine-build-failed >&2; exit 1")
                    self.assertEqual((report["status"], report["stage"]), ("failed", "check"))
                    self.assertEqual((title / "server/ENGINE_REV").read_text().strip(), new)
                    self.assertEqual((title / "server/vendor/tycoon-engine/tree").read_text(), "new\n")
                    self.assertIn(new, (title / "server/Cargo.toml").read_text())
                    calls = (base / "gh-calls.txt").read_text()
                    self.assertEqual(calls.count("pr create"), 1)
                    self.assertIn("--draft", calls)
                    self.assertIn("@Cubeage/studio", calls)
                    self.assertIn("engine-build-failed", calls)
                    self.assertEqual(self.poll(base, title, "engine", engine)["needed"], "false")
                    git(title, "checkout", "-q", "main")
                    self.assertEqual(self.poll(base, title, "engine", engine)["needed"], "false")
                    self.assertEqual(self.run_pr(base, title, "engine", engine)["status"], "branch-exists")
                    self.assertEqual((base / "gh-calls.txt").read_text().count("pr create"), 1)
