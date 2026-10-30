"""Closed literal query construction is inspected; opaque dataflow stays refused."""
from pathlib import Path
import hashlib
import importlib.util
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("finite_guard", ROOT / ".github/actions/migration-guard/migration_guard.py")
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


# Exact historical source; no product-name or hash allowance exists in the guard.
REVENUE_SQL = r"""-- Aggregate read projections only; no stored player, payment or migration-ledger row changes.
CREATE SCHEMA IF NOT EXISTS desk_report;
CREATE OR REPLACE FUNCTION desk_report.revenue_summary(
 since_day date DEFAULT current_date - 30, until_day date DEFAULT current_date)
RETURNS TABLE(day date, source text, currency text, paid_orders bigint,
 unique_payers bigint, gross_minor bigint, net_minor bigint, owed_open_orders bigint)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog
AS $report$
DECLARE
 delivery text;
 engine text := '';
 query_text text;
BEGIN
 IF since_day IS NULL OR until_day IS NULL OR NOT isfinite(since_day)
    OR NOT isfinite(until_day) OR since_day > until_day THEN RETURN; END IF;
 IF to_regclass('public.store_transaction_grant') IS NOT NULL THEN
  delivery := 'SELECT min(g.uid)::text AS payer, count(*) > 0 AS delivered FROM public.store_transaction_grant g WHERE g.transaction_id = p.transaction_id';
 ELSIF to_regclass('public.purchase_log') IS NOT NULL THEN
  delivery := 'SELECT min(g.uid)::text AS payer, count(*) > 0 AS delivered FROM public.purchase_log g WHERE g.order_id = p.transaction_id';
 ELSE
  RAISE EXCEPTION 'Verified purchase delivery projection is unavailable';
 END IF;
 IF to_regclass('tycoon.purchases') IS NOT NULL THEN
  engine := $engine$
   UNION ALL
   SELECT o.purchase_key, (o.purchase_time AT TIME ZONE 'UTC')::date,
    split_part(o.purchase_key, ':', 1), o.player_id::text,
    o.state = 'parked', 2
   FROM tycoon.purchases o
   WHERE o.environment = 'production' AND o.state IN ('credited','parked')
    AND o.source = 'new' AND o.purchase_time IS NOT NULL
    AND o.purchase_key ~ '^(app_store|google_play|web):.+'
    AND o.purchase_time >= ($1::timestamp AT TIME ZONE 'UTC')
    AND o.purchase_time < (($2 + 1)::timestamp AT TIME ZONE 'UTC')
    AND NOT EXISTS (SELECT 1 FROM tycoon.purchase_reversals r WHERE r.purchase_key = o.purchase_key)
  $engine$;
 END IF;
 query_text := $query$
  WITH raw(transaction_key, sale_day, store, payer, owed, priority) AS (
   SELECT 'app_store:' || p.transaction_id,
    (to_timestamp(p.purchased_at) AT TIME ZONE 'UTC')::date,
    'app_store', coalesce(d.payer,p.uid::text),
    p.status = 'open' AND NOT d.delivered, 1
   FROM public.store_unclaimed_purchase p
   CROSS JOIN LATERAL ($query$ || delivery || $query$) d
   WHERE p.environment = 'Production' AND coalesce(p.revoked_at,0) = 0
    AND p.status IN ('open','granted') AND p.transaction_id <> ''
    AND p.purchased_at > 0
    AND p.purchased_at >= extract(epoch FROM ($1::timestamp AT TIME ZONE 'UTC'))
    AND p.purchased_at < extract(epoch FROM (($2+1)::timestamp AT TIME ZONE 'UTC'))
   $query$ || engine || $query$
  ), observed AS (
   SELECT DISTINCT ON (transaction_key) transaction_key,sale_day,store,payer,owed
   FROM raw ORDER BY transaction_key,priority
  )
  SELECT sale_day, store || '_observed_unpriced', 'XXX'::text,
   count(*), CASE WHEN bool_and(payer IS NOT NULL) THEN count(DISTINCT payer) ELSE NULL::bigint END,
   NULL::bigint, NULL::bigint, count(*) FILTER(WHERE owed)
  FROM observed GROUP BY sale_day,store ORDER BY sale_day,store
 $query$;
 RETURN QUERY EXECUTE query_text USING since_day,until_day;
END;
$report$;
COMMENT ON FUNCTION desk_report.revenue_summary(date,date) IS
 'Inclusive UTC dates. Observed non-revoked production Apple receipts and new engine purchases, deduplicated only by exact store transaction key. Not a complete sales census. XXX and NULL cash mean local records cannot establish price, currency or settled fees; store exports remain cash authority. Missing mapped payer makes unique_payers NULL. Live mode does not establish natural-person provenance. No player identifiers leave the function.';

REVOKE ALL ON FUNCTION desk_report.revenue_summary(date,date) FROM PUBLIC;
"""


def routine(body, declarations="q text;", path="pg_catalog"):
    setting = " SET search_path = " + path if path else ""
    return ("CREATE FUNCTION reports.f(input text, flag boolean) RETURNS SETOF text LANGUAGE plpgsql"
            + setting + " AS $body$ DECLARE " + declarations + " BEGIN " + body + " END; $body$;")


def query(sql):
    return routine("q := $query$" + sql + "$query$; RETURN QUERY EXECUTE q USING input;")


class FiniteQueryTests(unittest.TestCase):
    def assert_refused(self, sql):
        self.assertIn("dynamic SQL", str(guard.check_sql(sql, "postgres")))

    def test_exact_revenue_checks_all_four_expansions(self):
        self.assertEqual(hashlib.sha256(REVENUE_SQL.encode()).hexdigest(),
                         "d0fd4d2d9433cc4939bc246967b801d6b9d757bf173ce5d6d331ba0b2b1dabb9")
        with patch.object(guard, "finite_read_query", wraps=guard.finite_read_query) as checked:
            self.assertEqual(guard.check_sql(REVENUE_SQL, "postgres"), [])
        expansions = [call.args[0] for call in checked.call_args_list if not call.kwargs.get("predicate")]
        self.assertEqual(len(expansions), 4)
        self.assertEqual({hashlib.sha256(text.encode()).hexdigest() for text in expansions}, {
            "8e96eaecee3aed6875cc4f375414b491830dec72bfa0869baee0574c01065d28",
            "cb2d869f3cb74baf1006de43db3e514260623f247069cfe8b78c47cd6ea0d744",
            "20b15d32736ac00aa8adcfefd4e53f31461cd037ffb5f8cfdc4428033c2fa642",
            "5538b2b3a76861e29dc2622186e58e63e398a986c3e4f1c1303a2a755ddeca29",
        })

    def test_two_by_two_closed_branches_are_all_checked(self):
        sql = routine("""
            IF flag THEN RETURN; END IF;
            IF flag THEN fragment := '1';
            ELSIF input IS NULL THEN fragment := '2';
            ELSE RAISE EXCEPTION 'no source'; END IF;
            IF flag THEN suffix := '::text'; END IF;
            q := 'SELECT ' || fragment || suffix;
            RETURN QUERY EXECUTE q USING input;
        """, "fragment text; suffix text := ''; q text;")
        with patch.object(guard, "finite_read_query", wraps=guard.finite_read_query) as checked:
            self.assertEqual(guard.check_sql(sql, "postgres"), [])
        self.assertEqual({call.args[0] for call in checked.call_args_list if not call.kwargs.get("predicate")},
                         {"SELECT 1", "SELECT 2", "SELECT 1::text", "SELECT 2::text"})

    def test_builtin_aggregates_ctes_literals_and_using_pass(self):
        for text in ["SELECT 'it''s safe'::text", "SELECT pg_catalog.count(*) FROM accounts",
                     "WITH a(x) AS (SELECT 1), b AS (SELECT x FROM a) SELECT count(*) FROM b",
                     "SELECT $1::date", "SELECT count(*) FILTER(WHERE active) FROM accounts"]:
            with self.subTest(text=text):
                self.assertEqual(guard.check_sql(query(text), "postgres"), [])
        self.assertEqual(guard.check_sql(routine("RETURN QUERY EXECUTE q;", "q text := 'SELECT 1';"), "postgres"), [])

    def test_unknown_nonliteral_mutable_and_missing_paths_fail_closed(self):
        for body in [
            "RETURN QUERY EXECUTE q;",
            "q := input; RETURN QUERY EXECUTE q;",
            "q := 'SELECT ' || input; RETURN QUERY EXECUTE q;",
            "q := make_query(); RETURN QUERY EXECUTE q;",
            "q := format('SELECT %s', input); RETURN QUERY EXECUTE q;",
            "q := 'SELECT 1'; q := 'SELECT 2'; RETURN QUERY EXECUTE q;",
            "q := 'SELECT 1'; RETURN QUERY EXECUTE q; q := 'SELECT 2';",
            "IF flag THEN q := 'SELECT 1'; END IF; RETURN QUERY EXECUTE q;",
            "IF flag THEN q := 'SELECT 1'; ELSE q := input; END IF; RETURN QUERY EXECUTE q;",
            "q := 'SELECT 1'; SELECT input INTO q; RETURN QUERY EXECUTE q;",
            "q := 'SELECT 1'; BEGIN q := input; END; RETURN QUERY EXECUTE q;",
            "q := 'SELECT 1'; LOOP q := input; END LOOP; RETURN QUERY EXECUTE q;",
            "q := 'SELECT 1'; FOR q IN SELECT input LOOP NULL; END LOOP; RETURN QUERY EXECUTE q;",
            "q := 'SELECT 1'; RETURN QUERY EXECUTE q USING make_parameter();",
            "IF mutate() THEN q := 'SELECT 1'; ELSE q := 'SELECT 2'; END IF; RETURN QUERY EXECUTE q;",
            "IF input::public.custom_type IS NULL THEN RETURN; END IF; q := 'SELECT 1'; RETURN QUERY EXECUTE q;",
            "q := 'SELECT 1'; DECLARE q text := input; BEGIN NULL; END; RETURN QUERY EXECUTE q;",
            "q := 'SELECT 1'; RETURN QUERY EXECUTE q; EXCEPTION WHEN others THEN EXECUTE input;",
        ]:
            with self.subTest(body=body):
                self.assert_refused(routine(body))

    def test_all_alternatives_and_sibling_execution_remain_visible(self):
        for sibling in ["EXECUTE input", "EXECUTE 'DROP TABLE accounts'", "CALL cleanup()",
                        "IF flag THEN EXECUTE input; END IF", "DROP TABLE accounts"]:
            for body in ["q := 'SELECT 1'; " + sibling + "; RETURN QUERY EXECUTE q;",
                         "q := 'SELECT 1'; RETURN QUERY EXECUTE q; " + sibling + ";"]:
                with self.subTest(body=body):
                    self.assert_refused(routine(body))
        for bad in ["DELETE FROM accounts", "SELECT mutate()"]:
            self.assert_refused(routine("IF flag THEN q := 'SELECT 1'; ELSE q := '" + bad
                                        + "'; END IF; RETURN QUERY EXECUTE q;"))

    def test_select_prefix_does_not_authorize_mutations_or_side_effect_calls(self):
        for text in [
            "WITH gone AS (DELETE FROM accounts WHERE id=1 RETURNING *) SELECT * FROM gone",
            "WITH changed AS (UPDATE accounts SET active=false WHERE id=1 RETURNING *) SELECT * FROM changed",
            "WITH added AS (INSERT INTO accounts VALUES (1) RETURNING *) SELECT * FROM added",
            "UPDATE accounts SET active=false WHERE id=1 RETURNING *",
            "SELECT * INTO saved FROM accounts", "SELECT * FROM accounts FOR UPDATE",
            "SELECT 1; SELECT 2", "SELECT 1; DROP TABLE accounts",
            "SELECT nextval('seq')", "SELECT set_config('search_path','public',false)",
            "SELECT public.count(*) FROM accounts", "SELECT mutate()",
            "SELECT 1, mutate() AS harmless", "SELECT 1::public.custom_type",
            "SELECT public.custom_type 'payload'", "SELECT public.text 'payload'",
            "SELECT pg_catalog.pg_advisory_lock(1)", "SELECT dblink_exec('x','DELETE FROM accounts')",
        ]:
            with self.subTest(text=text):
                self.assert_refused(query(text))

    def test_fixed_catalog_path_is_required_and_scope_is_local(self):
        body = "q := 'SELECT count(*) FROM accounts'; RETURN QUERY EXECUTE q;"
        for path in [None, "public, pg_catalog", "pg_catalog, public"]:
            self.assert_refused(routine(body, path=path))
        for mutation in ["SET search_path=public", "PERFORM public.configure_session()", "RESET ALL"]:
            self.assert_refused(routine(mutation + "; " + body))
        self.assertTrue(guard.check_sql(query("SELECT 1"), "mysql"))
        self.assert_refused("DO $$ DECLARE q text; BEGIN " + body + " END; $$;")
        nested = "CREATE FUNCTION reports.inner() RETURNS SETOF text LANGUAGE plpgsql AS $$ DECLARE q text; BEGIN " + body + " END; $$;"
        self.assert_refused(routine(nested))

    def test_path_depth_and_size_bounds_fail_closed(self):
        self.assert_refused(routine("IF flag THEN NULL; END IF; " * 7
                                    + "q := 'SELECT 1'; RETURN QUERY EXECUTE q;"))
        self.assert_refused(routine("IF flag THEN " * 33 + "q := 'SELECT 1'; "
                                    + "END IF; " * 33 + "RETURN QUERY EXECUTE q;"))
        self.assert_refused(query("SELECT '" + "x" * 1_000_001 + "'"))

    def test_acl_boundary_is_unchanged(self):
        for text in ["GRANT EXECUTE ON FUNCTION reports.f(text) TO %I",
                     "REVOKE ALL ON FUNCTION %s FROM %I",
                     "GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA reports TO %I"]:
            self.assert_refused("DO $$ BEGIN EXECUTE format('" + text + "', r.rolname); END; $$;")


if __name__ == "__main__":
    unittest.main()
