"""Constant ACL format templates can be inspected without executing SQL."""
from pathlib import Path
import importlib.util
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("migration_guard", ROOT / ".github/actions/migration-guard/migration_guard.py")
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


def body(command):
    return "DO $body$ BEGIN FOR r IN SELECT rolname FROM pg_roles LOOP " + command + "; END LOOP; END; $body$;"


class AclTests(unittest.TestCase):
    def test_constant_schema_and_exact_function_privileges_pass(self):
        for template in [
            "GRANT USAGE ON SCHEMA reports TO %I",
            "REVOKE USAGE ON SCHEMA reports FROM %I",
            "GRANT EXECUTE ON FUNCTION reports.daily(timestamptz,timestamptz) TO %I",
            "REVOKE EXECUTE ON FUNCTION reports.grant_readers() FROM %I",
            'GRANT EXECUTE ON FUNCTION "Reports"."Daily"(pg_catalog.text[], numeric(8,2)) TO %I;',
        ]:
            sql = body("EXECUTE format('" + template + "', r.rolname)")
            with self.subTest(template=template):
                self.assertEqual(guard.check_sql(sql, "postgres"), [])
                self.assertTrue(guard.check_sql(sql, "mysql"))
        self.assertEqual(guard.check_sql(body("EXECUTE format($acl$GRANT USAGE ON SCHEMA reports TO %I$acl$, role_name)"), "postgres"), [])
        self.assertEqual(guard.check_sql(body("EXECUTE pg_catalog.format('GRANT USAGE ON SCHEMA reports TO %I', r.rolname)"), "postgres"), [])

    def test_unknown_templates_and_sql_interpolation_remain_opaque(self):
        for command in [
            "EXECUTE query_text",
            "RETURN QUERY EXECUTE query_text USING start_time, end_time",
            "EXECUTE format(template, r.rolname)",
            "EXECUTE format('GRANT USAGE ON SCHEMA reports TO %s', r.rolname)",
            "EXECUTE format('GRANT USAGE ON SCHEMA reports TO %L', r.rolname)",
            "EXECUTE format('GRANT USAGE ON SCHEMA reports TO %1$I', r.rolname)",
            "EXECUTE format('GRANT USAGE ON SCHEMA reports TO ' || '%I', r.rolname)",
            "EXECUTE format('GRANT USAGE ON SCHEMA reports TO %I', calculate_role())",
            "EXECUTE format('GRANT USAGE ON SCHEMA reports TO %I', r.rolname, other_role)",
        ]:
            with self.subTest(command=command):
                self.assertIn("dynamic SQL", str(guard.check_sql(body(command), "postgres")))

    def test_extra_statements_and_privilege_scope_changes_are_refused(self):
        for template in [
            "GRANT USAGE ON SCHEMA reports TO %I; DROP TABLE accounts",
            "DROP TABLE accounts; GRANT USAGE ON SCHEMA reports TO %I",
            "GRANT USAGE ON SCHEMA reports TO %I; GRANT ALL ON DATABASE production TO attacker",
            "GRANT USAGE ON SCHEMA reports TO %I WITH GRANT OPTION",
            "GRANT ALL ON SCHEMA reports TO %I",
            "GRANT SELECT ON TABLE accounts TO %I",
            "GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA reports TO %I",
            "GRANT EXECUTE ON FUNCTION reports.first(), reports.second() TO %I",
            "GRANT EXECUTE ON FUNCTION daily() TO %I",
            "GRANT EXECUTE ON FUNCTION reports.daily(); SELECT steal(); -- () TO %I",
            "GRANT USAGE ON SCHEMA reports TO reader /* %I */",
        ]:
            with self.subTest(template=template):
                sql = body("EXECUTE format('" + template + "', r.rolname)")
                self.assertIn("dynamic SQL", str(guard.check_sql(sql, "postgres")))

    def test_only_identifier_quoting_can_carry_an_injection_shaped_role(self):
        sql = "DECLARE r record; BEGIN r.rolname := 'reader; DROP TABLE accounts; --'; "
        sql += "EXECUTE format('GRANT USAGE ON SCHEMA reports TO %I', r.rolname); END;"
        self.assertEqual(guard.check_sql("DO $$" + sql + "$$;", "postgres"), [])
        self.assertTrue(guard.check_sql("DO $$" + sql.replace("%I", "%s") + "$$;", "postgres"))

    def test_acl_does_not_hide_real_mutations_or_manufacture_a_marker(self):
        acl = "EXECUTE format('GRANT USAGE ON SCHEMA reports TO %I', r.rolname)"
        self.assertIn("DELETE without WHERE", str(guard.check_sql(body(acl + "; DELETE FROM accounts"), "postgres")))
        self.assertTrue(guard.check_sql(body(acl + "; EXECUTE query_text"), "postgres"))
        self.assertEqual(guard.check_sql("-- contract: released-expand-42\n" + body("EXECUTE query_text"), "postgres"), [])


if __name__ == "__main__":
    unittest.main()
