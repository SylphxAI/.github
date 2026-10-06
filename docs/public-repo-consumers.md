# Public repository consumers

**Policy: we publish what we use ourselves.** Every public, unarchived
SylphxAI repository names its internal consumer in the organization custom
property `sylphx_consumer`: the repository path or desk configuration that
runs it (at most 75 characters, for example
`SylphxAI/infra infra/addons/capi-system`). A public repository with no
consumer is a tool nobody in the company uses; it is re-scoped, merged into
what we use, or archived.

This follows the TODO Group guides: share what we actually use in production
([Starting an open source project](https://todogroup.org/resources/guides/starting-an-open-source-project/)),
and shut a project down when we no longer use it in our own work
([Shutting down an open source project](https://todogroup.org/resources/guides/shutting-down-an-open-source-project/)).

## Scope

- Public, unarchived, not disabled repositories, forks included (a fork with
  no consumer is a leftover).
- Delivered customer projects (`sylphx_delivery` = `delivered`, see
  `scripts/is-delivered.sh`) are skipped: they are hands-off.

## The check

`scripts/public_repo_consumers.py` lists every repository in scope with no
`sylphx_consumer` value. It reads the organization's repository list once
(`gh api --paginate orgs/SylphxAI/repos`; each repository object carries its
custom properties).

| Exit | Meaning |
| --- | --- |
| 0 | every public repository records a consumer |
| 1 | at least one has none (`MISSING` lines, or `missing` with `--json`) |
| 2 | the list or the properties could not be read; a credential that sees no custom property on any repository counts as unreadable, never as green or as "all missing" |

```bash
scripts/public_repo_consumers.py            # text report
scripts/public_repo_consumers.py --json     # machine-readable
```

`.github/workflows/public-repo-consumers.yml` runs it every Monday at 03:17 UTC
(and on demand) with an org-wide GitHub App read token, writes the list to the
run summary and fails while any repository is missing a consumer. The internal
work tracker runs the same script weekly and files one keep, merge or archive
decision per listed repository.

## Recording a consumer

An organization admin sets the value in the repository's settings
(Settings > Custom properties) or through the API:

```bash
gh api -X PATCH orgs/SylphxAI/properties/values --input - <<'EOF'
{"repository_names":["<repo>"],"properties":[{"property_name":"sylphx_consumer","value":"<owner/repo path>"}]}
EOF
```

When a consumer stops using the repository, clear the value; the next weekly
run lists it.
