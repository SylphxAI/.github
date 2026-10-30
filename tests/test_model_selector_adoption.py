"""Separately adopt selector coverage through the released checker and CLI."""
import tempfile
import unittest

from test_rule_adoption import Fixture, ROOT, UNKNOWN, WORKFLOW, a, sc, workflow


RELEASED = "96195d86b550ab1205159acb1ae0d75b1fbed47f"
SELECTOR = "src/default.ts"
SELECTED = 'const model = "new-vendor/future-line";\n'
LEGACY = "src/legacy.ts"
PINNED = 'const legacy = "gpt-5.1-2025-11-13";\n'


class SelectorAdoptionTests(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Fixture(temporary.name)

    def test_actual_predecessor_enforces_old_models_but_not_selectors(self):
        profile = a.old_checkers(sc.DictTree({WORKFLOW: workflow(RELEASED)}), shared_repo=str(ROOT))[0]
        self.assertIn("model-pin", profile["rules"])
        self.assertNotIn("model-selector", profile["rules"])
        self.assertEqual(a.profile_id(a.source_profile(sc.Tree(str(ROOT), RELEASED))),
                         "1b74b8047e9c95f701e867f097553d20e704d309d021fdc73412274a9ebb74a8")
        self.assertIn("model-selector", a.current_rules())

    def test_coverage_requires_a_literal_declaration_and_checker_invocation(self):
        files = {a.PREFIX + "action.yml": "python stack_conformance.py",
                 a.PREFIX + "stack_conformance.py": "def cmd_check(args):\n    model_pins.decide()\n",
                 a.PREFIX + "model_pins.py":
                 'raise RuntimeError("never import historical code")\nSELECTOR_RULE = "model-selector"\n'}
        self.assertEqual(a.source_profile(sc.DictTree(files))["rules"], ["model-pin", "model-selector"])
        files[a.PREFIX + "stack_conformance.py"] = "def cmd_check(args):\n    return 0\n"
        self.assertEqual(a.source_profile(sc.DictTree(files))["rules"], [])
        self.assertEqual(a.model_rules("def decide(): pass\n"), {"model-pin"})
        with self.assertRaisesRegex(ValueError, "unrecognized model selector"):
            a.model_rules("SELECTOR_RULE = compute()\n")

    def test_inherited_selector_seed_preserves_old_debt_then_ratchets(self):
        f = self.fixture()
        old_row = sc.model_pins.scan(sc.DictTree({LEGACY: PINNED}), "example/app", None,
                                     sc.architecture.source_views)["findings"][0]
        old_record = sc.model_pins.baseline_key(old_row)
        baseline = sc.model_pins.BASELINE_MARKER + "\n" + old_record + "\n"
        base = f.commit({WORKFLOW: workflow(RELEASED), SELECTOR: SELECTED, LEGACY: PINNED,
                         sc.BASELINE: baseline})
        result = f.plan(base)
        self.assertEqual(result["introduced_rules"], ["model-selector"])
        self.assertTrue(result["baseline_text"].startswith(baseline))
        self.assertEqual(len(result["evidence"]["added_records"]), 1)
        self.assertIn("new-vendor/future-line", result["evidence"]["added_records"][0])
        adopted = f.commit({WORKFLOW: workflow(UNKNOWN), sc.BASELINE: result["baseline_text"]})
        f.check(base, 0)
        f.check("", 0)

        # A second selector is new debt even if its author appends its key.
        source = SELECTED + 'const otherModel = "other-vendor/future-line";\n'
        rows = sc.model_pins.scan(sc.DictTree({SELECTOR: source}), "example/app", None,
                                 sc.architecture.source_views)["findings"]
        extra = next(sc.model_pins.baseline_key(row) for row in rows
                     if row["model"] == "other-vendor/future-line")
        f.commit({SELECTOR: source, sc.BASELINE: result["baseline_text"] + extra + "\n"})
        output, _ = f.check(adopted, 1)
        self.assertIn("model-pin baseline cannot grow", output)

        # Removing the capability marker cannot reopen inherited seeding.
        f.commit({SELECTOR: SELECTED, sc.BASELINE: result["baseline_text"].replace(
            a.RULE_MARKER + "model-selector\n", "")})
        output, _ = f.check(adopted, 1)
        self.assertIn("rule adoption marker cannot be removed: model-selector", output)

    def test_old_model_debt_does_not_gain_a_seed_permission(self):
        f = self.fixture()
        base = f.commit({WORKFLOW: workflow(RELEASED), SELECTOR: SELECTED, LEGACY: PINNED,
                         sc.BASELINE: sc.model_pins.BASELINE_MARKER + "\n"})
        result = f.plan(base)
        self.assertEqual(result["introduced_rules"], ["model-selector"])
        self.assertFalse(any("gpt-5.1" in row for row in result["evidence"]["added_records"]))
        f.commit({WORKFLOW: workflow(UNKNOWN), sc.BASELINE: result["baseline_text"]})
        output, _ = f.check(base, 1)
        self.assertIn("src/legacy.ts:1: vendor model id", output)

    def test_new_head_selector_cannot_be_seeded(self):
        f = self.fixture()
        base = f.commit({WORKFLOW: workflow(RELEASED), SELECTOR: SELECTED})
        f.commit({"src/new.ts": SELECTED})
        with self.assertRaisesRegex(ValueError, "new head debt cannot be adopted"):
            f.plan(base)

    def test_selector_adoption_adds_the_required_shared_model_marker(self):
        f = self.fixture()
        base = f.commit({WORKFLOW: workflow(RELEASED), SELECTOR: SELECTED})
        result = f.plan(base)
        self.assertIn(sc.model_pins.BASELINE_MARKER, result["baseline_text"].splitlines())
        f.commit({WORKFLOW: workflow(UNKNOWN), sc.BASELINE: result["baseline_text"]})
        f.check(base, 0)


if __name__ == "__main__":
    unittest.main()
