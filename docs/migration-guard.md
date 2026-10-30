# Migration safety

The `migration-guard` action reads SQL from Git objects without connecting to a
database. It supports PostgreSQL and MySQL. Configure every directory that holds
deployable SQL migrations; files are inspected recursively, including hidden
directories. A configured directory may be absent until its first migration.

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
  `DROP SCHEMA`, `DROP DATABASE`, and `TRUNCATE`.
- `DELETE` or `UPDATE` without a `WHERE` in that mutation's own scope.
- Renames, PostgreSQL column type changes, and MySQL `MODIFY`/`CHANGE` columns.
  Type changes are conservative: without the old definition, widening cannot
  be established, so all require the contract marker.
- Adding `NOT NULL` columns to existing tables without a default, or setting
  `NOT NULL` without an earlier default for that same table and column in the
  migration. Generated, identity, serial and auto-increment columns count as
  defaults. New tables can declare required columns directly.
- Opaque procedural or dynamic SQL, such as `DO`, `CALL`, `EXECUTE`, or function
  definitions, whose effects cannot be proved by statement inspection.

Comments and string literals cannot manufacture SQL keywords or a contract
marker. PostgreSQL dollar strings/nested comments and MySQL executable version
comments are handled explicitly. This is a conservative SQL policy check, not a
database planner or proof that a `WHERE` clause preserves all intended records.

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
