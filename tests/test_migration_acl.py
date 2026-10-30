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


def routine(command, path="pg_catalog, pg_temp"):
    setting = " SET search_path = " + path if path else ""
    return "CREATE FUNCTION reports.grant_readers() RETURNS void LANGUAGE plpgsql" + setting + " AS $body$ BEGIN " + command + "; END; $body$;"


class AclTests(unittest.TestCase):
    def test_bare_format_requires_a_fixed_routine_catalog_path(self):
        command = "EXECUTE format('GRANT USAGE ON SCHEMA reports TO %I', r.rolname)"
        for path in ["pg_catalog", "pg_catalog, pg_temp", '"pg_catalog", "pg_temp"']:
            with self.subTest(path=path):
                self.assertEqual(guard.check_sql(routine(command, path), "postgres"), [])
        for path in [None, "public, pg_catalog", "pg_catalog, public", "pg_catalog, pg_temp, public", '"PG_CATALOG"', "'pg_catalog, public'"]:
            with self.subTest(path=path):
                self.assertIn("dynamic SQL", str(guard.check_sql(routine(command, path), "postgres")))
        self.assertIn("dynamic SQL", str(guard.check_sql(body(command), "postgres")))

    def test_path_changes_and_nested_execution_invalidate_bare_format(self):
        acl = "EXECUTE format('GRANT USAGE ON SCHEMA reports TO %I', r.rolname)"
        for mutation in [
            "SET search_path = public, pg_catalog",
            "PERFORM set_config('search_path', 'public, pg_catalog', true)",
            "RESET ALL", "RESET search_path",
            "DO $$ BEGIN SET search_path = public, pg_catalog; END; $$",
            "PERFORM public.configure_session()",
            'PERFORM "custom"."configure_session"()',
        ]:
            for commands in [mutation + "; " + acl, acl + "; " + mutation]:
                with self.subTest(commands=commands):
                    self.assertIn("dynamic SQL", str(guard.check_sql(routine(commands), "postgres")))
        # A secured enclosing routine does not establish a nested routine's path.
        nested = "CREATE FUNCTION reports.other() RETURNS void LANGUAGE plpgsql AS $$ BEGIN " + acl + "; END; $$"
        self.assertIn("dynamic SQL", str(guard.check_sql(routine(nested), "postgres")))

    def test_formatter_overloads_are_never_credited_as_builtins(self):
        acl = "EXECUTE format('GRANT USAGE ON SCHEMA reports TO %I', r.rolname)"
        for function in ["public.format", "attack.format", "pg_catalog.format", 'public."format"']:
            declaration = "CREATE FUNCTION " + function + "(text,name) RETURNS text LANGUAGE sql AS $$ SELECT 'uninspected command'::text $$;"
            with self.subTest(function=function):
                self.assertIn("formatter redefinition", str(guard.check_sql(declaration + body(acl), "postgres")))
                self.assertIn("formatter redefinition", str(guard.check_sql(declaration + routine(acl), "postgres")))

    def test_placeholder_cannot_impersonate_a_constant_grantee(self):
        for template in [
            'GRANT USAGE ON SCHEMA %I TO "__guard_acl_role__"',
            'REVOKE USAGE ON SCHEMA %I FROM "__guard_acl_role__"',
            'GRANT EXECUTE ON FUNCTION %I.daily() TO "__guard_acl_role__"',
            'GRANT EXECUTE ON FUNCTION reports.%I() TO "__guard_acl_role__"',
            'GRANT USAGE ON SCHEMA reports TO "%I"',
            "GRANT USAGE ON SCHEMA reports TO reader -- TO %I",
            "GRANT USAGE ON SCHEMA reports TO -- %I",
            "GRANT USAGE ON SCHEMA reports TO -- TO %I",
            "GRANT USAGE ON SCHEMA reports TO reader /* TO %I */",
            "GRANT USAGE ON SCHEMA reports TO %I, reader",
        ]:
            with self.subTest(template=template):
                sql = body("EXECUTE pg_catalog.format('" + template + "', r.rolname)")
                self.assertIn("dynamic SQL", str(guard.check_sql(sql, "postgres")))
        # A literal with the old sentinel spelling is an ordinary fixed target.
        sql = body('EXECUTE pg_catalog.format(\'GRANT USAGE ON SCHEMA "__guard_acl_role__" TO %I\', r.rolname)')
        self.assertEqual(guard.check_sql(sql, "postgres"), [])
        for template in ["GRANT USAGE ON SCHEMA reports TO /* role */ %I", "GRANT USAGE ON SCHEMA reports TO -- role\n%I"]:
            self.assertEqual(guard.check_sql(body("EXECUTE pg_catalog.format($acl$" + template + "$acl$, r.rolname)"), "postgres"), [])

    def test_constant_schema_and_exact_function_privileges_pass(self):
        for template in [
            "GRANT USAGE ON SCHEMA reports TO %I",
            "REVOKE USAGE ON SCHEMA reports FROM %I",
            "GRANT EXECUTE ON FUNCTION reports.daily(timestamptz,timestamptz) TO %I",
            "REVOKE EXECUTE ON FUNCTION reports.grant_readers() FROM %I",
            'GRANT EXECUTE ON FUNCTION "Reports"."Daily"(pg_catalog.text[], numeric(8,2)) TO %I;',
        ]:
            sql = body("EXECUTE pg_catalog.format('" + template + "', r.rolname)")
            with self.subTest(template=template):
                self.assertEqual(guard.check_sql(sql, "postgres"), [])
                self.assertTrue(guard.check_sql(sql, "mysql"))
        self.assertEqual(guard.check_sql(body("EXECUTE pg_catalog.format($acl$GRANT USAGE ON SCHEMA reports TO %I$acl$, role_name)"), "postgres"), [])
        self.assertEqual(guard.check_sql(body("EXECUTE pg_catalog.format('GRANT USAGE ON SCHEMA reports TO %I', r.rolname)"), "postgres"), [])

    def test_unknown_templates_and_sql_interpolation_remain_opaque(self):
        for command in [
            "EXECUTE query_text",
            "RETURN QUERY EXECUTE query_text USING start_time, end_time",
            "EXECUTE pg_catalog.format(template, r.rolname)",
            "EXECUTE pg_catalog.format('GRANT USAGE ON SCHEMA reports TO %s', r.rolname)",
            "EXECUTE pg_catalog.format('GRANT USAGE ON SCHEMA reports TO %L', r.rolname)",
            "EXECUTE pg_catalog.format('GRANT USAGE ON SCHEMA reports TO %1$I', r.rolname)",
            "EXECUTE pg_catalog.format('GRANT USAGE ON SCHEMA reports TO ' || '%I', r.rolname)",
            "EXECUTE pg_catalog.format('GRANT USAGE ON SCHEMA reports TO %I', calculate_role())",
            "EXECUTE pg_catalog.format('GRANT USAGE ON SCHEMA reports TO %I', r.rolname, other_role)",
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
                sql = body("EXECUTE pg_catalog.format('" + template + "', r.rolname)")
                self.assertIn("dynamic SQL", str(guard.check_sql(sql, "postgres")))

    def test_only_identifier_quoting_can_carry_an_injection_shaped_role(self):
        sql = "DECLARE r record; BEGIN r.rolname := 'reader; DROP TABLE accounts; --'; "
        sql += "EXECUTE pg_catalog.format('GRANT USAGE ON SCHEMA reports TO %I', r.rolname); END;"
        self.assertEqual(guard.check_sql("DO $$" + sql + "$$;", "postgres"), [])
        self.assertTrue(guard.check_sql("DO $$" + sql.replace("%I", "%s") + "$$;", "postgres"))

    def test_acl_does_not_hide_real_mutations_or_manufacture_a_marker(self):
        acl = "EXECUTE pg_catalog.format('GRANT USAGE ON SCHEMA reports TO %I', r.rolname)"
        self.assertIn("DELETE without WHERE", str(guard.check_sql(body(acl + "; DELETE FROM accounts"), "postgres")))
        self.assertTrue(guard.check_sql(body(acl + "; EXECUTE query_text"), "postgres"))
        self.assertEqual(guard.check_sql("-- contract: released-expand-42\n" + body("EXECUTE query_text"), "postgres"), [])


if __name__ == "__main__":
    unittest.main()
