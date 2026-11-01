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
import unittest.mock

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
            "GRANT SELECT, INSERT, UPDATE, DELETE ON accounts TO app_role;",
            "REVOKE UPDATE, DELETE ON accounts FROM app_role;",
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


class ConcurrentIndexTests(unittest.TestCase):
    declaration = {"schema": "public", "table": "events", "index": "events_owner_idx",
                   "columns": ["owner_id"], "where_null": "deleted_at",
                   "lock_timeout_ms": 3000, "statement_timeout_ms": 240000}
    statement = 'CREATE INDEX CONCURRENTLY "events_owner_idx" ON "public"."events" USING btree ("owner_id") WHERE "deleted_at" IS NULL;'

    def source(self, declaration=None, statement=None):
        return ("-- atlas:txmode none\n-- migration:concurrent-index-v1 "
                + json.dumps(self.declaration if declaration is None else declaration)
                + "\n" + (self.statement if statement is None else statement) + "\n")

    def test_future_unsafe_concurrent_index_is_rejected(self):
        for sql in [
            "SET lock_timeout='5s'; DROP INDEX CONCURRENTLY IF EXISTS public.future_idx; CREATE INDEX CONCURRENTLY future_idx ON public.events(owner_id);",
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS future_idx ON public.events(owner_id);",
            "CREATE /* comment */ UNIQUE INDEX CONCURRENTLY future_idx ON public.events(owner_id);",
        ]:
            for marker in ("", "-- contract: released-expand-42\n"):
                with self.subTest(sql=sql, marker=marker):
                    self.assertTrue(guard.check_sql(marker + sql, "postgres"))

    def test_supported_contract_requires_explicit_runtime_adoption(self):
        self.assertTrue(guard.check_sql(self.source(), "postgres"))
        self.assertEqual(guard.check_sql(self.source(), "postgres", "atlas-qualified-v1"), [])

    def test_finite_bounds_and_exact_supported_statement(self):
        for key, value in [("lock_timeout_ms", 0), ("statement_timeout_ms", 0),
                           ("lock_timeout_ms", 3001), ("statement_timeout_ms", 240001),
                           ("lock_timeout_ms", True), ("statement_timeout_ms", "240000")]:
            with self.subTest(key=key, value=value):
                self.assertTrue(guard.check_sql(self.source(self.declaration | {key: value}), "postgres", "atlas-qualified-v1"))
        for statement in [self.statement.replace("CREATE INDEX", "CREATE UNIQUE INDEX"),
                          self.statement.replace("CONCURRENTLY", "CONCURRENTLY IF NOT EXISTS"),
                          self.statement + "\nSELECT 1;", self.statement.replace('"owner_id"', 'lower("owner_id")'),
                          "SET lock_timeout='3s';\n" + self.statement]:
            with self.subTest(statement=statement):
                self.assertTrue(guard.check_sql(self.source(statement=statement), "postgres", "atlas-qualified-v1"))
        for declaration in [self.declaration | {"unknown": 1}, self.declaration | {"columns": []},
                            self.declaration | {"table": "bad\nname"}, self.declaration | {"schema": "x" * 64}]:
            self.assertTrue(guard.check_sql(self.source(declaration), "postgres", "atlas-qualified-v1"))
        self.assertTrue(guard.check_sql(self.source().replace('-- atlas:txmode none', '-- atlas:txmode none\n-- atlas:txmode file'), "postgres", "atlas-qualified-v1"))
        self.assertTrue(guard.check_sql(self.source().replace('{"schema":', '{"schema":"other","schema":'), "postgres", "atlas-qualified-v1"))

    def test_quoted_identifiers_are_generated_without_interpolation(self):
        declaration = self.declaration | {"schema": "Tenant", "table": 'Event"Log', "index": "Owner Index"}
        statement = 'CREATE INDEX CONCURRENTLY "Owner Index" ON "Tenant"."Event""Log" USING btree ("owner_id") WHERE "deleted_at" IS NULL;'
        self.assertEqual(guard.check_sql(self.source(declaration, statement), "postgres", "atlas-qualified-v1"), [])
        with self.assertRaises(guard.SqlError):
            guard.check_sql(self.source(declaration, statement.replace('"Event""Log"', '"Event"Log"')), "postgres", "atlas-qualified-v1")


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

    def test_actual_added_path_adopts_concurrent_contract_without_rechecking_history(self):
        old = "CREATE INDEX CONCURRENTLY IF NOT EXISTS historical_idx ON public.accounts(id);"
        self.write("db/0002_historical.sql", old)
        self.base = self.commit()
        fixture = ConcurrentIndexTests()
        self.write("db/0003_future.sql", fixture.source())
        self.commit()
        self.assertTrue(self.check()[0])
        self.assertEqual(guard.check_range(self.root, self.base, "HEAD", ["db"], "postgres",
                                          concurrent_index_runtime="atlas-qualified-v1"), ([], 1))
        self.write("db/0002_historical.sql", old + "\n-- changed")
        self.commit()
        self.assertIn("merged migration", "\n".join(guard.check_range(
            self.root, self.base, "HEAD", ["db"], "postgres", concurrent_index_runtime="atlas-qualified-v1")[0]))

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

    def test_recorded_unapplied_withdrawal_and_refusals(self):
        import hashlib
        unapplied = "db/20261217091104_unapplied_kind.sql"
        sql = "ALTER TABLE accounts ADD COLUMN nickname text;\n"
        self.write(unapplied, sql)
        base = self.commit()
        digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        valid_entry = {
            "path": unapplied,
            "sha256": digest,
            "evidence": {
                "observed_at": "2026-10-31T03:33:58Z",
                "database": "platform/sylphx_prod",
                "atlas_revision_rows": 0,
                "file_digest_rows": 0,
                "positive_control_version": "20261217010724",
                "reason": "Never applied in production; replaced by 20261218033442.",
            },
        }
        self.git("rm", unapplied)
        self.write("scripts/migrations/unapplied-withdrawals.json", json.dumps([valid_entry]))
        self.write("db/20261218033442_replacement.sql", "ALTER TABLE accounts ADD COLUMN nickname text;\n")
        self.commit()
        self.assertEqual(guard.check_range(self.root, base, "HEAD", ["db"], "postgres"), ([], 1))
        with unittest.mock.patch.dict(os.environ, {"ATLAS_APPLIED_VERSIONS": "20261217091104"}):
            self.assertIn("merged migration", "\n".join(guard.check_range(self.root, base, "HEAD", ["db"], "postgres")[0]))
        for bad_evidence in [
            {**valid_entry["evidence"], "atlas_revision_rows": 1},
            {**valid_entry["evidence"], "atlas_revision_rows": False},
            {**valid_entry["evidence"], "file_digest_rows": 1},
            {**valid_entry["evidence"], "positive_control_version": "20261217091104"},
            {**valid_entry["evidence"], "positive_control_version": "123"},
            {**valid_entry["evidence"], "observed_at": "not-a-timestamp"},
            {**valid_entry["evidence"], "database": "   "},
            {**valid_entry["evidence"], "reason": ""},
        ]:
            self.write("scripts/migrations/unapplied-withdrawals.json", json.dumps([{**valid_entry, "evidence": bad_evidence}]))
            self.commit()
            self.assertIn("merged migration", "\n".join(guard.check_range(self.root, base, "HEAD", ["db"], "postgres")[0]))
        self.write("scripts/migrations/unapplied-withdrawals.json", json.dumps([{**valid_entry, "sha256": "0" * 64}]))
        self.commit()
        self.assertIn("merged migration", "\n".join(guard.check_range(self.root, base, "HEAD", ["db"], "postgres")[0]))
        self.write("scripts/migrations/unapplied-withdrawals.json", json.dumps([valid_entry, valid_entry]))
        self.commit()
        self.assertIn("merged migration", "\n".join(guard.check_range(self.root, base, "HEAD", ["db"], "postgres")[0]))

        archive = "db-withdrawn/20261217091104_unapplied_kind.sql"
        receipt = "docs/audit/2026-10-31-zero-step.json"
        receipt_body = '{"applied": 0, "total": 2}\n'
        receipt_digest = hashlib.sha256(receipt_body.encode("utf-8")).hexdigest()
        self.write(archive, sql)
        self.write(receipt, receipt_body)
        zero_step_entry = {
            "path": unapplied,
            "sha256": digest,
            "archive": archive,
            "evidence": {
                "observed_at": "2026-10-31T16:14:55.854572+00:00",
                "database": "platform/sylphx_prod",
                "atlas_revision_rows": 1,
                "atlas_applied_statements": 0,
                "atlas_total_statements": 2,
                "atlas_error": "pq: lock retry budget exhausted (55P03)",
                "atlas_partial_hashes": None,
                "file_digest_rows": 0,
                "positive_control_version": "20261217010724",
                "schema_objects_absent": ["accounts.nickname"],
                "reason": "Zero-step failed revision archived before replacement.",
                "receipt": receipt,
                "receipt_sha256": receipt_digest,
            },
        }
        self.write("scripts/migrations/unapplied-withdrawals.json", json.dumps([zero_step_entry]))
        self.commit()
        self.assertEqual(guard.check_range(self.root, base, "HEAD", ["db"], "postgres"), ([], 1))
        for bad_zero_step in [
            {**zero_step_entry, "archive": "db-withdrawn/wrong_name.sql"},
            {**zero_step_entry, "evidence": {**zero_step_entry["evidence"], "atlas_applied_statements": 1}},
            {**zero_step_entry, "evidence": {**zero_step_entry["evidence"], "atlas_partial_hashes": ["h1"]}},
            {**zero_step_entry, "evidence": {**zero_step_entry["evidence"], "schema_objects_absent": []}},
            {**zero_step_entry, "evidence": {**zero_step_entry["evidence"], "receipt_sha256": "0" * 64}},
        ]:
            self.write("scripts/migrations/unapplied-withdrawals.json", json.dumps([bad_zero_step]))
            self.commit()
            self.assertIn("merged migration", "\n".join(guard.check_range(self.root, base, "HEAD", ["db"], "postgres")[0]))
        self.write(archive, "ALTER TABLE accounts ADD COLUMN nickname text; -- altered\n")
        self.write("scripts/migrations/unapplied-withdrawals.json", json.dumps([zero_step_entry]))
        self.commit()
        self.assertIn("merged migration", "\n".join(guard.check_range(self.root, base, "HEAD", ["db"], "postgres")[0]))

    def test_catalog_backed_check_constraint_widening_and_refusals(self):
        widening = """
        DO $widen$
        DECLARE
          current_def text;
          kinds text[];
          kind_list text;
        BEGIN
          PERFORM set_config('lock_timeout', '2s', true);
          SELECT pg_get_constraintdef(oid) INTO current_def
            FROM pg_constraint
           WHERE conname = 'child_resources_kind_check'
             AND conrelid = 'public.child_resources'::regclass;
          SELECT array_agg(DISTINCT m[1] ORDER BY m[1]) INTO kinds
            FROM regexp_matches(current_def, '''([A-Za-z.]+)''::text', 'g') AS m;
          SELECT string_agg(quote_literal(k), ', ' ORDER BY k) INTO kind_list
            FROM unnest(kinds || ARRAY['VMPodScrape']) AS k;
          EXECUTE format(
            'ALTER TABLE public.child_resources DROP CONSTRAINT child_resources_kind_check, '
            'ADD CONSTRAINT child_resources_kind_check CHECK (kind = ANY (ARRAY[%s])) NOT VALID',
            kind_list
          );
        END
        $widen$;
        """
        self.assertEqual(guard.check_sql(widening, "postgres"), [])
        for bad in [
            widening.replace("NOT VALID", ""),
            widening.replace("DROP CONSTRAINT child_resources_kind_check", "DROP CONSTRAINT other_check"),
            widening.replace("'lock_timeout'", "'search_path'"),
            widening.replace("EXECUTE format(", "kind_list := '''x'''; EXECUTE format("),
            widening.replace(", '\n            'ADD", ", ' 'ADD"),
        ]:
            with self.subTest(bad=bad):
                self.assertIn("dynamic SQL", str(guard.check_sql(bad, "postgres")))

    def test_concurrent_index_requires_timeouts_and_preceding_drop_without_if_not_exists(self):
        good = """
        -- atlas:txmode none
        SET lock_timeout = '5s';
        SET statement_timeout = '30s';
        DROP INDEX CONCURRENTLY IF EXISTS public.devices_holder_idx;
        CREATE INDEX CONCURRENTLY devices_holder_idx
          ON public.devices (org_id, project_id, env_id, holder_agent_id);
        """
        self.assertEqual(guard.violations(good, "postgres"), [])
        for runtime in ["", "atlas-qualified-v1"]:
            self.assertIn("concurrent index:", str(guard.check_sql(good, "postgres", runtime)))
            self.assertIn("concurrent index:", str(guard.check_sql(
                "-- contract: released-expand\n" + good, "postgres", runtime)))

        good_set_config = """
        -- atlas:txmode none
        SELECT set_config('lock_timeout', '5000ms', false);
        SELECT set_config('statement_timeout', '30000ms', false);
        DROP INDEX CONCURRENTLY IF EXISTS devices_holder_idx;
        CREATE UNIQUE INDEX CONCURRENTLY "devices_holder_idx" ON devices (id);
        """
        self.assertEqual(guard.violations(good_set_config, "postgres"), [])

        # Spiron 20270101170000_devices_holder_index.sql failure shape: missing statement_timeout.
        spiron_missing_stmt_timeout = """
        -- atlas:txmode none
        SET lock_timeout = '5s';
        DROP INDEX CONCURRENTLY IF EXISTS public.devices_holder_idx;
        CREATE INDEX CONCURRENTLY devices_holder_idx
          ON public.devices (org_id, project_id, env_id, holder_agent_id);
        """
        errors = str(guard.violations(spiron_missing_stmt_timeout, "postgres"))
        self.assertIn("DROP INDEX CONCURRENTLY requires preceding non-zero lock_timeout and statement_timeout", errors)
        self.assertIn("CREATE INDEX CONCURRENTLY requires preceding non-zero lock_timeout and statement_timeout", errors)

        for bad, expected in [
            (
                good.replace("SET lock_timeout = '5s';", ""),
                "requires preceding non-zero lock_timeout and statement_timeout",
            ),
            (
                good.replace("SET statement_timeout = '30s';", "SET statement_timeout = '0s';"),
                "requires preceding non-zero lock_timeout and statement_timeout",
            ),
            (
                good.replace("SET statement_timeout = '30s';", "SET statement_timeout = '30s'; RESET statement_timeout;"),
                "requires preceding non-zero lock_timeout and statement_timeout",
            ),
            (
                good.replace("DROP INDEX CONCURRENTLY IF EXISTS public.devices_holder_idx;", ""),
                "requires a preceding DROP INDEX CONCURRENTLY IF EXISTS for the same index",
            ),
            (
                good.replace("DROP INDEX CONCURRENTLY IF EXISTS public.devices_holder_idx;", "DROP INDEX IF EXISTS public.devices_holder_idx;"),
                "requires a preceding DROP INDEX CONCURRENTLY IF EXISTS for the same index",
            ),
            (
                good.replace("DROP INDEX CONCURRENTLY IF EXISTS public.devices_holder_idx;", "DROP INDEX CONCURRENTLY public.devices_holder_idx;"),
                "requires a preceding DROP INDEX CONCURRENTLY IF EXISTS for the same index",
            ),
            (
                good.replace("DROP INDEX CONCURRENTLY IF EXISTS public.devices_holder_idx;", "DROP INDEX CONCURRENTLY IF EXISTS other_schema.devices_holder_idx;"),
                "requires a preceding DROP INDEX CONCURRENTLY IF EXISTS for the same index",
            ),
            (
                good.replace("CREATE INDEX CONCURRENTLY devices_holder_idx", "CREATE INDEX CONCURRENTLY IF NOT EXISTS devices_holder_idx"),
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS skips an invalid index shell",
            ),
            (
                good + "\nCREATE INDEX CONCURRENTLY devices_holder_idx ON public.devices (id);",
                "requires a preceding DROP INDEX CONCURRENTLY IF EXISTS for the same index",
            ),
        ]:
            with self.subTest(bad=bad):
                self.assertIn(expected, str(guard.violations(bad, "postgres")))


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
        self.assertEqual(step["env"]["MIGRATION_CONCURRENT_INDEX_RUNTIME"], "${{ inputs.concurrent-index-runtime }}")


if __name__ == "__main__":
    unittest.main()
