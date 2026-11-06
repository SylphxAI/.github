"""Released-expand comments authorize one top-level statement, never a file."""
from pathlib import Path
import importlib.util
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("migration_guard", ROOT / ".github/actions/migration-guard/migration_guard.py")
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)
MARKER = "-- contract: released-expand-42\n"


class ContractScopeTests(unittest.TestCase):
    def test_each_contraction_needs_its_own_preceding_marker(self):
        for dialect in ("postgres", "mysql"):
            for first in ("DROP TABLE retired;", "ALTER TABLE accounts DROP old_region;", "SELECT 1;"):
                sql = MARKER + first + "\nDROP TABLE accounts;"
                with self.subTest(dialect=dialect, first=first):
                    self.assertEqual(guard.check_sql(sql, dialect), [(3, "DROP TABLE")])
                    self.assertEqual(guard.check_sql(MARKER + first + "\n" + MARKER + "DROP TABLE accounts;", dialect), [])
            self.assertEqual(guard.check_sql("DROP TABLE accounts;\n" + MARKER, dialect), [(1, "DROP TABLE")])
            # An empty delimiter consumes the annotation rather than searching
            # forward for a later destructive statement.
            self.assertEqual(guard.check_sql(MARKER + "; DROP TABLE accounts;", dialect), [(2, "DROP TABLE")])
            self.assertTrue(guard.check_sql("ALTER TABLE accounts\n" + MARKER + "DROP old_region;", dialect))
            self.assertEqual(guard.check_sql(MARKER + "/* explanation */\nDROP TABLE accounts;", dialect), [])

    def test_inline_routine_envelope_does_not_cover_sibling_sql(self):
        for dialect, header in [
            ("postgres", "CREATE PROCEDURE p() LANGUAGE SQL BEGIN ATOMIC"),
            ("mysql", "CREATE PROCEDURE p() BEGIN"),
            ("mysql", "CREATE TRIGGER t BEFORE INSERT ON source FOR EACH ROW BEGIN"),
            ("mysql", "CREATE EVENT e ON SCHEDULE EVERY 1 DAY DO BEGIN"),
        ]:
            for body in ["SELECT 1; DROP TABLE retired;", "SELECT CASE WHEN true THEN 1 ELSE 0 END;",
                         "SELECT begin FROM source;", "SELECT begin; SELECT end;", "SELECT begin atomic FROM source;",
                         "CREATE FUNCTION nested() RETURNS int LANGUAGE SQL AS $$SELECT 1;$$;"]:
                sql = header + " " + body + " END;"
                with self.subTest(dialect=dialect, body=body, header=header):
                    self.assertIn("opaque procedural body", str(guard.check_sql(sql, dialect)))
                    self.assertEqual(guard.check_sql(MARKER + sql, dialect), [])
                    self.assertEqual(guard.check_sql(MARKER + sql + "\nDROP TABLE accounts;", dialect), [(3, "DROP TABLE")])
                    self.assertEqual(guard.check_sql(MARKER + sql + "\n" + MARKER + "DROP TABLE accounts;", dialect), [])

    def test_begin_identifiers_in_headers_and_single_statement_bodies_are_not_blocks(self):
        for sql in [
            "CREATE PROCEDURE begin() BEGIN SELECT 1; END;",
            "CREATE FUNCTION begin() RETURNS int BEGIN RETURN 1; END;",
            "CREATE PROCEDURE p() SELECT begin FROM source;",
            "CREATE FUNCTION f() RETURNS int RETURN begin;",
            "CREATE TRIGGER t BEFORE INSERT ON begin FOR EACH ROW SET NEW.id=1;",
            "CREATE TRIGGER t BEFORE INSERT ON begin FOR EACH ROW BEGIN SET NEW.id=1; END;",
            "CREATE EVENT begin ON SCHEDULE EVERY 1 DAY DO SELECT begin FROM source;",
            "CREATE EVENT begin ON SCHEDULE EVERY 1 DAY DO BEGIN SELECT 1; END;",
        ]:
            with self.subTest(sql=sql):
                self.assertEqual(guard.check_sql(MARKER + sql, "mysql"), [])
                self.assertEqual(guard.check_sql(MARKER + sql + "\nDROP TABLE accounts;", "mysql"), [(3, "DROP TABLE")])

    def test_nested_inline_blocks_keep_the_outer_boundary(self):
        for dialect, sql in [
            ("postgres", "CREATE PROCEDURE p() LANGUAGE SQL BEGIN ATOMIC CREATE PROCEDURE nested() LANGUAGE SQL BEGIN ATOMIC SELECT 1; END; SELECT 2; END;"),
            ("mysql", "CREATE PROCEDURE p() BEGIN BEGIN SELECT 1; END; SELECT 2; END;"),
            ("mysql", "CREATE PROCEDURE p() BEGIN IF true THEN BEGIN SELECT begin FROM t; END; END IF; END;"),
            ("mysql", "CREATE PROCEDURE p() BEGIN IF true THEN SELECT CASE WHEN true THEN begin ELSE 1 END; END IF; END;"),
            ("mysql", "CREATE PROCEDURE p() BEGIN DECLARE CONTINUE HANDLER FOR 1050 SET @x=begin; SELECT 1; END;"),
            ("mysql", "CREATE PROCEDURE p() BEGIN CASE WHEN true THEN BEGIN SELECT 1; END; ELSE SELECT 2; END CASE; END;"),
            ("mysql", "CREATE PROCEDURE p() BEGIN DECLARE CONTINUE HANDLER FOR 1050 BEGIN SELECT 1; END; SELECT 2; END;"),
            ("mysql", "CREATE PROCEDURE p() outer_label: BEGIN inner_label: BEGIN SELECT 1; END inner_label; END outer_label;"),
        ]:
            with self.subTest(sql=sql):
                self.assertEqual(guard.check_sql(MARKER + sql, dialect), [])
                self.assertEqual(guard.check_sql(MARKER + sql + "\nDROP TABLE accounts;", dialect), [(3, "DROP TABLE")])

    def test_nonreserved_begin_cannot_swallow_a_sibling_drop(self):
        for dialect, header in [("postgres", "CREATE PROCEDURE p() LANGUAGE SQL BEGIN ATOMIC"),
                                ("mysql", "CREATE PROCEDURE p() BEGIN")]:
            for expression in ("begin", "begin atomic", "CASE WHEN true THEN begin ELSE 1 END"):
                sql = MARKER + header + " SELECT " + expression + " FROM source; END; DROP TABLE accounts; END;"
                with self.subTest(dialect=dialect, expression=expression):
                    self.assertIn("DROP TABLE", str(guard.check_sql(sql, dialect)))

    def test_ambiguous_or_unbalanced_envelopes_are_never_waived(self):
        for dialect, sql in [
            ("postgres", "CREATE PROCEDURE p() LANGUAGE SQL BEGIN ATOMIC SELECT 1;"),
            ("postgres", "CREATE PROCEDURE p() LANGUAGE SQL BEGIN ATOMIC SELECT 1; END IF; END;"),
            ("postgres", "CREATE PROCEDURE p() LANGUAGE SQL BEGIN ATOMIC SELECT 1; END DROP TABLE accounts;"),
            ("mysql", "CREATE PROCEDURE p() BEGIN BEGIN SELECT 1; END;"),
            ("mysql", "CREATE PROCEDURE p() BEGIN SELECT CASE WHEN true THEN 1; END;"),
            ("mysql", "CREATE PROCEDURE p() BEGIN SELECT 1; END label extra;"),
        ]:
            with self.subTest(sql=sql), self.assertRaises(guard.SqlError):
                guard.check_sql(MARKER + sql, dialect)

    def test_mode_changes_cannot_invalidate_following_sql_inspection(self):
        for dialect, setting in [("postgres", "SET standard_conforming_strings=off;"),
                                 ("postgres", "SET standard_conforming_strings=on;"),
                                 ("mysql", "SET sql_mode='NO_BACKSLASH_ESCAPES';"),
                                 ("mysql", "SET @@session.`sql_mode`='ANSI_QUOTES';")]:
            with self.subTest(dialect=dialect, setting=setting):
                self.assertEqual(guard.check_sql(MARKER + setting, dialect), [])
                for following in ("SELECT 1;", MARKER + "DROP TABLE accounts;"):
                    with self.assertRaisesRegex(guard.SqlError, "lexical mode change must be the final"):
                        guard.check_sql(MARKER + setting + "\n" + following, dialect)
        for sql in ["DO $$ BEGIN SET standard_conforming_strings=off; END; $$; SELECT 1;",
                    "DO $$ BEGIN DO $inner$ BEGIN SET standard_conforming_strings=off; END; $inner$; END; $$; SELECT 1;"]:
            with self.subTest(sql=sql), self.assertRaisesRegex(guard.SqlError, "lexical mode change must be the final"):
                guard.check_sql(MARKER + sql, "postgres")
        for sql in [
            "CREATE PROCEDURE p() LANGUAGE SQL AS $$ SET standard_conforming_strings=off; $$; SELECT 1;",
            "DO $$ BEGIN CREATE PROCEDURE p() LANGUAGE SQL BEGIN ATOMIC SELECT 1; SET standard_conforming_strings=off; END; END; $$; SELECT 1;",
        ]:
            with self.subTest(unexecuted_definition=sql):
                self.assertEqual(guard.check_sql(MARKER + sql, "postgres"), [])

    def test_marked_statements_still_update_enclosing_safety_facts(self):
        for dialect in ("postgres", "mysql"):
            with self.subTest(dialect=dialect):
                self.assertEqual(guard.check_sql(MARKER + "ALTER TABLE accounts ALTER region SET DEFAULT ''; ALTER TABLE accounts ALTER region SET NOT NULL;", dialect), [])
                self.assertIn("SET NOT NULL", str(guard.check_sql("ALTER TABLE accounts ALTER region SET DEFAULT '';\n" + MARKER + "ALTER TABLE accounts ALTER region DROP DEFAULT; ALTER TABLE accounts ALTER region SET NOT NULL;", dialect)))
                cascade = "CREATE TABLE fresh(id int REFERENCES accounts(id) ON DELETE CASCADE);"
                self.assertIn("ON DELETE CASCADE", str(guard.check_sql(MARKER + "CREATE TABLE fresh(id int);" + cascade, dialect)))
                self.assertIn("ON DELETE CASCADE", str(guard.check_sql(MARKER + "DROP TABLE fresh;" + cascade, dialect)))
                routine = "CREATE PROCEDURE p() " + ("LANGUAGE SQL BEGIN ATOMIC" if dialect == "postgres" else "BEGIN")
                sql = "ALTER TABLE accounts ALTER region SET DEFAULT '';\n" + MARKER + routine + " ALTER TABLE accounts ALTER region DROP DEFAULT; END; ALTER TABLE accounts ALTER region SET NOT NULL;"
                self.assertEqual(guard.check_sql(sql, dialect), [])

    def test_nested_inline_definition_is_opaque_and_cannot_lend_facts(self):
        sql = "DO $outer$ BEGIN CREATE PROCEDURE p() LANGUAGE SQL BEGIN ATOMIC CREATE FUNCTION f() RETURNS int LANGUAGE SQL AS $inner$SELECT 1;$inner$; ALTER TABLE accounts ALTER region SET DEFAULT ''; END; ALTER TABLE accounts ALTER region SET NOT NULL; END; $outer$;"
        findings = str(guard.check_sql(sql, "postgres"))
        self.assertIn("opaque procedural body", findings)
        self.assertIn("SET NOT NULL", findings)
        self.assertEqual(guard.check_sql(MARKER + sql, "postgres"), [])

    def test_snapshot_reference_identity_does_not_change_with_position(self):
        before = MARKER + "CREATE TABLE accounts(id int, old_region text);"
        after = "CREATE TABLE accounts(id int);\n" + MARKER
        self.assertTrue(guard.check_schema(before, after, "postgres"))
        self.assertEqual(guard.check_schema(before, after.replace("expand-42", "expand-43"), "postgres"), [])


if __name__ == "__main__":
    unittest.main()
