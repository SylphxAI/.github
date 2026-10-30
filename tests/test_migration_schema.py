"""Snapshot refusal cases and Git-object integration for migration-guard."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("migration_schema_guard", ROOT / ".github/actions/migration-guard/migration_guard.py")
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)

BASE = "CREATE TABLE accounts (id bigint NOT NULL, nickname text, region varchar(24));"


def dump(columns="id bigint NOT NULL, nickname text", counter=42, comment="table"):
    return f"""/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
DROP TABLE IF EXISTS `accounts`;
/*!40101 SET @saved_cs_client = @@character_set_client */;
CREATE TABLE `accounts` ({columns}, PRIMARY KEY (id))
ENGINE=InnoDB AUTO_INCREMENT={counter} DEFAULT CHARSET=utf8mb4 COMMENT='{comment}';
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;
"""


class SchemaTests(unittest.TestCase):
    def test_new_foreign_key_child_mutations_require_contract_references(self):
        plain = "CREATE TABLE accounts (id bigint, owner_id bigint);"
        for dialect in ("postgres", "mysql"):
            for inline in (False, True):
                def referenced(action):
                    reference = "REFERENCES owners(id)" + (f" ON DELETE {action}" if action else "")
                    return f"CREATE TABLE accounts (id bigint, owner_id bigint {reference});" if inline else f"CREATE TABLE accounts (id bigint, owner_id bigint, CONSTRAINT owner_fk FOREIGN KEY (owner_id) {reference});"
                for action in ("", "RESTRICT", "NO ACTION"):
                    with self.subTest(dialect=dialect, inline=inline, action=action):
                        self.assertEqual(guard.check_schema(plain, referenced(action), dialect), [])
                for action in ("CASCADE", "SET NULL"):
                    changed = referenced(action)
                    with self.subTest(dialect=dialect, inline=inline, action=action):
                        self.assertIn(f"ON DELETE {action}", str(guard.check_schema(plain, changed, dialect)))
                        self.assertTrue(guard.check_schema("", changed, dialect))
                        self.assertEqual(guard.check_schema(plain, "-- contract: released-expand-42\n" + changed, dialect), [])
                        self.assertEqual(guard.check_schema(changed, changed.replace("id bigint,", "id bigint, nickname text,", 1), dialect), [])
                        self.assertEqual(guard.check_schema(changed, changed.replace("owner_fk", "renamed_fk"), dialect), [])
                self.assertTrue(guard.check_schema(referenced("CASCADE"), referenced("SET NULL"), dialect))

    def test_table_column_rename_removal_type_and_required_column_refusals(self):
        changes = [
            ("", "table"),
            (BASE.replace("accounts", "archived_accounts"), "removed or renamed"),
            (BASE.replace("nickname text, ", ""), "column"),
            (BASE.replace("nickname", "old_nickname"), "removed or renamed"),
            (BASE.replace("varchar(24)", "varchar(2)"), "type changed"),
            (BASE.replace("nickname text", "nickname text NOT NULL"), "NOT NULL"),
            (BASE.replace("nickname text", "nickname text PRIMARY KEY"), "NOT NULL"),
            (BASE.replace("nickname text", "nickname text, PRIMARY KEY (nickname)"), "NOT NULL"),
            (BASE.replace("nickname text", "nickname text, status text NOT NULL"), "NOT NULL"),
            (BASE.replace("nickname text", "nickname text, status text NOT NULL DEFAULT NULL"), "NOT NULL"),
        ]
        for dialect in ("postgres", "mysql"):
            for head, reason in changes:
                with self.subTest(dialect=dialect, head=head):
                    self.assertIn(reason, str(guard.check_schema(BASE, head, dialect)))
                    self.assertEqual(guard.check_schema(BASE, "-- contract: released-expand-42\n" + head, dialect), [])

    def test_additive_changes_defaults_identity_and_new_tables_pass(self):
        changes = [
            BASE,
            BASE.replace("nickname text", "nickname text, status text"),
            BASE.replace("nickname text", "nickname text, status text NOT NULL DEFAULT 'new'"),
            BASE.replace("nickname text", "nickname text, status text NOT NULL DEFAULT ('new')"),
            BASE.replace("nickname text", "nickname text NOT NULL DEFAULT 'unknown'"),
            BASE.replace("nickname text", "nickname text, sequence_id bigint GENERATED ALWAYS AS IDENTITY NOT NULL"),
            BASE + " CREATE TABLE fresh (id bigint NOT NULL, region text NOT NULL);",
            BASE + " UPDATE accounts SET nickname = 'new' WHERE id = 42;",
            "-- harmless\n" + BASE.replace(",", ",\n"),
        ]
        for dialect in ("postgres", "mysql"):
            for head in changes:
                with self.subTest(dialect=dialect, head=head):
                    self.assertEqual(guard.check_schema(BASE, head, dialect), [])

    def test_dump_scaffolding_and_volatile_options_are_not_new_migrations(self):
        self.assertEqual(guard.check_schema(dump(), dump(counter=99, comment="updated"), "mysql"), [])
        self.assertIn("removed", str(guard.check_schema(dump(), dump("id bigint NOT NULL"), "mysql")))
        self.assertIn("options changed", str(guard.check_schema(dump(), dump().replace("utf8mb4", "ascii"), "mysql")))
        self.assertEqual(guard.check_schema("", BASE, "mysql"), [])

    def test_literal_type_changes_and_quoted_names_remain_distinct(self):
        self.assertIn("type changed", str(guard.check_schema("CREATE TABLE t (state enum('a','b'));", "CREATE TABLE t (state enum('a','c'));", "mysql")))
        self.assertIn("type changed", str(guard.check_schema("CREATE TABLE t (name varchar(24) DEFAULT '' COLLATE utf8mb4_bin);", "CREATE TABLE t (name varchar(24) DEFAULT '' COLLATE ascii_bin);", "mysql")))
        self.assertIn("removed", str(guard.check_schema('CREATE TABLE "a.b" (id int);', "CREATE TABLE a.b (id int);", "postgres")))
        self.assertIn("removed", str(guard.check_schema("CREATE TABLE accounts (id int);", "CREATE TABLE Accounts (id int);", "mysql")))
        self.assertEqual(guard.check_schema("CREATE TABLE accounts (Name text);", "CREATE TABLE accounts (name text);", "mysql"), [])
        self.assertIn("removed", str(guard.check_schema('CREATE TABLE accounts ("Name" text);', 'CREATE TABLE accounts ("name" text);', "postgres")))
        self.assertIn("NOT NULL", str(guard.check_schema("CREATE TABLE accounts (index int);", "CREATE TABLE accounts (index int NOT NULL);", "postgres")))

    def test_new_and_changed_other_statements_are_inspected(self):
        for dialect in ("postgres", "mysql"):
            for sql in ["DELETE FROM accounts;", "UPDATE accounts SET nickname = 'new';", "TRUNCATE accounts;", "DROP SCHEMA app;", "CALL cleanup();"]:
                with self.subTest(dialect=dialect, sql=sql):
                    self.assertTrue(guard.check_schema(BASE, BASE + sql, dialect))
        existing = BASE + "UPDATE accounts SET nickname = 'old';"
        self.assertEqual(guard.check_schema(existing, existing, "mysql"), [])
        self.assertIn("without WHERE", str(guard.check_schema(existing, existing.replace("'old'", "'new'"), "mysql")))
        duplicate = dump().replace("CREATE TABLE", "DROP TABLE IF EXISTS `accounts`; CREATE TABLE", 1)
        self.assertIn("DROP TABLE", str(guard.check_schema(dump(), duplicate, "mysql")))
        with self.assertRaises(guard.SqlError):
            guard.check_schema(dump(), dump() + "DROP TABLE accounts;", "mysql")

    def test_persisted_or_fake_contract_markers_never_authorize_later_changes(self):
        removed = BASE.replace("nickname text, ", "")
        prior = "-- contract: released-expand-1\n"
        self.assertTrue(guard.check_schema(prior + BASE, prior + removed, "mysql"))
        for fake in ["/* -- contract: release-2 */\n", "SELECT '-- contract: release-2';\n", "-- contract: !!!\n"]:
            with self.subTest(fake=fake):
                self.assertTrue(guard.check_schema(BASE, fake + removed, "mysql"))
        self.assertEqual(guard.check_schema(prior + BASE, prior + "-- contract: released-expand-2\n" + removed, "mysql"), [])

    def test_ambiguous_and_unsupported_definitions_fail_even_with_marker(self):
        for sql in [
            "CREATE TABLE t (id int); CREATE TABLE t (other text);",
            "CREATE TABLE t (id int, id text);",
            "CREATE TABLE t AS SELECT 1;", "CREATE TABLE t (LIKE accounts);",
            "CREATE TABLE t (id int) AS SELECT 1;",
            "CREATE TABLE t (id int) INHERITS (accounts);",
            "CREATE TABLE t (id int, PRIMARY KEY (missing));",
            "CREATE TABLE t (id);", "CREATE TABLE t (id int;",
            "ALTER TABLE accounts ADD nickname text;", "SELECT 'unterminated",
        ]:
            with self.subTest(sql=sql), self.assertRaises(guard.SqlError):
                guard.check_schema(BASE, "-- contract: released-expand-42\n" + sql, "postgres")

    def test_unsupported_old_quote_modes_cannot_hide_new_sql(self):
        for dialect, sql in [
            ("mysql", "SET sql_mode='NO_BACKSLASH_ESCAPES';"),
            ("mysql", "SET @@session.`sql_mode`='ANSI_QUOTES';"),
            ("postgres", 'SET "standard_conforming_strings"=off;'),
        ]:
            with self.subTest(dialect=dialect), self.assertRaises(guard.SqlError):
                guard.check_schema(sql + BASE, sql + BASE, dialect)


class SchemaHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        self.write("db/schema.sql", dump())
        self.write("migrations/0001_initial.sql", "CREATE TABLE accounts (id int);")
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

    def check(self, schemas=None, dirs=None):
        return guard.check_range(self.root, self.base, "HEAD", dirs or [], "mysql", schemas or ["db/schema.sql"])

    def test_git_blobs_are_checked_instead_of_working_tree(self):
        self.write("db/schema.sql", dump("id bigint NOT NULL"))
        self.commit()
        self.write("db/schema.sql", dump())
        self.assertIn("removed", str(self.check()[0]))

    def test_missing_deleted_and_nonregular_schemas_fail_closed(self):
        self.assertIn("missing", str(self.check(["db/typo.sql"])[0]))
        self.git("rm", "db/schema.sql")
        self.commit()
        self.assertIn("deleted", str(self.check()[0]))
        (self.root / "db").mkdir(exist_ok=True)
        os.symlink("../migrations/0001_initial.sql", self.root / "db/schema.sql")
        self.commit()
        self.assertIn("regular SQL", str(self.check()[0]))

    def test_new_schema_and_combined_migration_scopes(self):
        self.write("db/future.sql", BASE)
        self.write("migrations/0002_expand.sql", "ALTER TABLE accounts ADD nickname text;")
        self.commit()
        self.assertEqual(self.check(["db/schema.sql", "db/future.sql"], ["migrations"]), ([], 1))
        self.write("migrations/0001_initial.sql", "-- contract: release-1\nDROP TABLE accounts;")
        self.commit()
        self.assertIn("merged migration", str(self.check(dirs=["migrations"])[0]))

    def test_contract_reference_is_scoped_to_its_changed_snapshot(self):
        self.write("db/other.sql", BASE)
        self.base = self.commit()
        self.write("db/schema.sql", "-- contract: release-2\n" + dump())
        self.write("db/other.sql", BASE.replace("nickname text, ", ""))
        self.commit()
        errors, _ = self.check(["db/schema.sql", "db/other.sql"])
        self.assertIn("db/other.sql", str(errors))
        self.assertNotIn("db/schema.sql", str(errors))

    def test_schema_only_cli_input_and_empty_scope_failure(self):
        env = {"GITHUB_WORKSPACE": str(self.root), "MIGRATION_DIALECT": "mysql", "MIGRATION_BASE": self.base, "MIGRATION_HEAD": "HEAD", "MIGRATION_SCHEMA_FILES": "db/schema.sql"}
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(guard.main([], env), 0)
        self.assertIn("1 schema snapshot", output.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(guard.main([], {**env, "MIGRATION_SCHEMA_FILES": ""}), 1)
            self.assertEqual(guard.main(["--schema-file", "db/schema.sql"], {**env, "MIGRATION_SCHEMA_FILES": ""}), 0)
            self.assertEqual(guard.main([], {**env, "MIGRATION_SCHEMA_FILES": "../schema.sql"}), 1)


if __name__ == "__main__":
    unittest.main()
