"""Force-push debt adoption and refusal through the actual shipped CLIs."""
import tempfile
import unittest

from test_rule_adoption import Fixture, OLD, ROOT, UNKNOWN, WORKFLOW, sc, workflow


SCRIPT = "scripts/publish.sh"
FORCED = "git push --force-with-lease origin HEAD:refs/heads/pin\n"
NORMAL = "git push origin HEAD:refs/heads/pin\n"


class ForcePushAdoptionTests(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Fixture(temporary.name)

    def test_inherited_exact_debt_adopts_then_only_shrinks(self):
        fixture = self.fixture()
        base = fixture.commit({WORKFLOW: workflow(OLD), SCRIPT: FORCED})
        plan = fixture.plan(base)
        self.assertIn("force-push", plan["introduced_rules"])
        self.assertIn(sc.no_force_push.BASELINE_MARKER, plan["baseline_text"])
        records = [r for r in plan["evidence"]["added_records"] if r.startswith("force-push ")]
        self.assertEqual(len(records), 1)
        adopted = fixture.commit({WORKFLOW: workflow(UNKNOWN), sc.BASELINE: plan["baseline_text"]})
        _, report = fixture.check(base, 0)
        self.assertEqual(len(report["force_pushes"]["findings"]), 1)
        self.assertTrue(report["force_pushes"]["findings"][0]["baseline_recorded"])
        fixture.check("", 0)

        fixture.commit({SCRIPT: FORCED + FORCED})
        output, _ = fixture.check(adopted, 1)
        self.assertIn("force push is forbidden", output)
        fixture.commit({SCRIPT: NORMAL})
        output, _ = fixture.check(adopted, 1)
        self.assertIn("stale force-push baseline", output)
        shrunk = "\n".join(line for line in plan["baseline_text"].splitlines()
                           if not line.startswith("force-push ")) + "\n"
        fixture.commit({sc.BASELINE: shrunk})
        fixture.check(adopted, 0)

    def test_new_command_cannot_seed_during_pin_adoption(self):
        fixture = self.fixture()
        base = fixture.commit({WORKFLOW: workflow(OLD), SCRIPT: NORMAL})
        fixture.commit({SCRIPT: FORCED})
        with self.assertRaisesRegex(ValueError, "new head debt"):
            fixture.plan(base)

    def test_no_baseline_refuses_new_workflow_branch_write_and_accepts_tag(self):
        fixture = self.fixture()
        base = fixture.commit({WORKFLOW: workflow(OLD), SCRIPT: NORMAL})
        fixture.commit({SCRIPT: "git push -f origin refs/tags/v1\n"})
        fixture.check(base, 0)
        fixture.commit({SCRIPT: NORMAL,
            ".github/workflows/publish.yml": "jobs:\n  publish:\n    steps:\n      - run: git push --mirror origin\n"})
        output, report = fixture.check(base, 1)
        self.assertIn("mirror push rewrites branches", output)
        self.assertEqual(report["force_pushes"]["unreadable"], [])

    def test_current_source_profile_includes_recognizer_provenance(self):
        # The staged source profile must bind the new module's bytes. Historical
        # profiles without that module keep their original identity.
        import hashlib
        directory = ROOT / sc.adoption.PREFIX
        files = {sc.adoption.PREFIX + path.name: path.read_text()
                 for path in directory.iterdir()
                 if path.suffix in (".py", ".json") or path.name == "action.yml"}
        scope_path = "policy/deal-randomness-scope.json"
        files[scope_path] = (ROOT / scope_path).read_text()
        for dependency in ("scripts/source_provenance.py", "policy/source-relocations.json"):
            files[dependency] = (ROOT / dependency).read_text()
        tree = sc.DictTree(files)
        source = sc.adoption.source_profile(tree)
        self.assertEqual(source, sc.adoption.source_profile(sc.Tree(str(ROOT), "HEAD")))
        self.assertIn("force-push", source["rules"])
        self.assertIn(sc.adoption.PREFIX + "no_force_push.py", source["sources"])
        self.assertEqual(source["sources"][sc.adoption.PREFIX + "no_force_push.py"],
                         hashlib.sha256((ROOT / sc.adoption.PREFIX / "no_force_push.py").read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
