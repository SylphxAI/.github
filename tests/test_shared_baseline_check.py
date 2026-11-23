import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import shared_baseline_check as baseline


class SharedBaselineTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workspace = Path(directory.name)
        self.repo = self.workspace / "source"
        self.repo.mkdir()
        (self.workspace / "baseline-checker").symlink_to(ROOT, target_is_directory=True)
        self.git("init", "-q", "-b", "main")
        self.base = self.commit({"README.md": "# Sample project\n"})

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), "-c", "user.name=Example",
                                        "-c", "user.email=example@example.com", *args], text=True).strip()

    def commit(self, files):
        for path, content in files.items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                target.write_bytes(content)
            else:
                target.write_text(content)
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        return self.git("rev-parse", "HEAD")

    def check(self, head, *, public=False, migrations=True, base=None):
        return baseline.check(self.repo, base or self.base, head, public, migrations, "postgres")

    def test_public_introductions_and_private_fast_pass(self):
        examples = ["ssh desk-next", "https://gra-c18:8443", "https://fsn1-c7.example.net",
                    "http://cache.test.svc.cluster.local", "operator@kylet.se", "@shtse8",
                    "host-claims", "/host-claims/current", "DESK_OPERATOR_TOKEN", "Kyle Tse"]
        examples.extend(json.loads((ROOT / "policy/merge-queue.json").read_text())["orgs"][1:])
        for value in examples:
            with self.subTest(value=value):
                self.assertTrue(list(baseline.hygiene_findings(b"", value.encode(), baseline.hygiene_rules())))
        head = self.commit({"paths/host-claims.md": "See example.com\n", "README.md": "http://desk-next\n"})
        failures = self.check(head, public=True)
        self.assertEqual(len(failures), 2)
        self.assertNotIn("desk-next", str(failures))
        self.assertNotIn("host-claims", str(failures))
        self.assertEqual(self.check(head), [])

    def test_normal_oss_text_and_historical_occurrences_pass(self):
        old = self.commit({"README.md": "Legacy: http://desk-next\n"})
        head = self.commit({"README.md": "Legacy: http://desk-next\n"
                            "https://example.com/docs; api.example.org; desktop-next; "
                            "desk-nextjs; gra-cast; fsn1-cache; host-claiming; "
                            "api.svc.cluster.local.example.com; not-kylet.se\n"})
        self.assertEqual(self.check(head, public=True, base=old), [])

    def test_sql_scope_and_safe_changes(self):
        head = self.commit({"docs/sample.sql": "DROP TABLE accounts;",
                            "db/migrations/10.sql": "ALTER TABLE accounts ADD COLUMN enabled boolean;",
                            "db/migrations_v2/11.SQL": "CREATE TABLE settings (id int);",
                            "migrations/12.sql": "DROP INDEX CONCURRENTLY idx;",
                            "migrations/13.sql": "-- expand-contract: drop-phase https://example.com/releases/v2\n"
                                                  "ALTER TABLE accounts DROP old_column;",
                            "migrations/14.sql": "-- contract: released-expand-v2\nDROP TABLE old_accounts;",
                            "migrations/15.sql": "SELECT 'DROP TABLE accounts'; /* DROP TABLE accounts; */"})
        self.assertEqual(self.check(head), [])

    def test_unphased_drop_and_marker_scope(self):
        head = self.commit({"migrations/01.sql": "ALTER TABLE accounts\nDROP /* comment */ COLUMN obsolete;",
                            "migrations/02.sql": "DROP TABLE accounts;",
                            "migrations/03.sql": "-- expand-contract: drop-phase\nDROP TABLE accounts;",
                            "migrations/04.sql": "-- contract: released-v2\nSELECT 1;\nDROP TABLE accounts;",
                            "migrations/05.sql": "ALTER TABLE accounts DROP CONSTRAINT obsolete;",
                            "migrations/06.mysql.sql": "/*!50000 DROP TABLE accounts */;",
                            "migrations/07.sql": "DO $$ BEGIN ALTER TABLE accounts DROP obsolete; END $$;"})
        failures = self.check(head)
        self.assertEqual(len(failures), 7, failures)
        self.assertEqual(self.check(head, public=True, migrations=False), [])

    def test_historical_drop_reaudit_only_on_semantic_or_marker_change(self):
        base = self.commit({"migrations/01.sql": "DROP TABLE old_accounts;",
                            "migrations/02.sql": "-- contract: released-v2\nDROP TABLE old_settings;"})
        head = self.commit({"migrations/01.sql": "-- old migration\nDROP TABLE old_accounts;\nSELECT 1;",
                            "migrations/02.sql": "DROP TABLE old_settings;"})
        failures = self.check(head, base=base)
        self.assertEqual(len(failures), 1, failures)
        self.assertIn("02.sql", failures[0])

    def test_unreadable_migration_fails_closed(self):
        head = self.commit({"migrations/10.sql": b"\xffinvalid"})
        self.assertIn("could not inspect", str(self.check(head)))

    def test_migration_symlink_fails_closed(self):
        (self.repo / "migrations").mkdir()
        os.symlink("../README.md", self.repo / "migrations/01.sql")
        head = self.commit({"other": "data"})
        self.assertIn("could not inspect", str(self.check(head)))

    def test_bad_base_is_not_a_pass(self):
        with self.assertRaises(ValueError):
            self.check(self.base, base="HEAD", public=True)

    def run_workflow_step(self, event_name, head, base=""):
        workflow = yaml.safe_load((ROOT / ".github/workflows/shared-baseline-check.yml").read_text())
        step, = [step for step in workflow["jobs"]["baseline"]["steps"] if "run" in step]
        self.assertEqual(step["env"]["EVENT_NAME"], "${{ github.event_name }}")
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", step["run"]],
            cwd=self.workspace, capture_output=True, text=True, timeout=10,
            env=dict(os.environ, EVENT_NAME=event_name, BASE_SHA=base, HEAD_SHA=head,
                     IS_PUBLIC="false", MIGRATIONS="", MIGRATION_DIALECT="postgres"),
        )

    def test_parameterless_dispatch_compares_only_with_checked_out_parent(self):
        self.commit({"migrations/01.sql": "DROP TABLE retired_accounts;"})
        head = self.commit({"README.md": "# Updated project\n"})
        result = self.run_workflow_step("workflow_dispatch", head)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_parameterless_dispatch_checks_changes_in_head(self):
        head = self.commit({"migrations/01.sql": "DROP TABLE retired_accounts;"})
        result = self.run_workflow_step("workflow_dispatch", head)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("01.sql", result.stdout)

    def test_parameterless_dispatch_without_parent_fails_closed(self):
        result = self.run_workflow_step("workflow_dispatch", self.base)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("could not resolve HEAD~1", result.stderr)

    def test_dispatch_keeps_explicit_base_instead_of_using_parent(self):
        self.commit({"migrations/01.sql": "DROP TABLE retired_accounts;"})
        head = self.commit({"README.md": "# Updated project\n"})
        result = self.run_workflow_step("workflow_dispatch", head, self.base)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("01.sql", result.stdout)

    def test_non_dispatch_events_do_not_fall_back_to_existing_parent(self):
        head = self.commit({"README.md": "# Updated project\n"})
        for event_name in ("pull_request", "merge_group", "push"):
            with self.subTest(event_name=event_name):
                result = self.run_workflow_step(event_name, head)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("could not read the comparison", result.stdout)
