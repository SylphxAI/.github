"""Declaration guard and shrinking adoption through the shipped checker CLI."""
import datetime
import importlib.util
import json
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
ACTION = ROOT / ".github/actions/stack-conformance"
SPEC = importlib.util.spec_from_file_location("tenant_tests_checker", ACTION / "stack_conformance.py")
sc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sc)
reads = sc.architecture.tenant_reads
RULE = "architecture/C2/tenant-read-functions"
RELEASE = "0a8e0782e55506e4c61062f55a77428d7186d799"
TODAY = datetime.date(2026, 10, 30)
MANIFEST = '[project]\nname = "Example"\n[resources.database]\ntier = "starter"\n'
V2_MANIFEST = 'version = "2"\n[[resources]]\nname = "primary"\nkind = "postgres"\ntier = "starter"\n'
DECLARATION = json.dumps({"kind": "database", "deskReportFunctions": [
    {"name": "activity_summary", "args": "timestamp with time zone"}]})
WORKFLOW = ".github/workflows/ci.yml"


def workflow(pin=RELEASE):
    return ("jobs:\n  stack:\n    steps:\n      - uses: SylphxAI/.github/.github/actions/stack-conformance@"
            + pin + " # main\n")


class Fixture:
    def __init__(self, directory):
        self.root = pathlib.Path(directory)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.root), *args],
                                       text=True, stderr=subprocess.PIPE).strip()

    def commit(self, files):
        for name, value in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value)
        self.git("add", ".")
        self.git("commit", "-qm", "fixture", "--allow-empty")
        return self.git("rev-parse", "HEAD")

    def plan(self, base):
        return sc.adoption.plan_adoption(sc.Tree(str(self.root), base), sc.Tree(str(self.root), "HEAD"),
            "example/app", shared_repo=str(ROOT), today=TODAY)

    def check(self, base, expected):
        report = self.root / "report.json"
        result = subprocess.run(["python3", str(ACTION / "stack_conformance.py"), "check",
            "--repo", str(self.root), "--base", base, "--repository", "example/app",
            "--today", TODAY.isoformat(), "--shared-source", str(ROOT), "--report", str(report)],
            capture_output=True, text=True)
        if result.returncode != expected:
            raise AssertionError(result.stdout + result.stderr)
        return result.stdout, json.loads(report.read_text())


class DeclarationTests(unittest.TestCase):
    def report(self, files):
        return sc.architecture.evaluate(sc.DictTree(files), "example/app", None, TODAY)

    def test_production_shapes_require_a_declaration(self):
        for manifest in (MANIFEST, '[resources]\ndatabase = {}\n',
                         '[database]\nsize = "small"\n', '[database]\n',
                         '[environments.production.resources.database]\ntier = "starter"\n',
                         '[environments.production.database]\nsize = "small"\n'):
            with self.subTest(manifest=manifest):
                report = self.report({"sylphx.toml": manifest})
                self.assertEqual(report["unreadable"], [])
                self.assertEqual([r["rule"] for r in report["findings"]], ["tenant-read-functions"])
                self.assertIn(reads.DECLARATION, report["findings"][0]["reason"])

    def test_no_database_preview_only_and_migration_only_pass(self):
        for manifest in ('[project]\nname = "Example"\n',
                         '[environments.preview.resources.database]\ntier = "starter"\n',
                         '[database.migrations]\nengine = "atlas"\n'):
            with self.subTest(manifest=manifest):
                self.assertEqual(self.report({"sylphx.toml": manifest})["findings"], [])

    def test_v2_postgres_resource_kinds_require_the_same_declaration(self):
        for kind in ("postgres", "POSTGRES", "MANAGED_RESOURCE_KIND_POSTGRES"):
            for prefix in ("", "environments.production."):
                manifest = V2_MANIFEST.replace('"postgres"', json.dumps(kind)).replace(
                    "[[resources]]", "[[" + prefix + "resources]]")
                with self.subTest(kind=kind, prefix=prefix):
                    report = self.report({"sylphx.toml": manifest})
                    self.assertEqual(report["unreadable"], [])
                    self.assertEqual([r["rule"] for r in report["findings"]],
                                     ["tenant-read-functions"])
                    self.assertEqual(self.report({"sylphx.toml": manifest,
                        reads.DECLARATION: DECLARATION})["findings"], [])

    def test_v2_other_resources_and_preview_only_database_pass(self):
        for kind in ("cache", "CACHE", "MANAGED_RESOURCE_KIND_CACHE", "queue", "volume"):
            with self.subTest(kind=kind):
                manifest = V2_MANIFEST.replace('"postgres"', json.dumps(kind))
                self.assertEqual(self.report({"sylphx.toml": manifest})["findings"], [])
        preview = V2_MANIFEST.replace("[[resources]]", "[[environments.preview.resources]]")
        self.assertEqual(self.report({"sylphx.toml": preview})["findings"], [])

    def test_real_patch_format_and_default_empty_arguments_pass(self):
        for declaration in (DECLARATION, '{"deskReportFunctions":[{"name":"counts"}]}'):
            report = self.report({"sylphx.toml": MANIFEST, reads.DECLARATION: declaration})
            self.assertEqual(report["findings"], [])
            self.assertEqual(report["unreadable"], [])

    def test_empty_malformed_duplicate_and_wrong_shapes_fail_closed(self):
        bad = ['{}', '{', '[]', '{"deskReportFunctions":[]}',
               '{"kind":"kv","deskReportFunctions":[{"name":"counts"}]}',
               '{"deskReportFunctions":[{"name":"counts","extra":true}]}',
               '{"deskReportFunctions":[{"name":"pg_counts"}]}',
               '{"deskReportFunctions":[{"name":"Counts"}]}',
               '{"deskReportFunctions":[{"name":"counts","args":false}]}',
               '{"deskReportFunctions":[{"name":"counts","args":"schema.type"}]}',
               '{"deskReportFunctions":[{"name":"counts"},{"name":"counts"}]}',
               '{"deskReportFunctions":[],"deskReportFunctions":[{"name":"counts"}]}',
               json.dumps({"deskReportFunctions": [{"name": "f" + str(i)} for i in range(65)]})]
        for declaration in bad:
            with self.subTest(declaration=declaration):
                report = self.report({"sylphx.toml": MANIFEST, reads.DECLARATION: declaration})
                self.assertTrue(report["unreadable"] or report["findings"])

    def test_monorepo_declaration_is_relative_to_its_manifest(self):
        report = self.report({"api/sylphx.toml": MANIFEST, reads.DECLARATION: DECLARATION})
        self.assertIn("api/" + reads.DECLARATION, report["findings"][0]["reason"])
        report = self.report({"api/sylphx.toml": MANIFEST, "api/" + reads.DECLARATION: DECLARATION})
        self.assertEqual(report["findings"], [])

    def test_fixture_manifest_does_not_require_a_declaration(self):
        self.assertEqual(self.report({"tests/fixtures/sylphx.toml": MANIFEST})["findings"], [])


class PublicGuardTests(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Fixture(temporary.name)

    def test_cli_must_fail_missing_and_pass_declaration_or_no_database(self):
        for files, expected in [({"sylphx.toml": MANIFEST}, 1),
                ({"sylphx.toml": MANIFEST, reads.DECLARATION: DECLARATION}, 0),
                ({"sylphx.toml": V2_MANIFEST}, 1),
                ({"sylphx.toml": V2_MANIFEST, reads.DECLARATION: DECLARATION}, 0),
                ({"sylphx.toml": V2_MANIFEST.replace('"postgres"', '"cache"')}, 0),
                ({"sylphx.toml": '[project]\nname = "Example"\n'}, 0)]:
            with self.subTest(expected=expected):
                f = self.fixture()
                base = f.commit({WORKFLOW: workflow(), "README.md": "example\n"})
                f.commit(files)
                output, _ = f.check(base, expected)
                if expected:
                    self.assertIn(reads.DECLARATION, output)
                    self.assertIn("PATCH /v1/resources/{id}", output)

    def test_existing_gap_without_explicit_adoption_fails_even_on_docs(self):
        f = self.fixture()
        base = f.commit({WORKFLOW: workflow(), "sylphx.toml": MANIFEST})
        f.commit({"README.md": "documentation change\n"})
        f.check(base, 1)

    def test_only_new_rule_is_seeded_and_existing_gap_then_shrinks(self):
        f = self.fixture()
        base = f.commit({WORKFLOW: workflow(), "sylphx.toml": MANIFEST})
        plan = f.plan(base)
        self.assertEqual(plan["introduced_rules"], [RULE, "force-push"])
        self.assertEqual(len(plan["evidence"]["added_records"]), 1)
        adopted = f.commit({sc.BASELINE: plan["baseline_text"]})
        f.check(base, 0)
        # Unrelated manifest edits preserve resource debt identity.
        f.commit({"sylphx.toml": "# new comment\n" + MANIFEST})
        f.check(adopted, 0)
        before_fix = f.git("rev-parse", "HEAD")
        f.commit({reads.DECLARATION: DECLARATION})
        output, _ = f.check(before_fix, 1)
        self.assertIn("stale architecture baseline", output)
        fixed = f.commit({sc.BASELINE: "\n".join(line for line in plan["baseline_text"].splitlines()
                         if not line.startswith("architecture ")) + "\n"})
        f.check(before_fix, 0)
        f.git("rm", reads.DECLARATION)
        f.commit({})
        f.check(fixed, 1)
        f.commit({sc.BASELINE: plan["baseline_text"]})
        output, _ = f.check(fixed, 1)
        self.assertIn("cannot grow", output)

    def test_new_database_cannot_be_added_to_the_adoption_baseline(self):
        f = self.fixture()
        base = f.commit({WORKFLOW: workflow(), "README.md": "example\n"})
        f.commit({"sylphx.toml": MANIFEST})
        with self.assertRaisesRegex(ValueError, "new head debt"):
            f.plan(base)

    def test_legacy_signature_list_is_a_baselinable_gap_not_a_waiver(self):
        f = self.fixture()
        base = f.commit({WORKFLOW: workflow(), "sylphx.toml": MANIFEST,
                         reads.DECLARATION: '{"version":1,"functions":["desk_report.counts()"]}'})
        f.check(base, 1)
        plan = f.plan(base)
        self.assertEqual(len(plan["evidence"]["added_records"]), 1)
        adopted = f.commit({sc.BASELINE: plan["baseline_text"]})
        f.check(base, 0)
        f.commit({reads.DECLARATION: DECLARATION, sc.BASELINE: "\n".join(
            line for line in plan["baseline_text"].splitlines() if not line.startswith("architecture ")) + "\n"})
        f.check(adopted, 0)

    def test_removing_baseline_record_while_gap_remains_fails(self):
        f = self.fixture()
        base = f.commit({WORKFLOW: workflow(), "sylphx.toml": MANIFEST})
        plan = f.plan(base)
        adopted = f.commit({sc.BASELINE: plan["baseline_text"]})
        f.check(base, 0)
        f.commit({sc.BASELINE: "\n".join(line for line in plan["baseline_text"].splitlines()
                 if not line.startswith("architecture ")) + "\n"})
        f.check(adopted, 1)

    def test_released_profile_stays_historical_and_current_profile_binds_module(self):
        profile = sc.adoption.source_profile(sc.Tree(str(ROOT), RELEASE))
        self.assertNotIn(sc.adoption.PREFIX + "tenant_reads.py", profile["sources"])
        self.assertNotIn(RULE, profile["rules"])
        self.assertIn(RULE, sc.adoption.current_rules())
        files = {sc.adoption.PREFIX + name: (ACTION / name).read_text() for name in (
            "action.yml", "stack_conformance.py", "architecture.py", "model_pins.py", "tenant_reads.py", "no_force_push.py")}
        current = sc.adoption.source_profile(sc.DictTree(files))
        self.assertIn(sc.adoption.PREFIX + "tenant_reads.py", current["sources"])
        self.assertIn(RULE, current["rules"])
        del files[sc.adoption.PREFIX + "tenant_reads.py"]
        with self.assertRaisesRegex(ValueError, "checker source missing"):
            sc.adoption.source_profile(sc.DictTree(files))

    def test_removed_declaration_and_unreadable_declaration_fail(self):
        f = self.fixture()
        base = f.commit({WORKFLOW: workflow(), "sylphx.toml": MANIFEST,
                         reads.DECLARATION: DECLARATION})
        f.git("rm", reads.DECLARATION)
        f.commit({})
        f.check(base, 1)
        f.commit({reads.DECLARATION: "{}"})
        output, _ = f.check(base, 1)
        self.assertIn("one to 64", output)

    def test_declaration_json_is_in_partial_clone_blob_scope(self):
        self.assertTrue(sc.blob_candidate(reads.DECLARATION))
        self.assertTrue(sc.blob_candidate("services/api/" + reads.DECLARATION))
