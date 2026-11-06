"""Fresh-table delete actions and historical identity at the public entrypoint."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest

from test_migration_guard import guard


class FreshTableTests(unittest.TestCase):
    def test_new_table_cascades_only(self):
        for dialect, table in (("postgres", '"app"."Child"'), ("mysql", '`app`.`Child`')):
            for conditional in ("", "IF NOT EXISTS "):
                sql = f'''CREATE /* CASCADE */ TABLE {conditional}{table} (
                    id int, parent_id int REFERENCES parent(id) ON DELETE CASCADE,
                    other_id int, FOREIGN KEY (other_id) REFERENCES parent(id) ON DELETE CASCADE,
                    note text DEFAULT 'ON DELETE CASCADE; DROP TABLE child'
                );'''
                with self.subTest(dialect=dialect, conditional=conditional):
                    self.assertEqual(guard.check_sql(sql, dialect), [])
                    self.assertTrue(guard.check_sql(sql, dialect, historical_tables={"child"}))
                    self.assertTrue(guard.check_sql(sql.replace('ON DELETE CASCADE,', 'ON DELETE SET NULL,'), dialect))
                    self.assertTrue(guard.check_sql(sql.replace('ON DELETE CASCADE,', 'ON DELETE SET DEFAULT,'), dialect))

    def test_other_statement_on_same_line_is_not_exempt(self):
        for dialect, table in (("postgres", '"app"."child"'), ("mysql", '`app`.`child`')):
            for action in ("CASCADE", "SET NULL", "SET DEFAULT"):
                sql = ("CREATE TABLE fresh (id int REFERENCES parent(id) ON DELETE CASCADE); "
                       f"aLtEr /* ON DELETE RESTRICT */ tAbLe {table} ADD CONSTRAINT fk "
                       f"FOREIGN KEY (id) REFERENCES parent(id) oN /* comment */ dElEtE {action};")
                errors = guard.check_sql(sql, dialect)
                with self.subTest(dialect=dialect, action=action):
                    self.assertEqual(len(errors), 1)
                    self.assertIn(f"ON DELETE {action} changes child data", errors[0][1])
                    self.assertIn("statement: aLtEr", errors[0][1])

    def test_comments_literals_and_quoted_keywords_are_not_actions(self):
        for dialect in ("postgres", "mysql"):
            self.assertEqual(guard.check_sql("SELECT 'ON DELETE CASCADE', 'ON DELETE SET DEFAULT'; /* ON DELETE SET NULL */ -- ON DELETE CASCADE\n", dialect), [])
        self.assertEqual(guard.check_sql('CREATE TABLE "ON DELETE CASCADE" ("ON" text, "DELETE" text, "CASCADE" text);', 'postgres'), [])

    def test_drop_recreate_keeps_both_findings(self):
        for dialect in ("postgres", "mysql"):
            errors = guard.check_sql("DROP TABLE child; CREATE TABLE child (id int REFERENCES parent(id) ON DELETE CASCADE);", dialect)
            self.assertIn((1, "DROP TABLE"), errors)
            self.assertIn("ON DELETE CASCADE changes child data", str(errors))

    def test_procedural_create_does_not_claim_freshness(self):
        sql = "DO $$ BEGIN CREATE TABLE child (id int REFERENCES parent(id) ON DELETE CASCADE); END $$;"
        self.assertIn("ON DELETE CASCADE changes child data", str(guard.check_sql(sql, "postgres")))


class HistoryCascadeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        self.write("0001_initial.sql", 'CREATE TABLE "app"."child" (id int); CREATE TABLE parent (id int PRIMARY KEY);')
        self.base = self.commit()

    def git(self, *args):
        return guard.git(self.root, *args).strip()

    def write(self, path, sql):
        target = self.root / "db" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(sql)

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-qm", "Fixture")
        return self.git("rev-parse", "HEAD")

    def run_guard(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = guard.main(["--dialect", "postgres", "--directory", "db", "--base", self.base, "--head", "HEAD"], {"GITHUB_WORKSPACE": str(self.root)})
        return code, output.getvalue()

    def test_two_new_tables_four_cascades_pass_entrypoint(self):
        for table in ("first_child", "second_child"):
            self.write(f"0002_{table}.sql" if table == "first_child" else "0003_second_child.sql", f'''CREATE TABLE {table} (
                id int,
                parent_id int REFERENCES parent(id) ON DELETE CASCADE,
                other_id int REFERENCES parent(id) ON DELETE CASCADE
            );''')
        self.commit()
        self.assertEqual(self.run_guard()[0], 0)

    def test_historical_names_and_conditional_create_refused(self):
        for table in ('child', 'app.child', '"app"."child"', '"other"."CHILD"'):
            for conditional in ("", "IF NOT EXISTS "):
                with self.subTest(table=table, conditional=conditional):
                    self.write("0002_fk.sql", f"CREATE TABLE {conditional}{table} (id int REFERENCES parent(id) ON DELETE CASCADE);")
                    self.commit()
                    code, output = self.run_guard()
                    self.assertEqual(code, 1)
                    self.assertIn("ON DELETE CASCADE changes child data; statement: CREATE TABLE", output)
                    self.assertNotIn("Merged migrations are immutable", output)
                    self.assertNotIn("Ship an expand step", output)

    def test_earlier_file_in_same_range_is_historical(self):
        self.write("0002_create.sql", 'CREATE TABLE fresh (id int);')
        self.write("0003_fk.sql", 'CREATE TABLE IF NOT EXISTS fresh (id int REFERENCES parent(id) ON DELETE CASCADE);')
        self.commit()
        self.assertEqual(self.run_guard()[0], 1)

    def test_drop_recreate_entrypoint_refuses(self):
        self.write("0002_recreate.sql", 'DROP TABLE child; CREATE TABLE child (id int REFERENCES parent(id) ON DELETE CASCADE);')
        self.commit()
        code, output = self.run_guard()
        self.assertEqual(code, 1)
        self.assertIn("DROP TABLE", output)
        self.assertIn("ON DELETE CASCADE changes child data", output)
