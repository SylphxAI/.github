"""Inspectable routine bodies use the same expand/contract rules as SQL files."""
from pathlib import Path
import importlib.util
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("migration_guard", ROOT / ".github/actions/migration-guard/migration_guard.py")
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


class ProcedureTests(unittest.TestCase):
    def test_additive_reports_and_grant_blocks_pass(self):
        for sql in [
            "CREATE OR REPLACE FUNCTION report() RETURNS bigint LANGUAGE sql AS $$ SELECT count(*) FROM accounts; $$;",
            "CREATE FUNCTION report(p text DEFAULT 'DROP TABLE accounts') RETURNS text AS $$ SELECT p; $$ LANGUAGE sql;",
            "CREATE FUNCTION report() RETURNS text LANGUAGE sql AS 'SELECT ''it''''s safe'';';",
            "CREATE FUNCTION report() RETURNS bigint AS $report$ BEGIN RETURN (SELECT count(*) FROM accounts); END; $report$ LANGUAGE plpgsql;",
            "CREATE PROCEDURE expand() LANGUAGE SQL AS $$ CREATE TABLE fresh (id int); GRANT SELECT ON fresh TO reader; $$;",
            "DO $grants$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='reader') THEN CREATE TABLE fresh (id int); GRANT SELECT ON fresh TO reader; END IF; END; $grants$;",
            "DO LANGUAGE plpgsql 'BEGIN CREATE TABLE fresh (id int); GRANT SELECT ON fresh TO reader; END;';",
            "CREATE FUNCTION report() RETURNS int LANGUAGE sql RETURN 1;",
            "CREATE PROCEDURE expand() LANGUAGE sql BEGIN ATOMIC CREATE TABLE fresh (id int); GRANT SELECT ON fresh TO reader; END;",
            "DO $$ BEGIN RETURN QUERY SELECT 'DROP TABLE accounts'; END $$;",
            "DO $$ BEGIN UPDATE accounts SET active=1 WHERE id=42; END $$;",
            "DO $$ BEGIN CREATE TABLE fresh (id int); ALTER TABLE fresh ADD region text NOT NULL; END; $$;",
            "DO $$ BEGIN IF true THEN ALTER TABLE accounts ALTER region SET DEFAULT '', ALTER region SET NOT NULL; END IF; END; $$;",
        ]:
            with self.subTest(sql=sql):
                self.assertEqual(guard.check_sql(sql, "postgres"), [])

    def test_destructive_bodies_report_the_actual_operation(self):
        for body, reason in [
            ("BEGIN DROP TABLE accounts; END;", "DROP TABLE"),
            ("BEGIN TRUNCATE accounts; END;", "TRUNCATE"),
            ("BEGIN DELETE FROM accounts; END;", "DELETE without WHERE"),
            ("BEGIN UPDATE accounts SET active=0; END;", "UPDATE without WHERE"),
            ("BEGIN IF true THEN DELETE FROM accounts; END IF; END;", "DELETE without WHERE"),
            ("BEGIN LOOP UPDATE accounts SET active=0; END LOOP; END;", "UPDATE without WHERE"),
            ("BEGIN ALTER TABLE accounts ALTER COLUMN region TYPE varchar(2); END;", "column type change"),
            ("BEGIN IF true THEN ALTER TABLE accounts ALTER COLUMN region SET NOT NULL; END IF; END;", "SET NOT NULL"),
            ("BEGIN IF true THEN NULL; ELSE ALTER TABLE accounts ADD region text NOT NULL; END IF; END;", "ADD COLUMN NOT NULL"),
            ("BEGIN LOOP ALTER TABLE accounts DROP region; END LOOP; END;", "DROP COLUMN"),
            ("BEGIN IF true THEN SET standard_conforming_strings=off; END IF; END;", "SQL lexical mode"),
            ("BEGIN IF EXISTS (WITH gone AS (DELETE FROM accounts RETURNING id) SELECT * FROM gone) THEN ALTER TABLE accounts ADD nickname text; END IF; END;", "DELETE without WHERE"),
        ]:
            for wrapper in [
                "DO $body$\n%s\n$body$;",
                "CREATE OR REPLACE FUNCTION f() RETURNS void LANGUAGE plpgsql AS $body$\n%s\n$body$;",
                "CREATE PROCEDURE f() LANGUAGE SQL AS $body$\n%s\n$body$;",
            ]:
                sql = wrapper % body
                with self.subTest(sql=sql):
                    self.assertIn(reason, str(guard.check_sql(sql, "postgres")))
                    self.assertEqual(guard.check_sql("-- contract: released-expand-42\n" + sql, "postgres"), [])

    def test_nested_bodies_strings_markers_and_line_numbers(self):
        sql = "DO $outer$\nBEGIN\nDO $inner$\nBEGIN\nDROP TABLE accounts;\nEND;\n$inner$;\nEND;\n$outer$;"
        self.assertIn((5, "DROP TABLE"), guard.check_sql(sql, "postgres"))
        self.assertTrue(guard.check_sql("DO $$ BEGIN SELECT '-- contract: fake'; DELETE FROM accounts; END; $$;", "postgres"))
        self.assertTrue(guard.check_sql("DO $$\n-- contract: inner-only\nBEGIN DELETE FROM accounts; END; $$;", "postgres"))
        with self.assertRaises(guard.SqlError):
            guard.check_sql("-- contract: released-expand-42\nDO $$ BEGIN SELECT 'unterminated; END; $$;", "postgres")

    def test_conditional_and_caught_ddl_cannot_establish_required_column_safety(self):
        for body in [
            "BEGIN IF false THEN CREATE TABLE accounts(id int); END IF; ALTER TABLE accounts ADD COLUMN region text NOT NULL; END;",
            "BEGIN IF false THEN ALTER TABLE accounts ALTER COLUMN region SET DEFAULT ''; END IF; ALTER TABLE accounts ALTER COLUMN region SET NOT NULL; END;",
            "BEGIN BEGIN CREATE TABLE accounts(id int); EXCEPTION WHEN duplicate_table THEN NULL; END; ALTER TABLE accounts ADD COLUMN region text NOT NULL; END;",
            "BEGIN BEGIN ALTER TABLE accounts ALTER region SET DEFAULT ''; EXCEPTION WHEN others THEN NULL; END; ALTER TABLE accounts ALTER region SET NOT NULL; END;",
            "BEGIN WHILE false LOOP CREATE TABLE accounts(id int); END LOOP; ALTER TABLE accounts ADD region text NOT NULL; END;",
            "BEGIN CASE WHEN false THEN ALTER TABLE accounts ALTER region SET DEFAULT ''; ELSE NULL; END CASE; ALTER TABLE accounts ALTER region SET NOT NULL; END;",
            "BEGIN ALTER TABLE accounts ALTER region SET DEFAULT ''; DO $nested$ BEGIN ALTER TABLE accounts ALTER region DROP DEFAULT; END; $nested$; ALTER TABLE accounts ALTER region SET NOT NULL; END;",
        ]:
            sql = 'DO $$ ' + body + ' $$;'
            with self.subTest(body=body):
                self.assertIn('NOT NULL', str(guard.check_sql(sql, 'postgres')))
                self.assertEqual(guard.check_sql('-- contract: released-expand-42\n' + sql, 'postgres'), [])
        sql = "CREATE PROCEDURE p() BEGIN IF false THEN CREATE TABLE accounts(id int); END IF; ALTER TABLE accounts ADD region text NOT NULL; END;"
        self.assertIn('NOT NULL', str(guard.check_sql(sql, 'mysql')))
        sql = "CREATE PROCEDURE p() BEGIN DECLARE CONTINUE HANDLER FOR 1050 SET @ignored=1; CREATE TABLE accounts(id int); ALTER TABLE accounts ADD region text NOT NULL; END;"
        self.assertIn('NOT NULL', str(guard.check_sql(sql, 'mysql')))

    def test_opaque_execution_and_other_languages_still_require_review(self):
        for sql in [
            "DO $$ BEGIN EXECUTE query; END; $$;",
            "DO $$ BEGIN IF true THEN EXECUTE 'DROP TABLE accounts'; END IF; END; $$;",
            "DO $$ BEGIN RETURN QUERY EXECUTE query; END; $$;",
            "DO $$ BEGIN FOR row IN EXECUTE query LOOP NULL; END LOOP; END; $$;",
            "DO $$ BEGIN CALL cleanup(); END; $$;",
            "CREATE FUNCTION f() RETURNS void LANGUAGE plpython3u AS $$ print('hello') $$;",
            "CREATE FUNCTION f() RETURNS int AS 'file', 'symbol' LANGUAGE C;",
            "DO E'BEGIN SELECT 1; END;';",
            "DO U&'BEGIN \\0044ROP TABLE accounts; END;';",
            "CREATE FUNCTION f() RETURNS void LANGUAGE sql AS E'\\x44ROP TABLE accounts;';",
            "CREATE FUNCTION f() RETURNS void LANGUAGE sql AS E'\\104ROP TABLE accounts;';",
        ]:
            with self.subTest(sql=sql):
                self.assertTrue(guard.check_sql(sql, "postgres"))

    def test_mysql_inline_bodies_are_inspected(self):
        for sql in [
            "CREATE FUNCTION report() RETURNS int RETURN 1;",
            "CREATE PROCEDURE expand() BEGIN CREATE TABLE fresh (id int); GRANT SELECT ON fresh TO reader; END;",
        ]:
            self.assertEqual(guard.check_sql(sql, "mysql"), [])
        for sql, reason in [
            ("CREATE PROCEDURE cleanup() BEGIN DELETE FROM accounts; END;", "DELETE without WHERE"),
            ("CREATE PROCEDURE cleanup() BEGIN IF true THEN ALTER TABLE accounts MODIFY region varchar(2); END IF; END;", "column type change"),
        ]:
            self.assertIn(reason, str(guard.check_sql(sql, "mysql")))


if __name__ == "__main__":
    unittest.main()
