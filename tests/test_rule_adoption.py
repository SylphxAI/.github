"""Adoption and ordinary ratchets through immutable trees and the shipped CLIs."""
import datetime
import importlib.util
import json
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
ACTION = ROOT / ".github/actions/stack-conformance"
SPEC = importlib.util.spec_from_file_location("adoption_tests_checker", ACTION / "stack_conformance.py")
sc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sc)
a = sc.adoption
TODAY = datetime.date(2026, 10, 30)
OLD = "bc664ac7410c00d233fe4f729b6747969a4a9bed"
ARCH = "096601dfd98afa27345312c615250212b9ea5e55"
UNKNOWN = "6c3557dbf5a9169417ba915a643395dc752709f6"
WORKFLOW = ".github/workflows/ci.yml"
SOURCE = "src/jobs.rs"
POLL = "fn run() {\n loop {\n  query();\n  sleep(1);\n }\n}\n"


def workflow(pin=OLD, verdict=False, marker="main"):
    return ("jobs:\n  stack:\n    steps:\n      - uses: SylphxAI/.github/.github/actions/stack-conformance@"
            + pin + " # " + marker + "\n" +
            ("  verdict:\n    if: needs.test.result != 'failure'\n" if verdict else ""))


class Fixture:
    def __init__(self, root):
        self.root = pathlib.Path(root)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.root), *args], text=True, stderr=subprocess.PIPE).strip()

    def write(self, files):
        for name, value in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value.encode())

    def commit(self, files):
        self.write(files)
        self.git("add", ".")
        self.git("commit", "-qm", "fixture", "--allow-empty")
        return self.git("rev-parse", "HEAD")

    def plan(self, base, head="HEAD"):
        return a.plan_adoption(sc.Tree(str(self.root), base), sc.Tree(str(self.root), head),
                               "example/app", shared_repo=str(ROOT), today=TODAY)

    def check(self, base, expected):
        report = self.root.parent / (self.root.name + ".json")
        result = subprocess.run(["python3", str(ACTION / "stack_conformance.py"), "check",
            "--repo", str(self.root), "--base", base, "--head", "HEAD", "--repository", "example/app",
            "--today", TODAY.isoformat(), "--report", str(report), "--shared-source", str(ROOT)], capture_output=True, text=True)
        if result.returncode != expected:
            raise AssertionError(result.stdout + result.stderr)
        return result.stdout, json.loads(report.read_text())


class AdoptionTests(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Fixture(temporary.name)

    def adopt(self, f, *, files=None, baseline="# existing baseline\n"):
        base = f.commit({WORKFLOW: workflow(verdict=True), SOURCE: POLL,
                         sc.BASELINE: baseline, **(files or {})})
        result = f.plan(base)
        adopted = f.commit({sc.BASELINE: result["baseline_text"], WORKFLOW: workflow(UNKNOWN, verdict=True)})
        f.check(base, 0)
        return base, adopted, result

    def test_cli_accepts_staged_tree_and_preserves_original_baseline_bytes(self):
        f = self.fixture()
        original = "# existing ratchet\r\n# exact spacing  \r\n"
        base = f.commit({WORKFLOW: workflow(marker="old PR"), SOURCE: POLL, sc.BASELINE: original})
        f.write({WORKFLOW: workflow(UNKNOWN)})
        f.git("add", ".")
        candidate = f.git("write-tree")
        evidence = f.root.parent / (f.root.name + "-evidence.json")
        result = subprocess.run(["python3", str(ACTION / "adoption.py"), "adopt", "--repo", str(f.root),
            "--base", base, "--head", candidate, "--shared-repo", str(ROOT), "--repository", "example/app",
            "--output", str(f.root / sc.BASELINE), "--evidence", str(evidence)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((f.root / sc.BASELINE).read_bytes().startswith(original.encode()))
        receipt = json.loads(evidence.read_text())
        self.assertEqual(receipt["head_tree"], candidate)
        self.assertEqual(receipt["old_checkers"][0]["commit"], OLD)
        f.commit({})
        f.check(base, 0)
        f.check("", 0)

    def test_pin_only_bump_and_line_shift_keep_debt_but_span_edit_fails(self):
        f = self.fixture()
        _, adopted, _ = self.adopt(f)
        f.commit({WORKFLOW: "# moved header\n" + workflow("422fdbf8529ec11f95c8ffa2de6e0a26ec251c67", verdict=True), SOURCE: "// context\n" + POLL})
        _, report = f.check(adopted, 0)
        self.assertTrue(all(not row["changed"] for row in report["findings"]))
        previous = f.git("rev-parse", "HEAD")
        f.commit({SOURCE: POLL.replace("query();", "query_again();")})
        output, _ = f.check(previous, 1)
        self.assertIn("stale architecture baseline", output)

    def test_inserted_identical_violation_cannot_hide_on_unchanged_lines(self):
        f = self.fixture()
        _, adopted, _ = self.adopt(f)
        f.commit({SOURCE: POLL + POLL})
        output, report = f.check(adopted, 1)
        polling = [row for row in report["findings"] if row["rule"] == "polling-loop"]
        self.assertEqual([row["occurrence"] for row in polling], [1, 2])
        self.assertTrue(polling[1]["changed"])

    def test_same_line_duplicate_spans_are_individually_counted(self):
        rows = sc.architecture.scan_file("src/a.rs", "loop { query(); } loop { query(); }\n",
                                         "example/app", set(), [])
        self.assertEqual([row["occurrence"] for row in rows], [1, 2])

    def test_adopted_rule_marker_cannot_be_removed_to_reopen_growth(self):
        f = self.fixture()
        _, adopted, result = self.adopt(f)
        marker = a.RULE_MARKER + "architecture/C5/polling-loop\n"
        f.commit({sc.BASELINE: result["baseline_text"].replace(marker, "")})
        output, _ = f.check(adopted, 1)
        self.assertIn("rule adoption marker cannot be removed", output)

    def test_duplicate_baseline_records_fail_before_set_parsing(self):
        f = self.fixture()
        _, adopted, result = self.adopt(f)
        record = result["evidence"]["added_records"][0]
        f.commit({sc.BASELINE: result["baseline_text"] + record + "\n"})
        output, _ = f.check(adopted, 1)
        self.assertIn("duplicate baseline record", output)

    def test_new_head_debt_refused_without_writing_output(self):
        f = self.fixture()
        base = f.commit({WORKFLOW: workflow(), SOURCE: POLL})
        head = f.commit({SOURCE: POLL + POLL})
        with self.assertRaisesRegex(ValueError, "new head debt"):
            f.plan(base, head)

    def test_mixed_pins_union_prevents_growth_of_old_architecture_rules(self):
        f = self.fixture()
        base = f.commit({WORKFLOW: workflow(ARCH, verdict=True), SOURCE: POLL,
                         ".github/workflows/other.yml": workflow(),
                         "src/model.ts": 'call("gpt-5.1-2025-11-13");\n'})
        result = f.plan(base)
        self.assertEqual(result["introduced_rules"], ["architecture/C1/cache-economy", "architecture/C1/deal-randomness",
                         "architecture/C1/project-portability",
                         "architecture/C2/tenant-read-functions", "force-push", "model-pin", "model-selector"])
        self.assertTrue(all(row.startswith("model-pin ") for row in result["evidence"]["added_records"]))
        record = sc.architecture.baseline_key(sc.architecture.evaluate(sc.Tree(str(f.root), base), "example/app", None, TODAY)["findings"][0])
        f.commit({sc.BASELINE: result["baseline_text"] + sc.architecture.BASELINE_MARKER + "\n" + record + "\n"})
        output, _ = f.check(base, 1)
        self.assertIn("cannot grow", output)

    def test_unknown_pin_followup_passes_but_cannot_reseed_existing_rule(self):
        f = self.fixture()
        _, adopted, result = self.adopt(f)
        next_base = f.commit({"README.md": "unchanged debt\n"})
        f.check(adopted, 0)
        f.commit({"src/other.rs": POLL})
        findings = sc.architecture.evaluate(sc.Tree(str(f.root), "HEAD"), "example/app", None, TODAY)["findings"]
        new_record = next(sc.architecture.baseline_key(row) for row in findings if row["path"] == "src/other.rs")
        f.commit({sc.BASELINE: result["baseline_text"] + new_record + "\n"})
        output, _ = f.check(next_base, 1)
        self.assertIn("unknown old checker pin", output)

    def test_unknown_old_pin_and_changed_baseline_refused(self):
        f = self.fixture()
        base = f.commit({WORKFLOW: workflow(UNKNOWN), SOURCE: POLL})
        with self.assertRaisesRegex(ValueError, "unknown old checker"):
            f.plan(base)
        base = f.commit({WORKFLOW: workflow()})
        f.commit({sc.BASELINE: "# replacement\n"})
        with self.assertRaisesRegex(ValueError, "preserve original bytes"):
            f.plan(base)

    def test_legacy_architecture_records_fail_closed(self):
        f = self.fixture()
        base = f.commit({WORKFLOW: workflow(), sc.BASELINE: "# architecture-ratchet-v1\n"})
        with self.assertRaisesRegex(ValueError, "explicit reviewed migration"):
            f.plan(base)

    def test_old_checker_sources_match_all_four_census_profiles(self):
        for pin in (OLD, "a7d2f765df5b91aad557962a70b6195985210464",
                    "f488fc90cda8ee46a1c3baa3f5b608aea5004786", "8bbd9a31a5f0364e4f286e4bf675d7f01c0abea2"):
            with self.subTest(pin=pin):
                profiles = a.old_checkers(sc.DictTree({WORKFLOW: workflow(pin)}), shared_repo=str(ROOT))
                self.assertEqual(profiles[0]["rules"], [])
                self.assertIsNone(profiles[0]["sources"][a.PREFIX + "architecture.py"])

    def test_published_model_checkers_allow_only_new_cache_rule_adoption(self):
        cache_rule = "architecture/C1/cache-economy"
        for pin in ("083eff31aa012b3a24d5601abfc524ad461b2394",
                    "77bd558b917b6926d2fe7695d347deff6c9b0fa4"):
            with self.subTest(pin=pin):
                tree = sc.DictTree({WORKFLOW: workflow(pin)})
                profiles = a.old_checkers(tree, shared_repo=str(ROOT))
                enforced = set(profiles[0]["rules"])
                self.assertEqual(len(enforced), 16)
                self.assertIn("model-pin", enforced)
                self.assertNotIn(cache_rule, enforced)
                with mock.patch.object(a, "current_rules", return_value=enforced | {cache_rule}):
                    introduced, _ = a.introduced_rules(tree, "", shared_repo=str(ROOT))
                    self.assertEqual(introduced, {cache_rule})
                    self.assertEqual(a.seed_permissions(tree, a.RULE_MARKER + cache_rule + "\n", "", set()),
                                     {cache_rule})
                    for rule in enforced:
                        with self.assertRaisesRegex(ValueError, "already enforced"):
                            a.seed_permissions(tree, a.RULE_MARKER + rule + "\n", "", set())

    def test_forged_capability_profile_and_wrong_source_are_rejected(self):
        data = a.load_registry()
        identity = data["commits"][OLD]
        data["profiles"][identity]["rules"] = ["model-pin"]
        with mock.patch.object(pathlib.Path, "read_text", return_value=json.dumps(data)):
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                a.load_registry()
        with mock.patch.object(a, "source_profile", return_value={"sources": {}, "rules": []}):
            with self.assertRaisesRegex(ValueError, "provenance mismatch"):
                a.old_checkers(sc.DictTree({WORKFLOW: workflow()}), shared_repo=str(ROOT))

    def test_capability_parser_does_not_execute_old_modules(self):
        tree = sc.DictTree({a.PREFIX + "action.yml": "python stack_conformance.py",
                            a.PREFIX + "stack_conformance.py":
                            'raise RuntimeError("must never execute")\ndef cmd_check(args):\n    return 0\n'})
        self.assertEqual(a.source_profile(tree)["rules"], [])

    def test_private_caller_missing_main_marker_fails_offline_and_source_is_exempt(self):
        f = self.fixture()
        base = f.commit({WORKFLOW: workflow()})
        f.commit({WORKFLOW: workflow(marker="old PR")})
        output, _ = f.check(base, 1)
        self.assertIn("missing # main", output)
        tree = sc.Tree(str(f.root), "HEAD")
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("network/subprocess forbidden")):
            self.assertTrue(a.main_marker_errors(tree, "private/app"))
            self.assertEqual(a.main_marker_errors(tree, "SylphxAI/.github"), [])

    def test_adoption_ignores_negative_fixture_config_but_refuses_real_invalid_config(self):
        f = self.fixture()
        base = f.commit({WORKFLOW: workflow(), SOURCE: POLL,
                         'tools/launch-gates/fixtures/b-invalid/sylphx.toml': 'this is = = not toml\n'})
        result = f.plan(base)
        f.commit({sc.BASELINE: result['baseline_text']})
        f.check(base, 0)
        f.commit({'sylphx.toml': 'this is = = not toml\n'})
        with self.assertRaisesRegex(ValueError, 'invalid environment declaration'):
            f.plan(f.git('rev-parse', 'HEAD'))
        output, _ = f.check(base, 1)
        self.assertIn('invalid environment declaration: sylphx.toml', output)

    def test_adoption_ignores_vendored_python_two_but_not_adjacent_production(self):
        f = self.fixture()
        base = f.commit({WORKFLOW: workflow(), SOURCE: POLL,
                         'client/bot/thirdparty/scons/setup.py': 'print "Python 2 dependency"\n'})
        result = f.plan(base)
        f.commit({sc.BASELINE: result['baseline_text']})
        f.check(base, 0)
        f.commit({'client/bot/src/setup.py': 'print "invalid production Python"\n'})
        with self.assertRaisesRegex(ValueError, 'source unreadable'):
            f.plan(f.git('rev-parse', 'HEAD'))
        output, _ = f.check(base, 1)
        self.assertIn('client/bot/src/setup.py', output)


if __name__ == "__main__":
    unittest.main()
