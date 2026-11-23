# Shared baseline check

Use this check for new public-source disclosures and unphased destructive SQL.
It reads Git objects, not the checked-out project's scripts. The public check
reports the category and location without repeating the matched value. It is
not a substitute for secret scanning.

Add the job to the existing PR and merge-group code workflow. If the workflow
already calls the shared path selector, update its pin and reuse its migration
output. Here both jobs use the same published commit:

```yaml
jobs:
  changes:
    uses: SylphxAI/.github/.github/workflows/changes.yml@<commit-sha> # main
    with:
      runner: ubuntu-latest # accepted only for public callers
  shared-baseline:
    needs: changes
    if: github.event.repository.visibility == 'public' || needs.changes.outputs.migrations != 'false'
    uses: SylphxAI/.github/.github/workflows/shared-baseline-check.yml@<commit-sha> # main
    with:
      migrations: ${{ needs.changes.outputs.migrations }}
```

Add `shared-baseline` to the existing `ci-ok` dependency list. The baseline
uses the standard GitHub-hosted `ubuntu-latest` runner for public repositories
and `sylphx-linux-control` otherwise. Private and internal repositories do
not run the public-source check. If the selector proves there are no changed
migration SQL files, the migration check does not run. In a nonpublic
repository this skips the job before runner allocation. If you do not use the
selector, omit `needs`, `if` and `with`: the checker reads the Git comparison
and loads the SQL parser only when an applicable file changed. An unreadable
comparison fails the job. Manual and scheduled runs without an explicit base
compare the checked-out commit with its first parent. If the parent cannot be
read, the check fails. Manual runs can pass `code-base` to select a different
base. Push, pull request, and merge queue runs still require a comparison SHA.

SQL files under a `migrations*` directory are checked for newly introduced or
changed table and column drops. PostgreSQL is the default; set
`migration-dialect: mysql` for unsuffixed MySQL files. The `.mysql.sql` and
`.postgresql.sql` suffixes always take precedence. To use SQLite for unsuffixed
files in selected directories, add `sqlite-migration-dirs` to the caller's `with`:

```yaml
sqlite-migration-dirs: |
  crates/example/migrations
  apps/example/migrations
```

Use one literal repository-relative directory per line, without globs. Only
migration SQL in those directories and their subdirectories uses SQLite rules;
similarly named sibling directories keep the default dialect.

Deploy the expand release first, and reference its release note immediately
above the drop:

```sql
-- expand-contract: drop-phase https://example.com/releases/v2
ALTER TABLE accounts DROP COLUMN obsolete;
```

The existing `-- contract: <released expand reference>` is also accepted.
The comment applies to the next statement only. The check requires a reference;
it cannot attest that the referenced release was actually deployed. Opaque SQL
that could hide a drop requires the same explicit reference. Use the complete
[migration guard](migration-guard.md) where the repository also needs immutable
migrations, schema snapshots, or other runtime-specific safeguards. Remove local
scripts only if their behavior is entirely covered by the replacement.
