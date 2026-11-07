"""MySQL client boundaries and inspectable trigger bodies preserve SQL findings."""
from pathlib import Path
import importlib.util
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("migration_guard", ROOT / ".github/actions/migration-guard/migration_guard.py")
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)
MARKER = "-- contract: released-expand-42\n"


class MysqlTriggerTests(unittest.TestCase):
    def test_single_statement_refusal_reproduction(self):
        sql = """CREATE TRIGGER care_example BEFORE UPDATE ON care_grant_audit FOR EACH ROW
SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='immutable';"""
        self.assertEqual(guard.check_sql(sql, "mysql"), [])
        self.assertEqual(guard.violations(sql, "mysql"), [])
        self.assertEqual(guard.check_sql("CREATE TRIGGER x BEFORE UPDATE ON t FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='immutable';", "mysql"), [])
        self.assertIn((2, "DELETE without WHERE"), guard.check_sql(sql.replace(
            "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='immutable'", "DELETE FROM care_grant_audit"), "mysql"))

    def test_delimiter_refusal_reproduction(self):
        sql = """DELIMITER $$
CREATE TRIGGER care_example BEFORE INSERT ON care_grant_audit FOR EACH ROW
BEGIN
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='immutable';
END$$
DELIMITER ;"""
        self.assertEqual(guard.check_sql(sql, "mysql"), [])
        self.assertEqual(guard.violations(sql, "mysql"), [])
        for body, reason in [("DELETE FROM care_grant_audit", "DELETE without WHERE"),
                             ("PREPARE q FROM @sql", "dynamic SQL"),
                             ("EXECUTE q", "dynamic SQL")]:
            with self.subTest(body=body):
                candidate = sql.replace("SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='immutable'", body)
                self.assertIn(reason, str(guard.check_sql(candidate, "mysql")))

    def test_trigger_headers_are_not_dml(self):
        for timing in ("BEFORE", "AFTER"):
            for event in ("INSERT", "UPDATE", "DELETE"):
                for prefix in ("CREATE TRIGGER x", "CREATE TRIGGER IF NOT EXISTS x",
                               "CREATE DEFINER = CURRENT_USER() TRIGGER `schema`.`x`",
                               "CREATE DEFINER = 'owner'@'localhost' TRIGGER x"):
                    for order in ("", " FOLLOWS earlier", " PRECEDES `earlier`"):
                        sql = f"{prefix} {timing} {event} ON `schema`.`accounts` FOR EACH ROW{order} SIGNAL SQLSTATE '45000';"
                        with self.subTest(sql=sql):
                            self.assertEqual(guard.check_sql(sql, "mysql"), [])

    def test_read_only_conditional_body_and_row_comparisons_pass(self):
        # An anonymized schema-only example: nested reads either allow the
        # requested row operation or reject it without changing stored rows.
        sql = """DELIMITER $$
CREATE TRIGGER immutable_update BEFORE UPDATE ON accounts FOR EACH ROW
BEGIN
    DECLARE expected_owner VARCHAR(64) DEFAULT NULL;
    SELECT owner_name INTO expected_owner FROM bindings WHERE id = NEW.binding_id;
    IF expected_owner IS NULL OR NOT (BINARY NEW.owner <=> BINARY OLD.owner) THEN
        IF EXISTS (SELECT 1 FROM bindings WHERE id = OLD.binding_id) THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'immutable';
        END IF;
    ELSE
        IF NEW.expires_at > OLD.expires_at THEN
            SIGNAL SQLSTATE '45000';
        END IF;
    END IF;
END$$
CREATE TRIGGER immutable_delete BEFORE DELETE ON accounts FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000';
END$$
DELIMITER ;
CREATE TRIGGER immutable_insert BEFORE INSERT ON accounts FOR EACH ROW
SIGNAL SQLSTATE '45000';
"""
        self.assertEqual(guard.check_sql(sql, "mysql"), [])

    def test_row_field_names_are_identifiers_even_when_they_spell_sql_verbs(self):
        for field in ("update", "delete", "truncate", "prepare", "execute", "call"):
            body = f"IF NEW.{field} <> OLD.{field} THEN SIGNAL SQLSTATE '45000'; END IF;"
            sql = "CREATE TRIGGER x BEFORE UPDATE ON accounts FOR EACH ROW BEGIN " + body + " END;"
            with self.subTest(field=field):
                self.assertEqual(guard.check_sql(sql, "mysql"), [])
                self.assertIn("DELETE without WHERE", str(guard.check_sql(sql.replace("END;", "DELETE FROM accounts; END;"), "mysql")))
                self.assertIn("dynamic SQL", str(guard.check_sql(sql.replace("END;", "EXECUTE q; END;"), "mysql")))

    def test_each_body_mutation_needs_its_own_where(self):
        for body, reason in [
            ("DELETE FROM accounts; SELECT id FROM other WHERE id=1;", "DELETE without WHERE"),
            ("UPDATE accounts SET active=0; SELECT id FROM other WHERE id=1;", "UPDATE without WHERE"),
            ("IF EXISTS (SELECT 1 FROM other WHERE id=1) THEN DELETE FROM accounts; END IF;", "DELETE without WHERE"),
            ("BEGIN UPDATE accounts SET active=0; END;", "UPDATE without WHERE"),
            ("DELETE FROM accounts; UPDATE other SET active=0 WHERE id=1;", "DELETE without WHERE"),
            ("DROP TABLE accounts;", "DROP TABLE"),
            ("TRUNCATE accounts;", "TRUNCATE"),
        ]:
            for delimiter in (";", "$$", "//", "!!"):
                sql = "CREATE TRIGGER x BEFORE UPDATE ON accounts FOR EACH ROW BEGIN " + body + " END" + delimiter
                if delimiter != ";":
                    sql = "DELIMITER " + delimiter + "\n" + sql + "\nDELIMITER ;"
                with self.subTest(body=body, delimiter=delimiter):
                    self.assertIn(reason, str(guard.check_sql(sql, "mysql")))
        self.assertEqual(guard.check_sql("CREATE TRIGGER x BEFORE UPDATE ON accounts FOR EACH ROW BEGIN UPDATE other SET active=0 WHERE id=NEW.id; DELETE FROM other WHERE id=OLD.id; END;", "mysql"), [])

    def test_loop_updates_are_not_postgres_upsert_clauses(self):
        for body in (
            "WHILE NEW.id = 1 DO UPDATE other SET active=0; END WHILE;",
            "REPEAT UPDATE other SET active=0; UNTIL NEW.id = 1 END REPEAT;",
            "LOOP UPDATE other SET active=0; END LOOP;",
        ):
            plain = "CREATE TRIGGER t BEFORE UPDATE ON x FOR EACH ROW BEGIN " + body + " END;"
            wrapped = "DELIMITER $$\n" + plain[:-1] + "$$\nDELIMITER ;"
            for sql in (plain, wrapped):
                for check in (guard.check_sql, guard.violations):
                    with self.subTest(body=body, wrapped=sql == wrapped, check=check.__name__):
                        self.assertIn("UPDATE without WHERE", str(check(sql, "mysql")))
                        self.assertEqual(check(sql.replace("SET active=0;", "SET active=0 WHERE id=NEW.id;"), "mysql"), [])

    def test_postgres_conflict_updates_and_anonymous_blocks_remain_distinct(self):
        for clause in (
            "ON CONFLICT (id)",
            "ON CONFLICT ON CONSTRAINT accounts_pkey",
            "ON CONFLICT (id) WHERE active > 0",
        ):
            sql = "INSERT INTO accounts (id) VALUES (1) " + clause + " DO UPDATE SET active=1;"
            with self.subTest(clause=clause):
                self.assertEqual(guard.check_sql(sql, "postgres"), [])
                self.assertEqual(guard.violations(sql, "postgres"), [])
                self.assertIn("UPDATE without WHERE", str(guard.check_sql(sql + " UPDATE other SET active=0;", "postgres")))
        for sql in (
            "DO $$ BEGIN UPDATE accounts SET active=0; END $$;",
            "DO $$ BEGIN WHILE true LOOP UPDATE accounts SET active=0; END LOOP; END $$;",
            "DO $$ BEGIN INSERT INTO accounts(id) VALUES(1) ON CONFLICT(id) DO UPDATE SET active=1; UPDATE other SET active=0; END $$;",
        ):
            with self.subTest(sql=sql):
                self.assertIn("UPDATE without WHERE", str(guard.check_sql(sql, "postgres")))
                self.assertIn("UPDATE without WHERE", str(guard.violations(sql, "postgres")))

    def test_postgres_targetless_do_update_is_not_an_upsert_exemption(self):
        # PostgreSQL 18: DO UPDATE "conflict_target must be provided": https://www.postgresql.org/docs/18/sql-insert.html#SQL-ON-CONFLICT
        sql = "INSERT INTO accounts(id,active) VALUES(1,1) ON CONFLICT DO UPDATE SET active=1;"
        for check in (guard.check_sql, guard.violations):
            with self.subTest(check=check.__name__):
                self.assertEqual(check(sql, "postgres"), [(1, "UPDATE without WHERE")])
                self.assertEqual(check(sql.replace("ON CONFLICT DO", "ON CONFLICT (id) DO"), "postgres"), [])

    def test_postgres_conflict_predicate_case_is_an_expression(self):
        sql = "INSERT INTO accounts(id,active) VALUES(1,1) ON CONFLICT (id) WHERE CASE WHEN active > 0 THEN true ELSE false END DO UPDATE SET active=1;"
        for candidate in (
            sql,
            sql.replace("THEN true", "THEN CASE WHEN active > 1 THEN true ELSE false END"),
            sql.replace("WHERE CASE", "WHERE (CASE").replace("END DO", "END) DO"),
        ):
            with self.subTest(sql=candidate):
                self.assertEqual(guard.check_sql(candidate, "postgres"), [])
                self.assertEqual(guard.violations(candidate, "postgres"), [])
        sql = "CREATE TRIGGER t BEFORE UPDATE ON x FOR EACH ROW BEGIN IF NEW.id = 1 THEN UPDATE other SET active=0; END IF; END;"
        self.assertIn("UPDATE without WHERE", str(guard.check_sql(sql, "mysql")))
        self.assertIn("UPDATE without WHERE", str(guard.violations(sql, "mysql")))

    def test_dynamic_sql_is_refused_in_every_control_scope(self):
        for statement in ("PREPARE q FROM @sql", "EXECUTE q", "CALL cleanup()"):
            for body in (statement + ";", "IF NEW.id=1 THEN " + statement + "; END IF;",
                         "DECLARE CONTINUE HANDLER FOR SQLEXCEPTION " + statement + ";",
                         "WHILE NEW.id=1 DO " + statement + "; END WHILE;"):
                sql = "DELIMITER $$\nCREATE TRIGGER x BEFORE INSERT ON accounts FOR EACH ROW BEGIN " + body + " END$$\nDELIMITER ;"
                with self.subTest(body=body):
                    self.assertIn("dynamic SQL", str(guard.check_sql(sql, "mysql")))

    def test_delimiters_cannot_hide_following_statements_or_leak_markers(self):
        for delimiter in ("$$", "//", "!!", ";;"):
            safe = "CREATE TRIGGER x BEFORE UPDATE ON accounts FOR EACH ROW BEGIN SIGNAL SQLSTATE '45000'; END"
            sql = f"DELIMITER {delimiter}\n{safe}{delimiter}\nDELETE FROM accounts{delimiter}\nDELIMITER ;\nUPDATE accounts SET active=0;"
            expected = [(3, "DELETE without WHERE"), (5, "UPDATE without WHERE")]
            with self.subTest(delimiter=delimiter):
                self.assertEqual(guard.check_sql(sql, "mysql"), expected)
                self.assertEqual(guard.check_sql(MARKER + sql, "mysql"), [(4, "DELETE without WHERE"), (6, "UPDATE without WHERE")])
                # Semicolons remain SQL boundaries even when the client sends
                # multiple statements in one custom-delimited envelope.
                compound = f"DELIMITER {delimiter}\nSELECT 1; DELETE FROM accounts{delimiter}\nDELIMITER ;"
                self.assertEqual(guard.check_sql(compound, "mysql"), [(2, "DELETE without WHERE")])

    def test_delimiters_adjacent_to_variables_and_word_suffixes_keep_boundaries(self):
        for delimiter in ("$$", "$$."):
            sql = f"DELIMITER {delimiter}\n" + MARKER + f"SELECT @name{delimiter}\nDELETE FROM accounts{delimiter}\nDELIMITER ;"
            with self.subTest(delimiter=delimiter):
                self.assertEqual(guard.check_sql(sql, "mysql"), [(4, "DELETE without WHERE")])
                sql = f"DELIMITER {delimiter}\n" + MARKER + f"SELECT name{delimiter}\nDELETE FROM accounts{delimiter}\nDELIMITER ;"
                self.assertEqual(guard.check_sql(sql, "mysql"), [(4, "DELETE without WHERE")])

    def test_delimiter_tokens_inside_quotes_and_comments_are_inert(self):
        sql = """-- DELIMITER !!
DELIMITER $$
CREATE TRIGGER x BEFORE UPDATE ON accounts FOR EACH ROW
BEGIN
  /* DELIMITER ; $$ */
  IF NEW.`value$$` <> OLD.`value$$` THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='END$$; DELIMITER ; DELETE FROM accounts';
  END IF;
END$$
DELIMITER ;
"""
        self.assertEqual(guard.check_sql(sql, "mysql"), [])
        self.assertEqual(guard.check_sql("SELECT 'DELIMITER $$';\n# DELIMITER !!\nDELETE FROM accounts;", "mysql"), [(3, "DELETE without WHERE")])

    def test_malformed_unbalanced_or_midstatement_delimiters_fail_closed(self):
        for sql in (
            "DELIMITER", "DELIMITER $$ extra", "DELIMITER '$$'", "DELIMITER \\",
            "DELIMITER --", "DELIMITER /*", "DELIMITER $$\nSELECT 1$$",
            "DELIMITER $$\nSELECT 1;\nDELIMITER ;",
            "SELECT 1\nDELIMITER $$\nDELIMITER ;",
            "DELIMITER $$\nCREATE TRIGGER x BEFORE INSERT ON accounts FOR EACH ROW BEGIN SELECT 1;$$\nDELIMITER ;",
            "DELIMITER $$\nCREATE TRIGGER x BEFORE INSERT ON accounts FOR EACH ROW BEGIN SELECT 1; END;\nDELIMITER ;",
            "DELIMITER $$\nSELECT (1$$\nDELIMITER ;",
        ):
            for marker in ("", MARKER):
                with self.subTest(sql=sql, marker=marker), self.assertRaises(guard.SqlError):
                    guard.check_sql(marker + sql, "mysql")

    def test_unsupported_trigger_headers_stay_opaque(self):
        for header in ("CREATE TRIGGER x BEFORE UPDATE accounts FOR EACH ROW",
                       "CREATE TRIGGER x BEFORE UPDATE ON accounts",
                       "CREATE TRIGGER x BEFORE UPDATE OR DELETE ON accounts FOR EACH ROW",
                       "CREATE TRIGGER x INSTEAD OF UPDATE ON accounts FOR EACH ROW",
                       "CREATE TRIGGER x BEFORE UPDATE ON accounts FOR EACH ROW FOLLOWS",
                       "CREATE DEFINER = TRIGGER x BEFORE UPDATE ON accounts FOR EACH ROW"):
            with self.subTest(header=header):
                self.assertIn("opaque procedural body", str(guard.check_sql(header + " SIGNAL SQLSTATE '45000';", "mysql")))

    def test_body_facts_do_not_authorize_later_migration_statements(self):
        sql = "CREATE TRIGGER x BEFORE INSERT ON accounts FOR EACH ROW BEGIN ALTER TABLE other ALTER region SET DEFAULT ''; END; ALTER TABLE other ALTER region SET NOT NULL;"
        self.assertIn("SET NOT NULL", str(guard.check_sql(sql, "mysql")))
        sql = "DELIMITER $$\n" + MARKER + "CREATE TRIGGER x BEFORE INSERT ON accounts FOR EACH ROW BEGIN DELETE FROM accounts; END$$\nDELETE FROM accounts$$\nDELIMITER ;"
        self.assertEqual(guard.check_sql(sql, "mysql"), [(4, "DELETE without WHERE")])


if __name__ == "__main__":
    unittest.main()
