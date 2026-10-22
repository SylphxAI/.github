# Identifiers

Fails a pull request, merge group or push that adds an id generator, or a
primary key, that is not a UUIDv7. Every persisted entity id is a UUIDv7 in a
Postgres `uuid` column, minted by `uuidv7()` or the SDK `ids` helper: no UUIDv4,
ULID, nanoid, CUID, KSUID, Snowflake, or text primary key (owner
`standards/identifiers.md`). [`rules.tsv`](rules.tsv) is the list the check
enforces.

```yaml
jobs:
  identifiers:
    runs-on: sylphx-linux-standard
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@<sha>
        with:
          fetch-depth: 0
      - uses: SylphxAI/.github/.github/actions/identifiers@<sha>
```

Only added lines are checked, so a repository adopts it without first migrating
the ids it already ships. A line that must keep an older scheme carries
`identifiers: allow <reason>`. Set `mode: warn` to annotate without failing, and
`exclude` to skip pathspecs such as recorded evidence.

Historical applied migrations must remain byte-identical. The pinned action alone
owns [`historical-allowances.json`](historical-allowances.json): each allowance
matches all four of the calling repository (from GitHub context), exact file
path, stable finding code (the fourth column of `rules.tsv`, also printed in
findings), and SHA256 of the complete file at the checked diff endpoint. It
suppresses only that matching finding, not a path or other findings; changed
bytes, another repository/path, and new migrations still fail. Allowances are
reviewed changes in this checker repository, never caller files, action inputs,
environment-supplied lists, or PR payloads, for pull requests, merge groups and
pushes alike; no inline waiver or warning downgrade is needed.
