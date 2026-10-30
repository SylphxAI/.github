"""New-table INSERT triggers can use same-file inspected trigger functions."""
from pathlib import Path
import importlib.util
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("migration_guard", ROOT / ".github/actions/migration-guard/migration_guard.py")
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)

FUNCTION = """CREATE FUNCTION app.assign_id() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN IF NEW.id IS NULL THEN NEW.id := nextval('app.ids'); END IF; RETURN NEW; END;
$$;"""
TABLE = "CREATE TABLE app.records(id bigint NOT NULL);"
TRIGGER = "CREATE TRIGGER assign_id BEFORE INSERT ON app.records FOR EACH ROW EXECUTE FUNCTION app.assign_id();"


class TriggerTests(unittest.TestCase):
    def test_new_table_inspected_insert_trigger_passes(self):
        self.assertEqual(guard.check_sql(FUNCTION + TABLE + TRIGGER, "postgres"), [])
        quoted = (FUNCTION + TABLE + TRIGGER).replace('app.', '"app".').replace('assign_id', '"assign_id"')
        self.assertEqual(guard.check_sql(quoted, "postgres"), [])

    def test_existing_or_conditional_table_requires_reference(self):
        for table in ["", "CREATE TABLE IF NOT EXISTS app.records(id bigint NOT NULL);"]:
            sql = FUNCTION + table + TRIGGER
            self.assertIn("opaque procedural body", str(guard.check_sql(sql, "postgres")))
            self.assertEqual(guard.check_sql("-- contract: released-expand-42\n" + sql, "postgres"), [])

    def test_missing_external_or_uninspected_function_requires_reference(self):
        for function in ["", FUNCTION.replace("app.assign_id", "other.assign_id"),
                         FUNCTION.replace("RETURNS trigger LANGUAGE plpgsql AS $$", "RETURNS trigger LANGUAGE plpython3u AS $$"),
                         FUNCTION.replace("assign_id()", "assign_id(value int)")]:
            self.assertTrue(guard.check_sql(function + TABLE + TRIGGER, "postgres"))

    def test_destructive_or_dynamic_function_is_refused(self):
        for body, reason in [("DELETE FROM accounts;", "DELETE without WHERE"),
                             ("DROP TABLE accounts;", "DROP TABLE"),
                             ("EXECUTE query;", "dynamic SQL")]:
            function = FUNCTION.replace("RETURN NEW;", body + " RETURN NEW;")
            sql = function + TABLE + TRIGGER
            self.assertIn(reason, str(guard.check_sql(sql, "postgres")))
            self.assertEqual(guard.check_sql("-- contract: released-expand-42\n" + sql, "postgres"), [])

    def test_search_path_and_quoted_dots_cannot_bind_an_uninspected_object(self):
        for sql in [
            (FUNCTION + TABLE + TRIGGER).replace('app.', ''),
            FUNCTION.replace('app.assign_id', '"app.assign_id"') + TABLE + TRIGGER,
            FUNCTION + TABLE.replace('app.records', '"app.records"') + TRIGGER,
            FUNCTION + TABLE + TRIGGER.replace('app.assign_id', 'assign_id'),
        ]:
            self.assertIn("opaque procedural body", str(guard.check_sql(sql, "postgres")))

    def test_schema_relocation_invalidates_table_and_function_identity(self):
        for relocation in [
            "ALTER TABLE app.records SET SCHEMA scratch; ALTER TABLE legacy.records SET SCHEMA app;",
            "ALTER FUNCTION app.assign_id() SET SCHEMA scratch; ALTER FUNCTION legacy.assign_id() SET SCHEMA app;",
            "DO $$BEGIN ALTER TABLE app.records SET SCHEMA scratch; ALTER TABLE legacy.records SET SCHEMA app; END;$$;",
            "DO $$BEGIN ALTER FUNCTION app.assign_id() SET SCHEMA scratch; ALTER FUNCTION legacy.assign_id() SET SCHEMA app; END;$$;",
        ]:
            sql = FUNCTION + TABLE + relocation + TRIGGER
            with self.subTest(relocation=relocation):
                self.assertIn("opaque procedural body", str(guard.check_sql(sql, "postgres")))
                self.assertEqual(guard.check_sql("-- contract: released-expand-42\n" + sql, "postgres"), [])

    def test_unsupported_trigger_forms_and_mysql_remain_opaque(self):
        for trigger in [TRIGGER.replace("INSERT ON", "UPDATE ON"),
                        TRIGGER.replace("app.assign_id()", "app.assign_id('arg')"),
                        TRIGGER.replace("FOR EACH ROW", "FOR EACH STATEMENT")]:
            self.assertTrue(guard.check_sql(FUNCTION + TABLE + trigger, "postgres"))
        self.assertTrue(guard.check_sql(FUNCTION + TABLE + TRIGGER, "mysql"))


if __name__ == "__main__":
    unittest.main()
