"""Privilege lists are not executable data changes."""
import unittest

from test_migration_guard import guard


class PrivilegeListTests(unittest.TestCase):
    def test_grant_and_revoke_privilege_names_are_not_mutations(self):
        for statement in (
            "REVOKE UPDATE, DELETE, TRUNCATE ON records FROM CURRENT_USER;",
            "GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON TABLE app.records TO reader;",
            "REVOKE GRANT OPTION FOR UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA app FROM reader;",
            "GRANT DELETE ON records TO reader;",
            "GRANT ALL PRIVILEGES ON records TO reader;",
        ):
            with self.subTest(statement=statement):
                self.assertEqual(guard.check_sql(statement, "postgres"), [])
                self.assertEqual(guard.check_sql("DO $$ BEGIN IF true THEN " + statement + " END IF; END; $$;", "postgres"), [])

    def test_following_destructive_statements_remain_refused(self):
        for statement, reason in (("TRUNCATE records;", "TRUNCATE"),
                                  ("DELETE FROM records;", "DELETE without WHERE"),
                                  ("UPDATE records SET active=false;", "UPDATE without WHERE")):
            for prefix in ("", "GRANT SELECT, DELETE, TRUNCATE ON records TO reader;",
                           "REVOKE UPDATE, DELETE, TRUNCATE ON records FROM CURRENT_USER;"):
                with self.subTest(statement=statement, prefix=prefix):
                    self.assertIn(reason, str(guard.check_sql(prefix + statement, "postgres")))

    def test_condition_before_privilege_list_is_still_inspected(self):
        sql = ("DO $$ BEGIN IF EXISTS (WITH removed AS (DELETE FROM records RETURNING id) "
               "SELECT * FROM removed) THEN REVOKE UPDATE, DELETE, TRUNCATE ON records "
               "FROM CURRENT_USER; END IF; END; $$;")
        self.assertIn("DELETE without WHERE", str(guard.check_sql(sql, "postgres")))


if __name__ == "__main__":
    unittest.main()
