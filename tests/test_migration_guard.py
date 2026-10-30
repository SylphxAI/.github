"""SQL refusal matrix and real Git history tests for migration-guard."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / ".github/actions/migration-guard"
SPEC = importlib.util.spec_from_file_location("migration_guard", ACTION / "migration_guard.py")
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


class SqlTests(unittest.TestCase):
    def test_foreign_key_delete_actions_have_separate_contract_policy(self):
        for dialect in ("postgres", "mysql"):
            for action in ("RESTRICT", "NO ACTION"):
                sql = f"ALTER TABLE accounts ADD CONSTRAINT account_owner FOREIGN KEY (owner_id) REFERENCES owners(id) ON DELETE {action};"
                with self.subTest(dialect=dialect, action=action):
                    self.assertEqual(guard.check_sql(sql, dialect), [])
                    self.assertIn((1, "DELETE without WHERE"), guard.check_sql(sql + "DELETE FROM accounts;", dialect))
            self.assertEqual(guard.check_sql("ALTER TABLE accounts ADD FOREIGN KEY (owner_id) REFERENCES owners(id);", dialect), [])
            for action in ("CASCADE", "SET NULL"):
                sql = f"ALTER TABLE accounts ADD FOREIGN KEY (owner_id) REFERENCES owners(id) ON DELETE {action};"
                with self.subTest(dialect=dialect, action=action):
                    self.assertIn(f"ON DELETE {action}", str(guard.check_sql(sql, dialect)))
                    self.assertEqual(guard.check_sql("-- contract: released-expand-42\n" + sql, dialect), [])

    def test_destructive_matrix_and_released_expand_marker(self):
        common = [
            "DROP TABLE accounts;", "DROP TABLE IF EXISTS accounts;",
            "DROP SCHEMA public CASCADE;", "DROP DATABASE app;",
            "ALTER TABLE accounts DROP COLUMN balance;", "ALTER TABLE accounts DROP balance;",
            "TRUNCATE accounts;", "TRUNCATE TABLE accounts;",
            "DELETE FROM accounts;", "UPDATE accounts SET balance = 0;",
            "ALTER TABLE accounts RENAME TO accounts_old;",
            "ALTER TABLE accounts RENAME COLUMN balance TO old_balance;",
            "ALTER TABLE accounts ADD COLUMN region text NOT NULL;",
            "ALTER TABLE accounts ADD region varchar(24) NOT NULL;",
            "ALTER TABLE accounts ADD nickname text, ADD region text NOT NULL;",
            "CREATE TABLE IF NOT EXISTS accounts (id int); ALTER TABLE accounts ADD region text NOT NULL;",
            "ALTER TABLE accounts ALTER COLUMN region SET NOT NULL;",
            "ALTER TABLE accounts ALTER COLUMN region TYPE varchar(2);",
            "ALTER TABLE accounts ALTER COLUMN region SET DATA TYPE integer;",
            "DELETE FROM accounts WHERE id = 1; UPDATE accounts SET balance = 0;",
            "UPDATE accounts SET balance = (SELECT balance FROM other WHERE id = 1);",
            "WITH selected AS (SELECT id FROM other WHERE id = 1) DELETE FROM accounts;",
            "WITH gone AS (DELETE FROM accounts RETURNING id) SELECT * FROM gone WHERE id = 1;",
            "ALTER /* comment */ TABLE accounts DROP /* comment */ COLUMN balance;",
            "EXECUTE 'DROP TABLE accounts';",
        ]
        extra = {
            "postgres": [
                "SET standard_conforming_strings = off; SELECT 1;",
                'SET "standard_conforming_strings"=off; SELECT \'a\\\'b\'; DROP TABLE accounts; -- \';',
                "ALTER TABLE accounts DROP identity;",
                "ALTER TABLE accounts DROP index;",
                "ALTER FOREIGN TABLE accounts DROP nickname;",
                "DROP OWNED BY app;",
                "DROP TYPE account_state CASCADE;",
                "DROP DOMAIN account_state CASCADE;",
                'ALTER TABLE "public"."accounts" DROP COLUMN "balance";',
                "DO $$ BEGIN EXECUTE 'DROP TABLE accounts'; END $$;",
                "CREATE OR REPLACE FUNCTION cleanup() RETURNS void AS $$ DELETE FROM accounts; $$ LANGUAGE SQL;",
                "/* outer /* inner */ still comment */ DROP TABLE accounts;",
            ],
            "mysql": [
                "SET sql_mode='NO_BACKSLASH_ESCAPES'; SELECT 'a\\'; DROP TABLE accounts; -- ';",
                "SET @@session.sql_mode='NO_BACKSLASH_ESCAPES'; SELECT 'a\\'; DROP TABLE accounts; -- ';",
                "SET @@session.`sql_mode`='NO_BACKSLASH_ESCAPES'; SELECT 'a\\'; DROP TABLE accounts; -- ';",
                "UPDATE accounts SET nickname = @where;",
                "SET @'a\\'b' = 1; DROP TABLE accounts; -- ';",
                'SET @"a\\"b" = 1; DROP TABLE accounts; -- ";',
                "SELECT `a\\`; DROP TABLE accounts; -- `;",
                "UPDATE accounts SET nickname = @@global.where;",
                "DELETE FROM accounts ORDER BY @where;",
                "CREATE EVENT cleanup ON SCHEDULE EVERY 1 DAY DO CALL cleanup_accounts();",
                "ALTER TABLE accounts DISCARD TABLESPACE;",
                "ALTER TABLE accounts CONVERT TO CHARACTER SET ascii;",
                "DROP TEMPORARY TABLE accounts;",
                "ALTER TABLE `accounts` DROP COLUMN `balance`;",
                "ALTER TABLE accounts ADD (id int, region text NOT NULL);",
                "ALTER TABLE accounts MODIFY COLUMN balance smallint;",
                "ALTER TABLE accounts CHANGE COLUMN balance old_balance int;",
                "RENAME TABLE accounts TO archived_accounts;",
                "DELETE accounts FROM accounts JOIN other ON accounts.id = other.id;",
                "/*!50000 DROP TABLE accounts */;",
                "/*M!100100 TRUNCATE accounts */;",
                "SELECT 1--2; DROP TABLE accounts;",
            ],
        }
        for dialect in ("postgres", "mysql"):
            for sql in common + extra[dialect]:
                with self.subTest(dialect=dialect, sql=sql):
                    self.assertTrue(guard.check_sql(sql, dialect))
                    self.assertEqual(guard.check_sql("-- contract: https://example.com/releases/expand-42\n" + sql, dialect), [])

    def test_additive_and_scoped_data_changes_pass(self):
        safe = [
            "CREATE TABLE accounts (id int NOT NULL, region varchar(8) NOT NULL);",
            "CREATE TABLE event (id int, event text);",
            "ALTER TABLE accounts ADD COLUMN nickname text;",
            "ALTER TABLE accounts ADD COLUMN region text NOT NULL DEFAULT 'unknown';",
            "ALTER TABLE accounts ADD COLUMN region text NOT NULL DEFAULT concat('a', 'b');",
            "ALTER TABLE accounts ADD COLUMN id bigint GENERATED ALWAYS AS IDENTITY NOT NULL;",
            "ALTER TABLE accounts ADD COLUMN id bigserial NOT NULL;",
            "ALTER TABLE accounts ADD CHECK (balance IS NOT NULL);",
            "ALTER TABLE accounts DROP CONSTRAINT balance_check;",
            "ALTER TABLE accounts ALTER COLUMN region SET DEFAULT 'unknown'; ALTER TABLE accounts ALTER COLUMN region SET NOT NULL;",
            "ALTER TABLE accounts ALTER COLUMN region SET DEFAULT 'unknown', ALTER COLUMN region SET NOT NULL;",
            "CREATE TABLE fresh (id int); ALTER TABLE fresh ADD region text NOT NULL;",
            "DELETE FROM accounts WHERE id = 42;",
            "UPDATE accounts SET balance = 0 WHERE id = 42;",
            "WITH selected AS (SELECT id FROM other) DELETE FROM accounts WHERE id IN (SELECT id FROM selected);",
            "WITH gone AS (DELETE FROM accounts WHERE id = 42 RETURNING id) SELECT * FROM gone;",
            "SELECT * FROM accounts FOR UPDATE;",
            "CREATE TABLE links (id int REFERENCES accounts ON UPDATE CASCADE);",
            "INSERT INTO accounts (id) VALUES (1) ON DUPLICATE KEY UPDATE id = 1;",
            "INSERT INTO accounts (id) VALUES (1) ON CONFLICT (id) DO UPDATE SET id = 1;",
            "SELECT TRUNCATE(1.23, 1);",
            "SELECT 'DROP TABLE accounts; DELETE FROM accounts;'; -- TRUNCATE accounts\n",
            "/* DROP TABLE accounts; */ SELECT 1;",
        ]
        for dialect in ("postgres", "mysql"):
            for sql in safe:
                with self.subTest(dialect=dialect, sql=sql):
                    self.assertEqual(guard.check_sql(sql, dialect), [])

    def test_defaults_do_not_leak_between_tables_columns_or_later_statements(self):
        for sql in [
            "ALTER TABLE other ALTER region SET DEFAULT 'x'; ALTER TABLE accounts ALTER region SET NOT NULL;",
            "ALTER TABLE accounts ALTER nickname SET DEFAULT 'x'; ALTER TABLE accounts ALTER region SET NOT NULL;",
            "ALTER TABLE accounts ALTER region SET NOT NULL; ALTER TABLE accounts ALTER region SET DEFAULT 'x';",
            "ALTER TABLE accounts ALTER region SET DEFAULT 'x'; ALTER TABLE accounts ALTER region DROP DEFAULT; ALTER TABLE accounts ALTER region SET NOT NULL;",
            'ALTER TABLE "Accounts" ALTER region SET DEFAULT 1; ALTER TABLE accounts ALTER region SET NOT NULL;',
        ]:
            with self.subTest(sql=sql):
                self.assertTrue(guard.check_sql(sql, "postgres"))

    def test_marker_must_be_real_nonempty_comment(self):
        for marker in ["-- contract:", "-- contract: !!!", "/* -- contract: release-1 */", "SELECT '-- contract: release-1';", "SELECT $$\n-- contract: release-1\n$$;"]:
            with self.subTest(marker=marker):
                self.assertTrue(guard.check_sql(marker + "\nDROP TABLE accounts;", "postgres"))
        self.assertEqual(guard.check_sql("  -- CONTRACT: release-1\nDROP TABLE accounts;", "postgres"), [])

    def test_unterminated_sql_fails_even_with_marker(self):
        for sql in ["SELECT 'oops", "/* oops", "DO $$oops"]:
            with self.subTest(sql=sql), self.assertRaises(guard.SqlError):
                guard.check_sql("-- contract: release-1\n" + sql, "postgres")

    def test_comments_strings_and_identifiers_preserve_lines(self):
        sql = "SELECT '-- not a comment', 'it''s safe';\n/* comment */\nALTER TABLE accounts DROP COLUMN balance;"
        self.assertIn((3, "DROP COLUMN"), guard.check_sql(sql, "postgres"))
        self.assertEqual(guard.check_sql('CREATE TABLE "drop" ("rename" text, "truncate" text);', "postgres"), [])
        self.assertEqual(guard.check_sql("SELECT 'a\\\'--b';", "mysql"), [])
        self.assertEqual(guard.check_sql("SET @'a\\'b' = 1;", "mysql"), [])
        self.assertEqual(guard.check_sql('SET @"a\\"b" = 1;', "mysql"), [])


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        self.write("db/0001_initial.sql", "CREATE TABLE accounts (id int);")
        self.base = self.commit()

    def git(self, *args):
        return guard.git(self.root, *args).strip()

    def write(self, path, content):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-qm", "Fixture")
        return self.git("rev-parse", "HEAD")

    def check(self, dirs=None):
        return guard.check_range(self.root, self.base, "HEAD", dirs or ["db"], "postgres")

    def test_new_nested_migration_is_inspected(self):
        self.write("db/nested/0002_drop.sql", "ALTER TABLE accounts DROP COLUMN balance;")
        self.commit()
        errors, count = self.check()
        self.assertEqual(count, 1)
        self.assertIn("DROP COLUMN", "\n".join(errors))

    def test_additive_migration_and_missing_future_directory_pass(self):
        self.write("db/0002_nickname.sql", "ALTER TABLE accounts ADD nickname text;")
        self.commit()
        self.assertEqual(self.check(["db", "future"]), ([], 1))

    def test_merged_edit_with_marker_still_fails(self):
        self.write("db/0001_initial.sql", "-- contract: release-1\nCREATE TABLE accounts (id bigint);")
        self.commit()
        self.assertIn("merged migration", "\n".join(self.check()[0]))

    def test_merged_delete_and_rename_fail(self):
        self.git("mv", "db/0001_initial.sql", "db/0002_initial.sql")
        self.commit()
        self.assertIn("merged migration", "\n".join(self.check()[0]))
        self.git("rm", "db/0002_initial.sql")
        self.commit()
        self.assertIn("merged migration", "\n".join(self.check()[0]))

    def test_numbered_migration_must_follow_merged_chain_even_with_marker(self):
        self.write("db/0000_backdated.sql", "-- contract: release-1\nSELECT 1;")
        self.commit()
        self.assertIn("above 1", "\n".join(self.check()[0]))

    def test_duplicate_new_versions_fail(self):
        self.write("db/0002_one.sql", "SELECT 1;")
        self.write("db/0002_two.sql", "SELECT 2;")
        self.commit()
        self.assertIn("unique", "\n".join(self.check()[0]))

    def test_symlink_cannot_read_uninspected_sql(self):
        os.symlink("0001_initial.sql", self.root / "db/0002_link.sql")
        self.commit()
        self.assertIn("regular SQL file", "\n".join(self.check()[0]))

    def test_branch_behind_main_uses_common_ancestor(self):
        self.git("checkout", "-qb", "feature")
        self.write("note.txt", "feature")
        feature = self.commit()
        self.git("checkout", "-q", "main")
        self.write("db/0002_main.sql", "SELECT 1;")
        newer_main = self.commit()
        self.assertEqual(guard.check_range(self.root, newer_main, feature, ["db"], "postgres"), ([], 0))

    def test_main_reports_failure_and_never_executes_sql(self):
        self.write("db/0002_drop.sql", "DROP TABLE accounts;")
        self.commit()
        env = {"GITHUB_WORKSPACE": str(self.root), "MIGRATION_BASE": self.base, "MIGRATION_HEAD": "HEAD", "MIGRATION_DIALECT": "postgres", "MIGRATION_DIRECTORIES": "db\nfuture"}
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(guard.main([], env), 1)
        self.assertIn("DROP TABLE", output.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(guard.main([], {**env, "MIGRATION_BASE": "missing-ref"}), 1)


class EventAndManifestTests(unittest.TestCase):
    def test_pr_group_push_and_explicit_ranges(self):
        for event, expected in [
            ({"pull_request": {"base": {"sha": "pr-base"}, "head": {"sha": "pr-head"}}}, ("pr-base", "pr-head")),
            ({"merge_group": {"base_sha": "group-base", "head_sha": "group-head"}}, ("group-base", "group-head")),
            ({"before": "push-base"}, ("push-base", "push-head")),
        ]:
            with self.subTest(event=event), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "event.json"
                path.write_text(json.dumps(event))
                env = {"GITHUB_EVENT_PATH": str(path), "GITHUB_SHA": "push-head"}
                self.assertEqual(guard.event_range(env), expected)
                self.assertEqual(guard.event_range({**env, "MIGRATION_BASE": "explicit-base", "MIGRATION_HEAD": "explicit-head"}), ("explicit-base", "explicit-head"))

    def test_no_base_and_invalid_configuration_fail_closed(self):
        for env in [{}, {"MIGRATION_BASE": "0" * 40}]:
            with self.assertRaises(ValueError):
                guard.event_range(env)
        for raw in ["", "../outside", "/absolute", "db/*.sql"]:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                guard.directories(raw)

    def test_manifest_uses_environment_and_bundled_script(self):
        import yaml
        manifest = yaml.safe_load((ACTION / "action.yml").read_text())
        step = manifest["runs"]["steps"][0]
        self.assertEqual(manifest["runs"]["using"], "composite")
        self.assertIn("$GITHUB_ACTION_PATH/migration_guard.py", step["run"])
        self.assertNotIn("${{", step["run"])
        self.assertEqual(step["env"]["MIGRATION_DIRECTORIES"], "${{ inputs.migration-directories }}")


if __name__ == "__main__":
    unittest.main()
