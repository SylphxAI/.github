"""PostgreSQL triggers reference callbacks; actual function bodies stay inspected."""
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
    def test_existing_new_and_conditional_tables_and_unqualified_callbacks_pass(self):
        for table in ["", TABLE, TABLE.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS")]:
            for sql in [FUNCTION + table + TRIGGER, (FUNCTION + table + TRIGGER).replace('app.', ''),
                        (FUNCTION + table + TRIGGER).replace('app.', '"app".').replace('assign_id', '"assign_id"')]:
                with self.subTest(sql=sql):
                    self.assertEqual(guard.check_sql(sql, "postgres"), [])

    def test_callback_references_do_not_claim_to_resolve_catalog_implementations(self):
        for sql in [TRIGGER, TRIGGER + FUNCTION,
                    TRIGGER.replace('app.assign_id', 'other.existing_callback'),
                    TRIGGER.replace('app.assign_id', '"app.assign_id"')]:
            self.assertEqual(guard.check_sql(sql, "postgres"), [])

    def test_before_after_events_and_supported_row_statement_forms(self):
        for timing in ["BEFORE", "AFTER"]:
            for events in ["INSERT", "UPDATE", "DELETE", "UPDATE OF id, nickname", "INSERT OR UPDATE OR DELETE"]:
                for level in ["", "FOR ROW", "FOR EACH ROW", "FOR STATEMENT", "FOR EACH STATEMENT"]:
                    sql = f"CREATE TRIGGER audit {timing} {events} ON app.records {level} EXECUTE FUNCTION app.audit();"
                    with self.subTest(sql=sql):
                        self.assertEqual(guard.check_sql(sql, "postgres"), [])
        for events in ["INSERT", "UPDATE", "DELETE", "INSERT OR UPDATE OR DELETE"]:
            self.assertEqual(guard.check_sql(f"CREATE TRIGGER audit INSTEAD OF {events} ON records FOR EACH ROW EXECUTE FUNCTION audit();", "postgres"), [])

    def test_truncate_is_an_event_not_a_truncate_statement(self):
        for timing in ["BEFORE", "AFTER"]:
            sql = f"CREATE TRIGGER audit {timing} TRUNCATE ON records EXECUTE FUNCTION audit();"
            self.assertEqual(guard.check_sql(sql, "postgres"), [])
            self.assertIn((1, "TRUNCATE"), guard.check_sql(sql + "TRUNCATE accounts;", "postgres"))
            self.assertIn("TRUNCATE", str(guard.check_sql(FUNCTION.replace("RETURN NEW;", "TRUNCATE accounts; RETURN NEW;") + sql, "postgres")))

    def test_constraint_trigger_and_optional_clauses(self):
        for options in ["", "FROM public.parent", "DEFERRABLE", "NOT DEFERRABLE", "INITIALLY IMMEDIATE",
                        "DEFERRABLE INITIALLY DEFERRED", "FROM parent NOT DEFERRABLE INITIALLY IMMEDIATE"]:
            sql = f"CREATE CONSTRAINT TRIGGER audit AFTER INSERT OR UPDATE ON records {options} FOR EACH ROW WHEN (NEW.id > 0) EXECUTE FUNCTION audit();"
            with self.subTest(options=options):
                self.assertEqual(guard.check_sql(sql, "postgres"), [])

    def test_replace_when_transition_relations_and_legacy_procedure_keyword(self):
        for sql in [
            'CREATE OR REPLACE TRIGGER "audit" BEFORE UPDATE OF balance ON accounts FOR EACH ROW WHEN (OLD.balance IS DISTINCT FROM NEW.balance) EXECUTE PROCEDURE public.audit();',
            "CREATE TRIGGER audit AFTER UPDATE ON records REFERENCING OLD TABLE AS old_rows NEW TABLE new_rows FOR EACH STATEMENT EXECUTE FUNCTION audit();",
            "CREATE TRIGGER audit AFTER INSERT ON records REFERENCING NEW TABLE AS inserted EXECUTE FUNCTION audit();",
            "CREATE TRIGGER audit AFTER DELETE ON records REFERENCING OLD TABLE deleted FOR ROW EXECUTE FUNCTION audit();",
            "CREATE TRIGGER audit AFTER UPDATE ON records FOR ROW WHEN (coalesce(NEW.balance, 0) > (OLD.balance + 1)) EXECUTE FUNCTION audit();",
            "CREATE TRIGGER audit BEFORE INSERT ON records EXECUTE FUNCTION audit('DROP TABLE accounts', $$DELETE FROM accounts$$, id, 42, 1.25, .5, 1e+3);",
            TRIGGER.replace("app.assign_id()", "app.assign_id('arg', 'it''s text', \"arg\")"),
        ]:
            with self.subTest(sql=sql):
                self.assertEqual(guard.check_sql(sql, "postgres"), [])

    def test_function_bodies_still_report_destructive_dynamic_or_opaque_operations(self):
        for body, reason in [("DELETE FROM accounts;", "DELETE without WHERE"),
                             ("UPDATE accounts SET active=0;", "UPDATE without WHERE"),
                             ("DROP TABLE accounts;", "DROP TABLE"),
                             ("EXECUTE query;", "dynamic SQL"),
                             ("EXECUTE 'DROP TABLE accounts';", "dynamic SQL"),
                             ("DO $inner$ BEGIN DROP TABLE accounts; END; $inner$;", "DROP TABLE")]:
            function = FUNCTION.replace("RETURN NEW;", body + " RETURN NEW;")
            for sql in [function + TRIGGER, TRIGGER + function]:
                with self.subTest(sql=sql):
                    self.assertIn(reason, str(guard.check_sql(sql, "postgres")))
                    self.assertEqual(guard.check_sql("-- contract: released-expand-42\n" + sql, "postgres"), [])
        for function in [FUNCTION.replace("plpgsql", "plpython3u"),
                         "CREATE FUNCTION app.assign_id() RETURNS trigger AS 'file', 'symbol' LANGUAGE C;"]:
            self.assertIn("opaque procedural body", str(guard.check_sql(function + TRIGGER, "postgres")))

    def test_following_sibling_mutations_and_trigger_conditions_remain_inspected(self):
        for operation, reason in [("DELETE FROM accounts;", "DELETE without WHERE"),
                                  ("UPDATE accounts SET active=0;", "UPDATE without WHERE"),
                                  ("TRUNCATE accounts;", "TRUNCATE"),
                                  ("DROP TABLE accounts;", "DROP TABLE"),
                                  ("EXECUTE query;", "dynamic SQL")]:
            self.assertIn(reason, str(guard.check_sql(TRIGGER + operation, "postgres")))
        self.assertTrue(guard.check_sql(TRIGGER.replace("FOR EACH ROW", "FOR EACH ROW WHEN (DELETE FROM accounts)"), "postgres"))

    def test_malformed_or_inline_callback_actions_fail_closed(self):
        for callback in ["", "audit", "audit.", ".audit()", "app..audit()", "app.audit.()", "a.b.audit()",
                         "'audit'()", "42()", "audit() trailing", "audit() EXECUTE FUNCTION other()",
                         "audit(1 + 2)", "audit(other())", "audit(,)", "audit('a',)", "audit('a',,'b')",
                         "audit() BEGIN RETURN NEW; END", "audit() AS $$BEGIN RETURN NEW; END$$"]:
            sql = TRIGGER.replace("app.assign_id()", callback)
            with self.subTest(callback=callback):
                self.assertIn("opaque procedural body", str(guard.check_sql(sql, "postgres")))
        for action in ["DO $$BEGIN RETURN NEW; END$$", "EXECUTE query", "EXECUTE FUNCTION 'audit'()"]:
            self.assertTrue(guard.check_sql(TRIGGER.replace("EXECUTE FUNCTION app.assign_id()", action), "postgres"))

    def test_invalid_trigger_clauses_and_mysql_stay_opaque(self):
        for trigger in [
            TRIGGER.replace("BEFORE INSERT", "INSERT"),
            TRIGGER.replace("BEFORE INSERT", "BEFORE SELECT"),
            TRIGGER.replace("BEFORE INSERT", "BEFORE INSERT OR INSERT"),
            TRIGGER.replace("assign_id BEFORE", "app.assign_id BEFORE"),
            TRIGGER.replace("BEFORE INSERT", "INSTEAD OF TRUNCATE"),
            TRIGGER.replace("BEFORE INSERT", "INSTEAD OF UPDATE OF id"),
            TRIGGER.replace("BEFORE INSERT", "INSTEAD OF INSERT").replace("FOR EACH ROW", ""),
            TRIGGER.replace("BEFORE INSERT", "BEFORE TRUNCATE"),
            TRIGGER.replace("FOR EACH ROW", "FOR EACH ROW WHEN ()"),
            TRIGGER.replace("FOR EACH ROW", "FOR EACH ROW WHEN (SELECT 1)"),
            TRIGGER.replace("FOR EACH ROW", "FOR EACH ROW DEFERRABLE"),
            TRIGGER.replace("FOR EACH ROW", "REFERENCING NEW TABLE rows"),
            TRIGGER.replace("BEFORE INSERT", "AFTER INSERT").replace("FOR EACH ROW", "REFERENCING OLD TABLE rows"),
            TRIGGER.replace("BEFORE INSERT", "AFTER INSERT").replace("FOR EACH ROW", "REFERENCING NEW TABLE a NEW TABLE b"),
            TRIGGER.replace("CREATE TRIGGER", "CREATE CONSTRAINT TRIGGER"),
            TRIGGER.replace("CREATE TRIGGER", "CREATE OR REPLACE CONSTRAINT TRIGGER").replace("BEFORE", "AFTER"),
            TRIGGER.replace("CREATE TRIGGER", "CREATE CONSTRAINT TRIGGER").replace("BEFORE", "AFTER").replace("FOR EACH ROW", "FOR ROW"),
        ]:
            with self.subTest(trigger=trigger):
                self.assertIn("opaque procedural body", str(guard.check_sql(trigger, "postgres")))
        self.assertTrue(guard.check_sql(FUNCTION + TABLE + TRIGGER, "mysql"))
        self.assertTrue(guard.check_sql("CREATE EVENT audit ON SCHEDULE EVERY 1 DAY DO CALL audit();", "postgres"))


if __name__ == "__main__":
    unittest.main()
