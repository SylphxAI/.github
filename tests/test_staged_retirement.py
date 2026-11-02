"""Published checker adoption and source-bound retirement counterexamples."""
import hashlib
import pathlib
import subprocess
import unittest
from unittest import mock

from test_rule_adoption import Fixture, OLD, UNKNOWN, ROOT, TODAY, WORKFLOW, sc, workflow
import tempfile

r = sc.staged_retirement
PROJECT = {"legacy/build.settings": "settings = {}\n", "legacy/main.lua": "print('existing')\n",
           "legacy/scene.lua": "return {}\n", "legacy/icon.bin": "binary\u0000asset"}


class StagedRetirementTests(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Fixture(temporary.name)

    def adopted(self):
        f = self.fixture()
        base = f.commit({WORKFLOW: workflow(OLD), sc.BASELINE: "# existing ratchet\n", **PROJECT})
        f.write({WORKFLOW: workflow("e54a2255249c20995d23c3b3fc24363d0865fbbf")})
        f.git("add", ".")
        result = f.plan(base, f.git("write-tree"))
        adopted = f.commit({sc.BASELINE: result["baseline_text"]})
        f.check(base, 0)
        return f, adopted, result

    def test_introduced_category_is_source_bound_and_stays_visible(self):
        f, adopted, result = self.adopted()
        self.assertIn(r.RULE, result["introduced_rules"])
        line, = [line for line in result["evidence"]["added_records"] if line.startswith("solar2d ")]
        self.assertRegex(line, r"^solar2d legacy/build.settings [a-f0-9]{64}$")
        _, report = f.check(adopted, 0)
        self.assertIn("solar2d legacy/build.settings", report["legacy_departures"])
        f.check("", 0)

    def test_new_project_or_added_source_cannot_be_seeded(self):
        for changed in ({"other/build.settings": "settings = {}", "other/main.lua": "return {}"},
                        {"legacy/new.lua": "return {}"}, {"legacy/icon2.png": "new binary"},
                        {"legacy/scene.lua": "return 'changed'"}):
            with self.subTest(changed=tuple(changed)):
                f = self.fixture()
                base = f.commit({WORKFLOW: workflow(OLD), sc.BASELINE: "# baseline\n", **PROJECT})
                head = f.commit(changed)
                with self.assertRaisesRegex(ValueError, "retired project cannot be adopted"):
                    f.plan(base, head)

    def test_changed_inputs_additions_renames_and_modes_fail_after_adoption(self):
        for kind in ("bytes", "add", "rename", "mode", "symlink"):
            with self.subTest(kind=kind):
                f, adopted, _ = self.adopted()
                if kind == "bytes":
                    f.write({"legacy/scene.lua": "return 'changed'"})
                elif kind == "add":
                    f.write({"legacy/new.lua": "return {}"})
                elif kind == "rename":
                    f.git("mv", "legacy/scene.lua", "legacy/renamed.lua")
                elif kind == "mode":
                    (f.root / "legacy/main.lua").chmod(0o755)
                else:
                    (f.root / "legacy/link").symlink_to("scene.lua")
                # A caller recomputing a valid head digest still cannot expand debt.
                f.git("add", ".")
                candidate = sc.Tree(str(f.root), f.git("write-tree"))
                old = (f.root / sc.BASELINE).read_text()
                old_line = next(line for line in old.splitlines() if line.startswith("solar2d "))
                f.commit({sc.BASELINE: old.replace(old_line, r.binding(candidate, "legacy/build.settings", sc.BASELINE))})
                output, _ = f.check(adopted, 1)
                self.assertIn("retired project inputs changed or expanded", output)

    def test_outside_project_change_and_identical_binary_inputs_pass(self):
        f, adopted, _ = self.adopted()
        f.commit({"README.md": "route maintained source here\n", "modern/new.rs": "fn main() {}\n"})
        f.check(adopted, 0)

    def test_deletion_only_refresh_and_complete_retirement(self):
        f, adopted, _ = self.adopted()
        f.git("rm", "legacy/icon.bin")
        candidate = f.git("write-tree")
        result = f.plan(adopted, candidate)
        f.commit({sc.BASELINE: result["baseline_text"]})
        f.check(adopted, 0)
        previous = f.git("rev-parse", "HEAD")
        f.git("rm", "-r", "legacy")
        result = f.plan(previous, f.git("write-tree"))
        self.assertNotIn("solar2d legacy/", result["baseline_text"])
        self.assertIn(sc.adoption.RULE_MARKER + r.RULE, result["baseline_text"])
        f.commit({sc.BASELINE: result["baseline_text"]})
        f.check(previous, 0)

    def test_plain_record_forged_digest_missing_marker_and_stale_binding_fail(self):
        for kind in ("plain", "digest", "marker", "stale"):
            with self.subTest(kind=kind):
                f, adopted, result = self.adopted()
                text = result["baseline_text"]
                line = next(line for line in text.splitlines() if line.startswith("solar2d "))
                if kind == "plain":
                    text = text.replace(line, "solar2d legacy/build.settings")
                elif kind == "digest":
                    text = text.replace(line, "solar2d legacy/build.settings " + "0" * 64)
                elif kind == "marker":
                    text = text.replace(sc.adoption.RULE_MARKER + r.RULE + "\n", "")
                else:
                    f.git("rm", "-r", "legacy")
                f.commit({sc.BASELINE: text})
                f.check(adopted, 1)

    def test_anchor_deletion_keeps_binding_and_visible_finding_until_all_inputs_are_gone(self):
        for anchor in ("legacy/build.settings", "legacy/main.lua"):
            with self.subTest(anchor=anchor):
                f, adopted, _ = self.adopted()
                f.git("rm", anchor)
                result = f.plan(adopted, f.git("write-tree"))
                self.assertIn("solar2d legacy/build.settings ", result["baseline_text"])
                previous = f.commit({sc.BASELINE: result["baseline_text"]})
                _, report = f.check(adopted, 0)
                self.assertIn("solar2d legacy/build.settings", report["legacy_departures"])
                f.check("", 0)
                f.git("rm", "legacy/icon.bin")
                result = f.plan(previous, f.git("write-tree"))
                previous_base = previous
                previous = f.commit({sc.BASELINE: result["baseline_text"]})
                f.check(previous_base, 0)
                f.git("rm", "-r", "legacy")
                result = f.plan(previous, f.git("write-tree"))
                self.assertNotIn("solar2d legacy/", result["baseline_text"])
                f.commit({sc.BASELINE: result["baseline_text"]})
                f.check(previous, 0)

    def test_anchor_deletion_with_change_or_addition_cannot_be_adopted_or_manually_unbound(self):
        for changed in ({}, {"legacy/scene.lua": "return 'changed'"},
                        {"legacy/new-render.lua": "return {}"}):
            for anchor in ("legacy/build.settings", "legacy/main.lua"):
                with self.subTest(changed=changed, anchor=anchor):
                    f, adopted, original = self.adopted()
                    f.git("rm", anchor)
                    f.write(changed)
                    f.git("add", ".")
                    if changed:
                        with self.assertRaisesRegex(ValueError, "retired project inputs changed or expanded"):
                            f.plan(adopted, f.git("write-tree"))
                    text = original["baseline_text"]
                    line = next(line for line in text.splitlines() if line.startswith("solar2d "))
                    f.commit({sc.BASELINE: text.replace(line + "\n", "")})
                    output, report = f.check(adopted, 1)
                    self.assertIn("retained retired project inputs require their source binding", output)
                    self.assertIn("solar2d legacy/build.settings", report["legacy_departures"])

    def test_complete_retirement_cannot_replace_entire_subtree_without_binding(self):
        f, adopted, original = self.adopted()
        f.git("rm", "-r", "legacy")
        f.write({"legacy/modern.rs": "fn main() {}"})
        f.git("add", ".")
        with self.assertRaisesRegex(ValueError, "retired project inputs changed or expanded"):
            f.plan(adopted, f.git("write-tree"))
        line = next(line for line in original["baseline_text"].splitlines() if line.startswith("solar2d "))
        f.commit({sc.BASELINE: original["baseline_text"].replace(line + "\n", "")})
        f.check(adopted, 1)

    def test_first_baseline_cannot_manually_seed_plain_solar(self):
        f = self.fixture()
        base = f.commit({WORKFLOW: workflow(OLD), **PROJECT})
        f.commit({sc.BASELINE: "solar2d legacy/build.settings\n"})
        output, _ = f.check(base, 1)
        self.assertIn("source-bound adoption record", output)

    def test_unknown_category_and_marker_removal_remain_refused(self):
        f, adopted, result = self.adopted()
        f.commit({sc.BASELINE: result["baseline_text"] + sc.adoption.RULE_MARKER + "legacy/unknown\n"})
        output, _ = f.check(adopted, 1)
        self.assertIn("unknown rule adoption marker", output)

    def test_old_solar_recognizer_prevents_category_rename_reset(self):
        pin = "a9569436f04155e37cf6c5a93feb370ab7903d15"
        tree = sc.Tree(str(ROOT), pin)
        profile = {"commit": pin, **sc.adoption.source_profile(tree)}
        self.assertEqual(sc.adoption.legacy_rules(profile, shared_repo=str(ROOT)), {r.RULE})
        f = self.fixture()
        base = f.commit({WORKFLOW: workflow(pin), sc.BASELINE: "# old checker already active\n", **PROJECT})
        with mock.patch.object(sc.adoption, "old_checkers", return_value=[profile]):
            introduced, _ = sc.adoption.introduced_rules(sc.Tree(str(f.root), base), "# old checker already active\n",
                                                       shared_repo=str(ROOT))
        self.assertNotIn(r.RULE, introduced)
        profile["sources"][sc.adoption.PREFIX + "stack_conformance.py"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "provenance mismatch"):
            sc.adoption.legacy_rules(profile, shared_repo=str(ROOT))

    def test_root_project_is_conservative_not_guessed_source_extensions(self):
        tree = sc.DictTree({"build.settings": "settings = {}", "main.lua": "return {}",
                            "arbitrary.dat": "bytes", sc.BASELINE: "baseline one"})
        before = r.inputs(tree, "build.settings", sc.BASELINE)
        self.assertIn("arbitrary.dat", before)
        tree.files[sc.BASELINE] = "baseline two"
        self.assertEqual(before, r.inputs(tree, "build.settings", sc.BASELINE))
        tree.files["README.md"] = "tracked root input"
        self.assertFalse(r.subset(before, r.inputs(tree, "build.settings", sc.BASELINE)))

    def test_prefixed_view_preserves_git_identity_and_scope(self):
        f, _, _ = self.adopted()
        source = sc.Tree(str(f.root), "HEAD")
        view = sc.source_provenance.TreeView(source, "legacy")
        direct = r.inputs(source, "legacy/build.settings", sc.BASELINE)
        nested = r.inputs(view, "build.settings", sc.BASELINE)
        self.assertEqual(direct, nested)
        self.assertEqual(direct["main.lua"], source.entries()["legacy/main.lua"])


if __name__ == "__main__":
    unittest.main()
