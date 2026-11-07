"""Opt-in parser selection for mixed-engine migration directories."""
import contextlib
import io
import os
import json
import sys
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_migration_guard import guard


MYSQL = """DELIMITER $$
CREATE TRIGGER immutable_update BEFORE UPDATE ON accounts FOR EACH ROW
BEGIN SIGNAL SQLSTATE '45000'; END$$
DELIMITER ;"""
POSTGRES = """CREATE FUNCTION immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'immutable'; END $$;
CREATE TRIGGER immutable_update BEFORE UPDATE OR DELETE ON accounts
FOR EACH ROW EXECUTE FUNCTION immutable();"""


class DialectSuffixTests(unittest.TestCase):
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

    def write(self, path, sql):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(sql)

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-qm", "Fixture")
        return self.git("rev-parse", "HEAD")

    def check(self, dialect="mysql", **kwargs):
        return guard.check_range(self.root, self.base, "HEAD", ["db"], dialect, **kwargs)

    def mixed(self, fallback):
        self.write("db/0002_trigger.mysql.sql", MYSQL)
        self.write("db/0003_trigger.postgresql.sql", POSTGRES)
        self.write("db/0004_fallback.sql", MYSQL if fallback == "mysql" else POSTGRES)
        self.commit()

    def test_mixed_directory_and_unsuffixed_fallback(self):
        for fallback in ("mysql", "postgres"):
            with self.subTest(fallback=fallback):
                self.mixed(fallback)
                self.assertEqual(self.check(fallback, dialect_from_suffix=True), ([], 3))
                default = self.check(fallback)
                self.assertTrue(default[0])
                self.assertEqual(self.check(fallback, dialect_from_suffix=False), default)

    def test_destructive_sql_is_refused_in_every_suffix_class(self):
        for suffix in (".mysql.sql", ".postgresql.sql", ".sql"):
            for sql, reason in (("UPDATE accounts SET id=0;", "UPDATE without WHERE"),
                                ("ALTER TABLE accounts DROP COLUMN id;", "DROP COLUMN")):
                with self.subTest(suffix=suffix, sql=sql):
                    self.git("reset", "--hard", self.base)
                    path = "db/0002_bad" + suffix
                    self.write(path, sql)
                    self.commit()
                    self.assertEqual(self.check(dialect_from_suffix=True), ([f"{path}:1: {reason}"], 1))

    def test_routed_trigger_bodies_cannot_hide_unscoped_mutations(self):
        for suffix, sql in ((".mysql.sql", MYSQL.replace("SIGNAL SQLSTATE '45000'", "UPDATE accounts SET id=0")),
                            (".postgresql.sql", POSTGRES.replace("RAISE EXCEPTION 'immutable'", "UPDATE accounts SET id=0"))):
            with self.subTest(suffix=suffix):
                self.git("reset", "--hard", self.base)
                path = "db/0002_bad" + suffix
                self.write(path, sql)
                self.commit()
                errors, count = self.check(dialect_from_suffix=True)
                self.assertEqual(count, 1)
                self.assertTrue(any(e.startswith(path + ":") and "UPDATE without WHERE" in e for e in errors), errors)

    def test_suffix_matching_is_case_insensitive_and_not_a_path_substring(self):
        self.write("db/0002_trigger.MYSQL.SQL", MYSQL)
        self.write("db/0003_trigger.POSTGRESQL.SQL", POSTGRES)
        self.write("db/folder.postgresql.sql/0001_fallback.sql", MYSQL)
        self.write("db/0004_other.postgres.sql", MYSQL)
        self.commit()
        self.assertEqual(self.check(dialect_from_suffix=True), ([], 4))

    def test_merged_files_remain_immutable_in_every_suffix_class(self):
        paths = ["db/0002_old.mysql.sql", "db/0003_old.postgresql.sql", "db/0004_old.sql"]
        for path in paths:
            self.write(path, "SELECT 1;")
        self.base = self.commit()
        for path in paths:
            for operation in ("edit", "delete", "rename"):
                with self.subTest(path=path, operation=operation):
                    self.git("reset", "--hard", self.base)
                    if operation == "edit":
                        self.write(path, "-- contract: release-42\nSELECT 2;")
                    elif operation == "delete":
                        (self.root / path).unlink()
                    else:
                        (self.root / path).rename(self.root / (path + ".mysql.sql"))
                    self.commit()
                    errors, _ = self.check(dialect_from_suffix=True)
                    self.assertIn(path + ": merged migration was modified, deleted or renamed; append a new migration instead", errors)

    def test_historical_table_identity_is_scoped_to_effective_dialect(self):
        self.write("db/0002_prior.mysql.sql", "CREATE TABLE child (id int);")
        self.base = self.commit()
        sql = "CREATE TABLE child (id int REFERENCES accounts(id) ON DELETE CASCADE);"
        self.write("db/0003_fresh.postgresql.sql", sql)
        self.commit()
        self.assertEqual(self.check(dialect_from_suffix=True), ([], 1))
        self.write("db/0004_existing.mysql.sql", sql)
        self.commit()
        errors, count = self.check(dialect_from_suffix=True)
        self.assertEqual(count, 2)
        self.assertEqual(len(errors), 1)
        self.assertIn("0004_existing.mysql.sql", errors[0])
        self.assertIn("ON DELETE CASCADE changes child data", errors[0])

    def test_schema_snapshots_keep_the_configured_dialect(self):
        self.write("schema.postgresql.sql", MYSQL)
        self.commit()
        self.assertEqual(guard.check_range(self.root, self.base, "HEAD", [], "mysql",
                                          schemas=["schema.postgresql.sql"], dialect_from_suffix=True), ([], 0))

    def test_merge_group_target_comparison_receives_suffix_mode(self):
        self.git("checkout", "-qb", "queue-ahead")
        self.write("db/0002_trigger.postgresql.sql", POSTGRES)
        ahead = self.commit()
        self.write("db/0003_trigger.mysql.sql", MYSQL)
        head = self.commit()
        event = self.root / "event.json"
        event.write_text(json.dumps({"merge_group": {
            "base_ref": "refs/heads/main", "base_sha": ahead, "head_sha": head}}))
        env = {"GITHUB_WORKSPACE": str(self.root), "GITHUB_EVENT_PATH": str(event),
               "MIGRATION_DIALECT": "mysql", "MIGRATION_DIRECTORIES": "db",
               "MIGRATION_DIALECT_FROM_SUFFIX": "true"}
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(guard.main([], env), 0)
        self.assertIn("2 new SQL migration file(s)", output.getvalue())

    def test_cli_and_environment_select_the_same_mode_and_reject_invalid_input(self):
        self.mixed("mysql")
        env = {"GITHUB_WORKSPACE": str(self.root), "MIGRATION_BASE": self.base,
               "MIGRATION_HEAD": "HEAD", "MIGRATION_DIALECT": "mysql", "MIGRATION_DIRECTORIES": "db"}
        for args, value, expected in (([], "false", 1), ([], "true", 0),
                                      (["--dialect-from-suffix"], "false", 0),
                                      (["--no-dialect-from-suffix"], "true", 1), ([], "unknown", 1)):
            with self.subTest(args=args, value=value), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(guard.main(args, {**env, "MIGRATION_DIALECT_FROM_SUFFIX": value}), expected)
                if value == "unknown":
                    self.assertIn("dialect-from-suffix must be true or false", output.getvalue())
        result = subprocess.run([sys.executable, guard.__file__, "--dialect-from-suffix"],
                                env={**os.environ, **env, "MIGRATION_DIALECT_FROM_SUFFIX": "false"},
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("3 new SQL migration file(s)", result.stdout)


if __name__ == "__main__":
    unittest.main()
