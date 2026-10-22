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
| 1 | at least one has none (`MISSING` lines, or `missing` with `--json`), or a shut-down project does not say so (`NO-NOTICE` and `UNDEPRECATED` lines, `archived_without_notice` and `undeprecated_packages` with `--json`; see below) |
| 2 | the list or the properties could not be read; a credential that sees no custom property on any repository counts as unreadable, never as green or as "all missing" |

```bash
scripts/public_repo_consumers.py            # text report
scripts/public_repo_consumers.py --json     # machine-readable
```

`.github/workflows/public-repo-consumers.yml` runs it every Monday at 03:17 UTC
(and on demand) with the job's own read-only token (public repositories, their
custom properties and READMEs are public), the org npm token and the org crates.io
token (`CARGO_REGISTRY_TOKEN`, shared with this repository), deprecates the
listed npm packages and yanks the listed crates, writes the lists to the run summary and fails while
anything is still listed. The internal work tracker runs the same script weekly
and files one keep, merge or archive decision per listed repository.

## Shut-down projects say so

A project we stop is archived, and it tells its users: the TODO Group guide
asks to make the shutdown obvious and name an alternative, GitHub's archiving
guide asks to update the README and description, and each registry has a
marker for it. So every archived public repository (forks and delivered
customer projects excluded):

- carries a status line in its description, or near the top of its README
  (the first 4,000 characters): archived, deprecated, discontinued, retired,
  no longer maintained, superseded, merged into, moved to, replaced by, end of
  life or sunset; and
- has every package it published marked: deprecated on npm
  ([`npm deprecate`](https://docs.npmjs.com/deprecating-and-undeprecating-packages-or-package-versions)),
  discontinued on pub.dev ([package options](https://dart.dev/tools/pub/publishing#discontinue)),
  yanked on crates.io (its only registry-side marker).

The same script lists the exceptions after the consumer list:

| Line | Meaning | Fix |
| --- | --- | --- |
| `NO-NOTICE SylphxAI/<repo>` | archived with no status line | unarchive, prefix the description with `Archived: no longer maintained.` (and the alternative, if any), archive again; an archived repository is read-only |
| `UNDEPRECATED <registry> <package>` | a package whose source (`repository` URL, renamed owners resolved through GitHub's redirect) is an archived repository, still unmarked | npm: the weekly run deprecates it itself (`--fix-npm`, org `NPM_TOKEN`); crates.io: the weekly run yanks every version itself (`--fix-crates`, org `CARGO_REGISTRY_TOKEN`; `cargo yank --undo` reverses); pub.dev: a publisher admin marks it discontinued on the package's Admin tab (pub.dev accepts no token for package options) |

Packages are found from the npm organization `sylphx` and its maintainers'
packages, the pub.dev publisher `sylphx.com`, and the crates.io owner of the
SylphxAI crates; all three are public reads. A registry that cannot be read
makes the run exit 2, never green.

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
