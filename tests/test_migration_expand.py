"""Regression tests for proof-based expand exceptions in the migration guard."""
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / ".github/actions/migration-guard"), str(ROOT / "scripts")]
try:
    import migration_guard as guard
    import shared_baseline_check as baseline
finally:
    del sys.path[:2]


# The deployed phase adds the defaulted column, then pins it to that default.
PG_CREATE = """CREATE TABLE public.scopes (id TEXT PRIMARY KEY);
INSERT INTO public.scopes (id) VALUES ('scope_default');
CREATE TABLE public.entries (
    id TEXT CONSTRAINT entries_pkey PRIMARY KEY,
    related_id TEXT CONSTRAINT entries_related_fk REFERENCES public.entries(id),
    slug TEXT NOT NULL,
    notes TEXT,
    CONSTRAINT entries_slug_key UNIQUE (slug)
);
"""
PG_ADD_SCOPE = """ALTER TABLE public.entries ADD COLUMN scope_id TEXT NOT NULL DEFAULT 'scope_default';
"""
PG_PIN = PG_ADD_SCOPE + """ALTER TABLE public.entries ADD CONSTRAINT entries_default_scope
    CHECK (scope_id = 'scope_default') NOT VALID;
"""
PG_EXPAND = """ALTER TABLE public.entries
    DROP CONSTRAINT entries_default_scope,
    ADD CONSTRAINT entries_scope_fk FOREIGN KEY (scope_id) REFERENCES public.scopes(id) ON DELETE RESTRICT,
    ADD CONSTRAINT entries_scope_nonempty CHECK (length(scope_id) > 0);
"""
PG_HISTORY = (PG_CREATE, PG_PIN)


SQLITE_INDEX = """CREATE INDEX entries_open_by_rank ON entries (rank, slug) WHERE status = 'open';
"""
SQLITE_TRIGGER = """CREATE TRIGGER entries_clear_closed_notes AFTER UPDATE OF status ON entries
WHEN NEW.status = 'closed' AND OLD.status <> 'closed'
BEGIN
    UPDATE entries SET notes = NULL WHERE id = NEW.id;
END;
"""
SQLITE_CREATE = """CREATE TABLE scopes (id TEXT PRIMARY KEY);
INSERT INTO scopes (id) VALUES ('scope_default');
CREATE TABLE entries (
    id TEXT PRIMARY KEY NOT NULL,
    slug TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    notes TEXT,
    rank INTEGER NOT NULL DEFAULT 1 CHECK (rank > 0),
    UNIQUE (slug)
);
""" + SQLITE_INDEX + SQLITE_TRIGGER
SQLITE_PIN = """ALTER TABLE entries ADD COLUMN scope_id TEXT NOT NULL DEFAULT 'scope_default'
    CHECK (scope_id = 'scope_default');
"""
SQLITE_EXPAND = """CREATE TABLE entries_scope_upgrade (
    id TEXT PRIMARY KEY NOT NULL,
    slug TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    notes TEXT,
    rank INTEGER NOT NULL DEFAULT 1 CHECK (rank > 0),
    scope_id TEXT NOT NULL DEFAULT 'scope_default' REFERENCES scopes(id) ON DELETE RESTRICT CHECK (length(scope_id) > 0),
    UNIQUE (scope_id, slug)
);
INSERT INTO entries_scope_upgrade (rowid, id, slug, status, notes, rank, scope_id)
SELECT rowid, id, slug, status, notes, rank, scope_id
FROM entries;
DROP TABLE entries;
ALTER TABLE entries_scope_upgrade RENAME TO entries;
""" + SQLITE_INDEX + SQLITE_TRIGGER + """CREATE TRIGGER entries_scope_immutable BEFORE UPDATE OF scope_id ON entries
WHEN NEW.scope_id IS NOT OLD.scope_id
BEGIN SELECT RAISE(ABORT, 'scope is immutable'); END;

ALTER TABLE entries ADD COLUMN external_id BIGINT;
CREATE UNIQUE INDEX entries_external_id ON entries (external_id) WHERE external_id IS NOT NULL;
"""
SQLITE_HISTORY = (SQLITE_CREATE, SQLITE_PIN + """
INSERT INTO entries (rowid, id, slug, notes) VALUES (3, 'a', 'first', 'keep');
INSERT INTO entries (rowid, id, slug, rank) VALUES (47, 'b', 'second', 2);
""")


def change(sql, old, new):
    if sql.count(old) != 1:
        raise AssertionError(f"expected one occurrence of {old!r}")
    return sql.replace(old, new)


def drop_line(sql, clause):
    return sql[:sql.index(clause)].count("\n") + 1


class ExpandSqlTests(unittest.TestCase):
    def check(self, sql, dialect, history=()):
        return guard.drop_phase_findings("", sql, dialect, history=lambda: list(history))

    def assert_drop_at(self, sql, dialect, clause, history=()):
        findings = self.check(sql, dialect, history)
        self.assertIn(drop_line(sql, clause), [line for line, _ in findings], findings)

    def test_postgres_replaces_deployed_pin_in_one_alter(self):
        self.assertEqual(self.check(PG_EXPAND, "postgres", PG_HISTORY), [])

    def test_postgres_if_exists_and_same_file_evidence(self):
        expand = change(PG_EXPAND, "DROP CONSTRAINT entries_default_scope",
                        "DROP CONSTRAINT IF EXISTS entries_default_scope")
        self.assertEqual(self.check(expand, "postgres", PG_HISTORY), [])
        self.assertEqual(self.check(PG_PIN + expand, "postgres", (PG_CREATE,)), [])

    def test_sqlite_in_place_rebuild_preserves_rows_and_schema(self):
        self.assertEqual(self.check(SQLITE_EXPAND, "sqlite", SQLITE_HISTORY), [])

    def test_unphased_drop_is_not_exempt_in_either_dialect(self):
        for dialect, sql, history in (
            ("postgres", "DROP TABLE public.entries;", PG_HISTORY),
            ("sqlite", "DROP TABLE entries;", SQLITE_HISTORY),
        ):
            with self.subTest(dialect=dialect):
                self.assert_drop_at(sql, dialect, "DROP TABLE", history)
                self.assertEqual(guard.drop_phase_findings("", sql, dialect), [(1, "DROP TABLE")])

    def test_postgres_rebuild_is_not_exempt(self):
        sql = """CREATE TABLE public.entries_upgrade (LIKE public.entries INCLUDING ALL);
INSERT INTO public.entries_upgrade (id, related_id, slug, notes, scope_id)
SELECT id, related_id, slug, notes, scope_id FROM public.entries;
DROP TABLE public.entries;
ALTER TABLE public.entries_upgrade RENAME TO entries;
"""
        self.assert_drop_at(sql, "postgres", "DROP TABLE", PG_HISTORY)

    def test_postgres_other_drops_are_not_pins(self):
        for clause in (
            "DROP CONSTRAINT entries_pkey",
            "DROP CONSTRAINT entries_related_fk",
            "DROP CONSTRAINT entries_slug_key",
            "DROP CONSTRAINT entries_unknown",
            "DROP COLUMN notes",
        ):
            with self.subTest(clause=clause):
                sql = change(PG_EXPAND, "DROP CONSTRAINT entries_default_scope", clause)
                self.assert_drop_at(sql, "postgres", clause, PG_HISTORY)

    def test_postgres_requires_prior_pin_evidence(self):
        for name, history in (
            ("absent", (PG_CREATE, PG_ADD_SCOPE)),
            ("different column", (PG_CREATE, change(PG_PIN, "CHECK (scope_id =", "CHECK (slug ="))),
            ("different literal", (PG_CREATE, change(PG_PIN, "scope_id = 'scope_default'", "scope_id = 'other'"))),
        ):
            with self.subTest(name=name):
                self.assert_drop_at(PG_EXPAND, "postgres", "DROP CONSTRAINT", history)

    def test_postgres_requires_validated_nonempty_replacement_in_same_alter(self):
        for name, sql in (
            ("stricter", change(PG_EXPAND, "length(scope_id) > 0", "length(scope_id) > 20")),
            ("unvalidated", change(PG_EXPAND, "CHECK (length(scope_id) > 0);",
                                   "CHECK (length(scope_id) > 0) NOT VALID;")),
            ("unrelated FK", change(PG_EXPAND, "FOREIGN KEY (scope_id)", "FOREIGN KEY (notes)")),
            ("separate alter", change(PG_EXPAND, "DROP CONSTRAINT entries_default_scope,",
                                      "DROP CONSTRAINT entries_default_scope;\nALTER TABLE public.entries")),
        ):
            with self.subTest(name=name):
                self.assert_drop_at(sql, "postgres", "DROP CONSTRAINT", PG_HISTORY)

    def test_postgres_pin_exemption_does_not_cover_same_line_sibling_clause(self):
        for clause, reason in (("DROP COLUMN notes", "DROP COLUMN"),
                               ("DROP CONSTRAINT entries_pkey", "ALTER TABLE DROP")):
            with self.subTest(clause=clause):
                sql = change(PG_EXPAND, "DROP CONSTRAINT entries_default_scope,",
                             f"DROP CONSTRAINT entries_default_scope, {clause},")
                self.assertIn((drop_line(sql, clause), reason), self.check(sql, "postgres", PG_HISTORY))

    def test_same_line_unrelated_drop_statement_is_still_reported(self):
        sql = " ".join(PG_EXPAND.splitlines()) + " DROP TABLE public.obsolete;"
        self.assertIn((1, "DROP TABLE"), self.check(sql, "postgres", PG_HISTORY))

    def test_sqlite_requires_deployed_pin(self):
        unpinned = change(SQLITE_PIN, "\n    CHECK (scope_id = 'scope_default')", "")
        self.assert_drop_at(SQLITE_EXPAND, "sqlite", "DROP TABLE", (SQLITE_CREATE, unpinned))

    def test_sqlite_rejects_nonidentity_rebuilds(self):
        without_notes = change(SQLITE_EXPAND, "    notes TEXT,\n", "")
        without_notes = without_notes.replace("slug, status, notes, rank", "slug, status, rank")
        without_rowid = SQLITE_EXPAND.replace("rowid, id, slug", "id, slug")
        for name, sql in (
            ("dropped column", without_notes),
            ("dropped rowid", without_rowid),
            ("filtered copy", change(SQLITE_EXPAND, "FROM entries;", "FROM entries WHERE rank > 1;")),
            ("rewritten copy", change(SQLITE_EXPAND, "SELECT rowid, id, slug, status, notes, rank, scope_id",
                                      "SELECT rowid, id, slug, status, NULL, rank, scope_id")),
            ("different final table name", change(SQLITE_EXPAND, "RENAME TO entries;", "RENAME TO other_entries;")),
        ):
            with self.subTest(name=name):
                self.assert_drop_at(sql, "sqlite", "DROP TABLE", SQLITE_HISTORY)

    def test_sqlite_rejects_unmodeled_statements_even_after_valid_copy(self):
        for sql in (
            "COMMIT;",
            "DROP TABLE scopes;",
            "DELETE FROM scopes;",
            "CREATE TRIGGER detach_scope AFTER UPDATE ON entries BEGIN DELETE FROM scopes; END;",
        ):
            with self.subTest(sql=sql):
                self.assert_drop_at(SQLITE_EXPAND + sql, "sqlite", "DROP TABLE entries", SQLITE_HISTORY)

    def test_sqlite_rejects_rebuild_that_loses_dependents(self):
        for name, ddl in (("index", SQLITE_INDEX), ("trigger", SQLITE_TRIGGER)):
            with self.subTest(name=name):
                sql = change(SQLITE_EXPAND, ddl, "")
                self.assert_drop_at(sql, "sqlite", "DROP TABLE", SQLITE_HISTORY)

    def test_sqlite_rejects_new_restrictions_on_legacy_rows(self):
        for name, old, new in (
            ("new check", "notes TEXT,", "notes TEXT CHECK (length(notes) > 4),"),
            ("removed unique", ",\n    UNIQUE (scope_id, slug)", ""),
            ("new unique", "UNIQUE (scope_id, slug)", "UNIQUE (scope_id, slug), UNIQUE (status)"),
            ("new unique index on old column", "ALTER TABLE entries ADD COLUMN external_id BIGINT;",
             "CREATE UNIQUE INDEX entries_status_unique ON entries (status);\n"
             "ALTER TABLE entries ADD COLUMN external_id BIGINT;"),
            ("new not null", "notes TEXT,", "notes TEXT NOT NULL,"),
            ("strict scope check", "length(scope_id) > 0", "length(scope_id) > 20"),
        ):
            with self.subTest(name=name):
                self.assert_drop_at(change(SQLITE_EXPAND, old, new), "sqlite", "DROP TABLE", SQLITE_HISTORY)


class ExpandBaselineTests(unittest.TestCase):
    PG_DIR = PurePosixPath("db/migrations_pg")
    SQLITE_DIR = PurePosixPath("db/migrations")

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repo = Path(directory.name)
        self.git("init", "-q", "--object-format=sha1")

    def git(self, *args):
        return subprocess.check_output(
            ["git", "-C", str(self.repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com",
             "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args], text=True,
        ).strip()

    def write(self, path, sql):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(sql, encoding="utf-8")

    def commit(self, files):
        for path, sql in files.items():
            self.write(path, sql)
        self.git("add", "--", *map(str, files))
        self.git("commit", "-qm", "migration fixture")
        return self.git("rev-parse", "HEAD")

    def check(self, base, head):
        return baseline.check(self.repo, base, head, public=False, migrations=True, dialect="postgres",
                              sqlite_dirs=(self.SQLITE_DIR,))

    def assert_refused(self, base, head, migration, clause, sql, reason):
        findings = self.check(base, head)
        prefix = f'"{migration}":{drop_line(sql, clause)}: {reason}'
        self.assertTrue(any(finding.startswith(prefix) for finding in findings), findings)

    def test_both_expands_use_committed_directory_history(self):
        self.commit({self.PG_DIR / "001_create.sql": PG_CREATE,
                     self.SQLITE_DIR / "001_create.sql": SQLITE_CREATE})
        base = self.commit({self.PG_DIR / "002_pin.sql": PG_PIN,
                            self.SQLITE_DIR / "002_pin.sql": SQLITE_PIN})
        head = self.commit({self.PG_DIR / "003_expand.sql": PG_EXPAND,
                            self.SQLITE_DIR / "003_expand.sql": SQLITE_EXPAND})
        self.write(self.PG_DIR / "002_pin.sql", PG_ADD_SCOPE)
        self.write(self.SQLITE_DIR / "003_expand.sql", "DROP TABLE entries;")
        self.assertEqual(self.check(base, head), [])

    def test_backdated_sqlite_rebuild_cannot_use_stale_shape(self):
        migration = self.SQLITE_DIR / "003_expand.sql"
        base = self.commit({self.SQLITE_DIR / "001_create.sql": SQLITE_CREATE,
                            self.SQLITE_DIR / "002_pin.sql": SQLITE_PIN,
                            self.SQLITE_DIR / "004_extra.sql": "ALTER TABLE entries ADD COLUMN saved TEXT;"})
        head = self.commit({migration: SQLITE_EXPAND})
        self.assert_refused(base, head, migration, "DROP TABLE", SQLITE_EXPAND, "DROP TABLE")

    def test_postgres_head_edit_cannot_forge_prior_pin(self):
        pin = self.PG_DIR / "002_pin.sql"
        migration = self.PG_DIR / "003_expand.sql"
        base = self.commit({self.PG_DIR / "001_create.sql": PG_CREATE, pin: PG_ADD_SCOPE})
        head = self.commit({pin: PG_PIN, migration: PG_EXPAND})
        self.assert_refused(base, head, migration, "DROP CONSTRAINT", PG_EXPAND, "ALTER TABLE DROP")

    def test_sqlite_uncommitted_schema_cannot_prove_rebuild(self):
        pin = self.SQLITE_DIR / "002_pin.sql"
        migration = self.SQLITE_DIR / "003_expand.sql"
        base = self.commit({self.SQLITE_DIR / "001_create.sql": SQLITE_CREATE})
        head = self.commit({migration: SQLITE_EXPAND})
        self.write(pin, SQLITE_PIN)
        for staged in (False, True):
            with self.subTest(staged=staged):
                if staged:
                    self.git("add", "--", str(pin))
                self.assert_refused(base, head, migration, "DROP TABLE", SQLITE_EXPAND, "DROP TABLE")


if __name__ == "__main__":
    unittest.main()
