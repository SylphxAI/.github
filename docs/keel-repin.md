# Keel repin

A title that builds on Keel polls Keel's tags itself. When a new `keel-verified-*` tag is ahead of
its pin, it opens the pull request that moves the pin. No central bot, no cross-org App, and no
dependence on someone remembering.

## The flow

Each title carries one small workflow, `keel-repin.yml` (from `workflow-templates/keel-repin.yml`),
that runs every 30 minutes on our own runners and calls the `keel-repin` action twice:

1. **poll** (a cheap job: checkout, read Keel's tags with the read-only reader App). A repin is
   needed when the newest `keel-verified-*` tag is not already the pin, Keel reports the tag
   **ahead** of the pin (never a downgrade), and no pull request from `chore/keel-repin-<tag>`
   exists in any state (a closed one is never reopened).
2. **repin** (only when needed): moves every pin, refreshes `Cargo.lock` for the Keel crates, runs
   the title's build check, pushes `chore/keel-repin-<tag>`, opens the pull request with the
   workflow token and the repository's single `owner:*` label, then starts the title's CI on that
   branch with `workflow_dispatch`.

Why the dispatch: a pull request opened with the workflow token starts no `pull_request` run, so
nothing would check it. The dispatched run's check runs attach to the branch head, which is the pull
request head, and a ruleset's required checks match by check name and the GitHub Actions app, not
by event, so `check` and `ci-ok` on that commit satisfy them. Every workflow named in
`ci-workflows` must list `workflow_dispatch`; the action waits for each run to appear on the head
before it starts the next, so an aggregate `ci-ok` workflow listed last sees the others.

A build that breaks on the new tag (an API change, say) still opens the pull request, as a draft
with the last lines of the error; CI is not started for it. The bot never merges, never enables
auto-merge and calls no pull-request approval or review API. A branch that exists is left alone, so
a repeat run never overwrites a fix someone pushed to it.

## Permissions

The workflow declares `permissions: {}` at the top and per job:

| Job | Scopes |
| --- | --- |
| poll | `contents: read`, `pull-requests: read` |
| repin | `contents: write` (push the branch), `pull-requests: write` (open it), `actions: write` (dispatch CI) |

The workflow token cannot edit files under `.github/workflows/`; the action never touches them and
names, in the pull request, any workflow file that still mentions the old pin.

**Repository setting.** The token can only open a pull request where
Settings > Actions > General > Workflow permissions > "Allow GitHub Actions to create and approve
pull requests" is on. (Only creation is used; the bot never approves.) Where the organization has
it off, a repository cannot turn it on by itself: turn it on for the organization first
(Organization settings > Actions > General, or
`PUT /orgs/{org}/actions/permissions/workflow` with `can_approve_pull_request_reviews: true`), then
a repository may turn it off again if it does not use the bot.

## What counts as a pin

- A `rev = "<40 hex>"` or `tag = "keel-..."` on a `Cargo.toml` line that names the Keel git
  repository, and every `deps/keel.rev`.
- The old full commit, and its 12, 8 and 7 character prefixes, in any other tracked text file
  (Dockerfiles, cargo config, CI scripts, docs). `vendor/`, `target/`, `Cargo.lock`,
  `CHANGELOG.md` and `.github/workflows/` are skipped.
- A `# Keel tag: <tag>` comment in a `Cargo.toml` moves with the pin.

A title that needs more (re-vendored sources, a patch block) keeps `tools/repin_keel.sh <tag>`.
When that file exists the action runs it instead of the rewrite and the lock refresh, then runs the
check; edits it makes under `.github/workflows/` are discarded.

## Inputs worth knowing

| Input | Meaning |
| --- | --- |
| `mode` | `poll` or `repin`. |
| `tag` | A Keel tag, or `latest` (the newest `keel-verified-*`). |
| `dry-run` | Print the pull request it would open and the workflows it would start; push nothing. Rebuilds even if the branch exists. |
| `reader-app-id`, `reader-app-key` | A read-only App that can read the private Keel repository. |
| `extra-read-owner`, `extra-read-repos` | A second private source the build fetches, such as a title kit. |
| `check-command`, `check-dir` | The build check. Default `cargo check`. |
| `ci-workflows` | Workflow files started on the branch, in order. Default `ci.yml`. |

## Tests

`python -m unittest tests.test_keel_repin`: pin discovery and rewrite, poll (ahead, behind, pinned,
pull request exists), the build check, the draft pull request, supersession of older repin pull
requests, CI dispatch order, workflow files named and never edited, no approve, review or merge
call anywhere, the permission scopes, and no GitHub-hosted runner label. A consumer run:
`workflow_dispatch` the title's `keel-repin.yml` with `dry_run` true.
