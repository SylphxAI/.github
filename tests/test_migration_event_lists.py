"""Trigger event names do not authorize unrelated data changes."""
import unittest

from test_migration_guard import guard


class TriggerEventListTests(unittest.TestCase):
    trigger = ("CREATE TRIGGER immutable_rows BEFORE UPDATE OR DELETE OR TRUNCATE "
               "ON records FOR EACH STATEMENT EXECUTE FUNCTION refuse_change();")

    def test_three_event_callback_reference_is_additive_ddl(self):
        self.assertEqual(guard.check_sql(self.trigger, "postgres"), [])
        body = "CREATE FUNCTION refuse_change() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'immutable'; END; $$;"
        self.assertEqual(guard.check_sql(body + self.trigger, "postgres"), [])

    def test_malformed_event_lists_and_real_mutations_still_refuse(self):
        for events in ("UPDATE OR DELETE OR TRUNCATE OR", "UPDATE OR DELETE OR TRUNCATE OR DELETE",
                       "UPDATE DELETE TRUNCATE"):
            with self.subTest(events=events):
                self.assertTrue(guard.check_sql(self.trigger.replace("UPDATE OR DELETE OR TRUNCATE", events), "postgres"))
        for statement, reason in (("UPDATE records SET active=false;", "UPDATE without WHERE"),
                                  ("DELETE FROM records;", "DELETE without WHERE"),
                                  ("TRUNCATE records;", "TRUNCATE")):
            for sql in (statement, self.trigger + statement):
                with self.subTest(sql=sql):
                    self.assertIn(reason, str(guard.check_sql(sql, "postgres")))

    def test_callback_bodies_are_not_event_lists(self):
        body = "CREATE FUNCTION refuse_change() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN DELETE FROM records; END; $$;"
        self.assertIn("DELETE without WHERE", str(guard.check_sql(body + self.trigger, "postgres")))
        self.assertIn("opaque procedural body", str(guard.check_sql(body.replace("plpgsql", "plpython3u") + self.trigger, "postgres")))


if __name__ == "__main__":
    unittest.main()
