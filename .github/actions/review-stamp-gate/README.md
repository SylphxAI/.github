# Review stamp gate

A merge-group-only composite action extracted from cloud#11840. It refuses a
queued pull request whose newest trusted `ops-security/review` commit status is
`failure`, `error`, or `pending`, even outside the configured scope. A success
passes; a missing status fails only on a scoped path or label when
`enforceMissing` is true (the default).

## Caller

Pin the shared action to a reviewed commit. Put this job in the `needs` of the
repository's required aggregate check. Skipping it outside `merge_group` is
expected; a failed gate must never be treated as skipped or advisory.

```yaml
review-stamp:
  if: github.event_name == 'merge_group'
  runs-on: sylphx-linux-standard
  permissions:
    contents: read
    pull-requests: read
  steps:
    - uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5
      with:
        fetch-depth: 0
    - uses: SylphxAI/.github/.github/actions/review-stamp-gate@<commit>
      with:
        config-path: .github/review-stamp.json
        trusted-creator-ids: '8020099'
```

Python 3 and git are the only runtime dependencies. The checkout retains its
read-only credentials to enumerate queue refs and fetch the target branch.
The token needs `contents: read` and `pull-requests: read`; it performs no
writes. The creator IDs are numeric GitHub IDs, comma-separated, supplied by
the caller, not trusted from the JSON scope.

## Repository-owned scope

```json
{
  "context": "ops-security/review",
  "enforceMissing": false,
  "requireStampLabels": ["security", "area:security"],
  "requireStampPaths": [".github/workflows/", ".github/actions/", ".github/review-stamp.json", "auth/", "billing/"]
}
```

Directory paths end in `/`; other paths match exactly. Repositories without an
assigned stamping practice use `enforceMissing: false`: explicit trusted
non-success statuses still block, but unstamped PRs pass until the stamping
owner is decided. Turning on missing-stamp enforcement is a repository policy
change, not a code fork.

The action reads scope from the merge group's base commit so a PR cannot
loosen its own policy. Only first adoption, when the base has no scope file,
reads the queued copy. It checks every entry carried by the group, including
earlier entries in a multi-PR group; unrecognized commits fail closed. It
paginates statuses so unrelated contexts cannot bury the trusted stamp.

The workflow and creator input still come from the queued commit. A workflow
that removes this gate needs review; a pinned required workflow is the eventual
hardening boundary. Cloud#11840 retains its local implementation until its
owner deliberately adopts the shared action.
