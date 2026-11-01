# Migration safety

The `migration-guard` action reads SQL from Git objects without connecting to a
database. It supports PostgreSQL and MySQL. Configure migration directories,
schema snapshot files, or both. At least one scope is required.

Migration directories are inspected recursively, including hidden directories.
A configured directory may be absent until its first migration.

```yaml
on:
  pull_request:
  merge_group:
  push:
    branches: [main]

permissions:
  contents: read

jobs:
  migration-guard:
    runs-on: sylphx-linux-standard
    steps:
      - uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5 # v4
        with:
          fetch-depth: 0
          persist-credentials: false
      # Pin the shared action to a reviewed commit SHA with a `# main` marker.
      - uses: SylphxAI/.github/.github/actions/migration-guard@<commit-sha> # main
        with:
          dialect: postgres
          migration-directories: |
            server/.drizzle
            packages/db/migrations
```

Require this job in the repository's merge rules, or make the existing required
`ci-ok` verdict depend on its success. Run it for every PR and merge group,
including changes that only touch CI. A path-filtered workflow cannot enforce
an always-required check reliably.

The comparison base and head come from the PR, merge group, or push event.
`base-ref` and `head-ref` can override them for a manual run. Fetch the complete
history. Missing refs, missing configuration and unreadable SQL fail the check.
PRs behind main compare against their common ancestor.

Merged SQL files cannot be changed, deleted or renamed, even with a contract
marker. Append a new migration. Numeric `NNNN_name.sql` versions must be unique
within their directory and greater than the highest version on the base.

New migrations are refused for:

- `DROP TABLE`, `DROP COLUMN` (including the short `ALTER TABLE t DROP c`),
  `DROP SCHEMA`, `DROP DATABASE`, `DROP OWNED`, `DROP TYPE`, `DROP DOMAIN`, and
  `TRUNCATE`. Temporary tables and foreign-table column changes are included.
- `DELETE` or `UPDATE` without a `WHERE` in that mutation's own scope.
- New foreign-key `ON DELETE CASCADE` or `ON DELETE SET NULL` actions, which
  allow a future parent deletion to change child data. `RESTRICT`, `NO ACTION`,
  and references without delete actions pass.
- Renames, PostgreSQL column type changes, and MySQL `MODIFY`/`CHANGE` columns.
  Type changes are conservative: without the old definition, widening cannot
  be established, so all require the contract marker.
- Adding `NOT NULL` columns to existing tables without a default, or setting
  `NOT NULL` without an earlier default for that same table and column in the
  migration. Generated, identity, serial and auto-increment columns count as
  defaults. New tables can declare required columns directly.
- MySQL `DISCARD TABLESPACE` and character-set conversions.
- Opaque dynamic SQL, such as `CALL`, `EXECUTE`, and `PREPARE`, and trigger or
  scheduled-event definitions whose effects cannot be established locally.
- `SET` changes to `sql_mode` or `standard_conforming_strings`, including quoted
  setting names. Changing quote rules can hide later SQL from inspection.

Comments and string literals cannot manufacture SQL keywords or a contract
marker. PostgreSQL dollar strings/nested comments and MySQL executable version
comments are handled explicitly. This is a conservative SQL policy check, not a
database planner or proof that a `WHERE` clause preserves all intended records.

PostgreSQL SQL and PL/pgSQL functions, procedures, and `DO` blocks are inspected
recursively. Dollar-quoted and ordinary single-quoted bodies use the same SQL
rules as the surrounding migration, including SQL inside control blocks.
Additive report functions and create/grant blocks pass without a marker;
destructive bodies report the underlying operation. Unquoted inline bodies,
including MySQL routine definitions, other procedural languages, escape-string
bodies, and opaque dynamic execution require a contract reference. A marker inside a body string
does not authorize the surrounding migration.
One PostgreSQL dynamic ACL form is inspected: `EXECUTE pg_catalog.format(...)`
with a constant template for `GRANT`/`REVOKE USAGE ON SCHEMA`
or `GRANT`/`REVOKE EXECUTE ON FUNCTION schema.name(types)`. The only placeholder
is one final `%I` role name, supplied by a simple variable or record field.
Bare `format` is accepted only inside a routine with a fixed local
`SET search_path = pg_catalog` (optionally followed by `pg_temp`). An absent or
user-schema path, a body that changes or resets that path, calls a function
outside `pg_catalog` by a qualified name, or executes nested `DO` does not
establish builtin formatter identity. Formatter redefinitions and later
routine search-path changes require a contract reference. The template prefix is parsed separately from
the final placeholder, so a literal grantee cannot impersonate it.
Identifier quoting prevents role names from adding SQL syntax. Other privilege
scopes, grant options, SQL interpolation (`%s`/`%L`), concatenated templates,
extra commands and other assembled queries remain opaque. ACL inspection does
not prove that a granted role should have access; the repository owns that policy.

`RETURN QUERY EXECUTE local_text [USING parameter, ...]` is also inspected in a
routine with that fixed catalog-only search path when its complete body fits a
closed literal construction: local `text` declarations, optional literal defaults,
plain/dollar-quoted strings, `||`, and `IF`/`ELSIF`/`ELSE` branches. Every branch is
explored, including the unchanged path of an `IF` without `ELSE`. Each local may
be assigned once per path after its declaration default; every value reaching an
execution must be known. Plain `RETURN` and literal `RAISE EXCEPTION` end a path.
Conditions admit builtin `isfinite`/`to_regclass` calls and ordinary comparisons,
not unknown function calls. `USING` binds simple parameter names, never query text.

Every generated query must be one `SELECT` or `WITH ... SELECT`, pass the existing
recursive checks, and contain no data-changing CTE, `RETURNING`, `INTO`, locking
clause, or extra statement. Calls are limited to `min`, `max`, `count`, `sum`,
`avg`, `bool_and`, `bool_or`, `coalesce`, `nullif`, `greatest`, `least`, `split_part`,
`to_timestamp`, and `extract`, bare or qualified with `pg_catalog`; explicit casts
and typed literals use builtin types. Unknown inputs, reassignment, SQL `INTO`,
loops, exception handlers, nested scopes, formatter interpolation, and unsupported
siblings invalidate the complete construction proof. The limits are 64 live paths,
64 distinct generated queries, 32 nested conditions, and 1 MB of generated text;
exceeding a limit keeps the execution opaque. Crediting one execution does not
skip inspection of sibling statements or surrounding migrations. Relation
implementations, column/operator types, existing ACL continuity, and application
access policy still require review; this syntax check is not a database planner.
Simple PostgreSQL `BEFORE INSERT` and `AFTER INSERT` row triggers pass when the
target table was created unconditionally earlier in the same migration and the
zero-argument trigger function was defined and inspected there too. Both table
and function names must include an explicit schema. This supports additive ID
assignment triggers without trusting search-path resolution or an external
function. Triggers on existing tables, other events, trigger arguments, and
unresolved or opaque functions still require a contract reference.
In bodies with conditional, loop, exception, nested `DO`, or rollback paths,
earlier statements do not establish that a table was created or a default was
set. Required-column changes must carry their default in the same SQL statement
or have a contract reference.

After the expand step has shipped and old readers/writers have stopped using
the old contract, a **new** migration may name that released step on its own line:

```sql
-- contract: https://example.com/releases/expand-42
ALTER TABLE accounts DROP COLUMN old_region;
```

The reference must be nonempty and contain a word character. The check preserves
the existing expand/contract marker convention; reviewers verify that the named
expand step was actually released. It never exempts edits to merged migrations
or invalid ordering. Existing historical SQL is not retroactively linted.

## Concurrent PostgreSQL indexes

New `CREATE INDEX CONCURRENTLY` statements require an explicitly adopted
runtime contract. The default refuses them. A preceding `SET`, a preceding
`DROP`, `IF NOT EXISTS`, or a released-expand marker does not establish safe
Atlas partial resume. Unchanged historical files are preserved without
retroactive linting.

A caller with a qualified Atlas runner sets
`concurrent-index-runtime: atlas-qualified-v1`. That runner must validate the
immutable directory and exact revision/hash/failed statement, bound every fresh
Atlas and cleanup connection, and remove only the matching interrupted invalid,
nonunique, unconstrained index with no active operation. Atlas alone writes its
revision history. Attempts, backoff, total duration and child lifetime are
bounded. A valid index with a pending revision is refused and preserved.
The input declares adoption; runtime regressions in the caller prove it.

Each newly supported file starts with `-- atlas:txmode none` followed by a
blank line. Atlas v1.2.0 recognizes global file directives only when detached
from statement comments by that separator; without it the CREATE runs in a
transaction and fails with `25001`. The guard refuses that malformed header.
The remaining content contains one declaration and exactly its generated SQL:

```sql
-- atlas:txmode none

-- migration:concurrent-index-v1 {"schema":"public","table":"events","index":"events_owner_idx","columns":["owner_id"],"where_null":"deleted_at","lock_timeout_ms":3000,"statement_timeout_ms":240000}
CREATE INDEX CONCURRENTLY "events_owner_idx" ON "public"."events" USING btree ("owner_id") WHERE "deleted_at" IS NULL;
```

The declaration supports explicit schema/table/index identifiers, a nonempty
list of plain columns, and an optional `where_null` column. Identifiers are
double-quoted with embedded quotes doubled. Limits are positive integer
milliseconds: lock at most 3000, statement at most 240000, and lock below
statement. Tighter existing connection caps remain effective. The runtime
applies these through connection startup options, because Atlas skips earlier
successful statements on resume.

The rule refuses extra SQL, UNIQUE, IF NOT EXISTS, expressions, unsupported
predicates, additional declaration fields and missing/invalid bounds. More SQL
forms need a reviewed runtime and guard contract; a comment cannot authorize
them. The guard reads only newly added SQL from the actual configured Git range;
it does not inspect production state or repair an index.

## Schema snapshots

For a repository without a migration chain, configure complete `CREATE TABLE`
definitions with `schema-files`. Use this input on its own or alongside
`migration-directories`:

```yaml
with:
  dialect: mysql
  schema-files: |
    db/schema.sql
```

Each named file must exist at the head revision and be a regular file. A new
snapshot may have no base file. Deleting a configured snapshot fails the check.
The CLI equivalent is `--schema-file db/schema.sql`; repeat the flag for more
files. The action passes the input through `MIGRATION_SCHEMA_FILES`.

The check compares each base/head table and column declaration. It refuses
removed or renamed tables and columns, changed column types, and new or
tightened `NOT NULL` columns without a default. Primary keys count as required
columns. Type comparison includes enum values and remains conservative: even a
widening requires a contract reference. Table storage and collation options
also require a reference when changed. MySQL table `AUTO_INCREMENT` counters
and table comments are ignored.

New `ON DELETE CASCADE` and `ON DELETE SET NULL` actions require a reference
in snapshots too, including actions on newly declared tables. Existing
unchanged actions are preserved; adding a nullable column beside one passes.

Existing dump statements such as `DROP TABLE IF EXISTS` and versioned `SET`
comments are compared with the base rather than treated as new migrations.
New or changed SQL outside table declarations goes through the same statement
rules as new migrations. Literal changes count, so editing only the value of
an old unbounded `UPDATE` still triggers inspection. A newly added dump `DROP`
is checked too; omit it when adding a table unless a released expand step
authorizes it. Moving a `DROP TABLE` after its table declaration fails.

A snapshot contraction needs a **new reference in that changed file**:

```sql
-- contract: https://example.com/releases/expand-43
CREATE TABLE accounts (id bigint NOT NULL, region text);
```

A reference already present at the base cannot exempt later edits. A reference
in one snapshot cannot authorize another snapshot. Reviewers still verify the
released expand step; markers never bypass unreadable or unsupported syntax.

Snapshots must contain explicit column definitions in `CREATE TABLE`.
`CREATE TABLE AS`, `LIKE`, inherited columns, duplicate definitions, and
`ALTER TABLE` are refused: update the complete declaration instead, or put
incremental SQL in a migration directory. Quote modes that change the lexer's
interpretation, including MySQL `ANSI_QUOTES` / `NO_BACKSLASH_ESCAPES` and
PostgreSQL `standard_conforming_strings=off`, are unsupported in snapshots.
This mode compares declarations; it does not execute a dump or plan a database
migration, and it does not prove that scoped data updates preserve every row.
