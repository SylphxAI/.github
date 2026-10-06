# Keel repin

A title that builds on Keel polls Keel's tags itself. When a new `keel-verified-*` tag is ahead of
its pin, it opens the pull request that moves the pin. No central bot, no cross-org App, and no
dependence on someone remembering.

## The flow

Each title carries one small workflow, `keel-repin.yml` (from `workflow-templates/keel-repin.yml`),
that runs every 10 minutes on our own runners (so a repin pull request exists within 15 minutes of a
tag) and calls the `keel-repin` action in three modes:

1. **poll** (a cheap job: checkout, read Keel's tags with the read-only reader App). A repin is
   needed when the newest `keel-verified-*` tag is not already the pin, Keel reports the tag
   **ahead** of the pin (never a downgrade), and no pull request from `chore/keel-repin-<tag>`
   exists in any state (a closed one is never reopened).
2. **repin** (only when needed): moves every pin, refreshes `Cargo.lock` for the Keel crates, runs
   the title's build check, pushes `chore/keel-repin-<tag>`, opens the pull request with the
   workflow token and the repository's single `owner:*` label, then starts the title's CI on that
   branch with `workflow_dispatch`.

Why the dispatch: a pull request opened with the workflow token gets only a `pull_request` run
that GitHub holds for a maintainer (conclusion `action_required`, a check suite with no check
runs; GitHub's behaviour since 2026-06-11), so nothing would check it unless someone clicks. The
dispatch needs no click and the held run is left alone: it posts no check run, so `settle` (which
reads check runs only) neither waits on it nor counts it red. The dispatched run's check runs attach to the branch head, which is the pull
request head, and a ruleset's required checks match by check name and the GitHub Actions app, not
by event, so `check` and `ci-ok` on that commit satisfy them. Every workflow named in
`ci-workflows` must list `workflow_dispatch`; the action waits for each run to appear on the head
before it starts the next, so an aggregate `ci-ok` workflow listed last sees the others.

3. **settle** (every run, whether or not a repin was needed): merges a repin pull request once it has
   earned it, and tells its owner when it cannot.

A build that breaks on the new tag (an API change, say) still opens the pull request, as a draft
with the last lines of the error; CI is not started for it, and it is never merged. The two common
lock-refresh breaks are handled: a crate the lock holds at two Keel commits (the title's pin and a
title kit's own pin) is updated by its full package id (`git+URL?rev=OLD#name@version`), so cargo
does not call it ambiguous and the kit's commit is left as it is; and when the build cannot fetch a
private repository the draft names it, because every private git dependency of every tracked
`Cargo.lock` (a sub-crate's own lock included) must be in `extra-read-repos`. The bot calls no
pull-request approval API. A branch that exists is left alone, so a repeat run never overwrites a fix
someone pushed to it.

## When the bot merges

`settle` looks at every open pull request from a `chore/keel-repin-*` branch and merges one only when
all of these hold, judged on the pull request's **exact head commit**:

- every check in `required-checks` (default `ci-ok web-smoke`) has run on that commit, finished, and
  succeeded. A check that has not appeared or not finished keeps the pull request waiting;
- no check at all on that commit failed, was cancelled or timed out, required or not;
- only runs of the GitHub Actions app count, so another App cannot post a passing check under the name;
- of several runs of one name (a re-run), the newest decides;
- the pull request is not a draft, and every commit on it is the bot's own. A person's push to the
  branch hands the merge back to a person;
- the merge call is pinned to the judged commit (`--match-head-commit`), so a push after the verdict
  voids it. There is no admin or bypass merge. In a repository with a merge queue the pull request is
  queued (`--auto`) and the queue's own gate still runs.

`web-smoke` is the title's own check that its packed web build boots in a browser to a drawn frame, with
the boot splash visible at once and no black screen. A title with no `web-smoke` check is never merged
by the bot: the pull request waits, then turns red (below) after `max-wait-minutes` (default 360). A
title with no web build lists only its aggregate check in `required-checks`.

**A red pull request** (a failed check, a check that never finished, a draft, a refused merge) is left
open and gets one comment naming the failing check and mentioning `owner`. The comment carries the head
commit, so the same condition seen on every run adds no second comment; a new head that is red again gets
a new one.

**After the merge.** A merge made with the workflow token starts no `push` workflow, so a title that
deploys on push lists its deploy workflow in `after-merge-workflows`; the bot starts it on the default
branch once it reads the pull request as merged.

## Permissions

The workflow declares `permissions: {}` at the top and per job:

| Job | Scopes |
| --- | --- |
| poll | `contents: read`, `pull-requests: read` |
| repin | `contents: write` (push the branch), `pull-requests: write` (open it), `actions: write` (dispatch CI) |
| settle | `contents: write` (merge), `pull-requests: write` (merge, comment), `checks: read` (read the head's checks), `actions: write` (after-merge dispatch) |

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
| `mode` | `poll`, `repin` or `settle`. |
| `tag` | A Keel tag, or `latest` (the newest `keel-verified-*`). |
| `dry-run` | Print the pull request it would open and the workflows it would start; push nothing. Rebuilds even if the branch exists. |
| `reader-app-id`, `reader-app-key` | A read-only App that can read the private Keel repository. |
| `extra-read-owner`, `extra-read-repos` | Private repositories of one owner that the lock refresh or the build fetches, such as a title kit; list every private git dependency of every tracked `Cargo.lock`. |
| `check-command`, `check-dir` | The build check. Default `cargo check`. |
| `ci-workflows` | Workflow files started on the branch, in order. Default `ci.yml`. The web smoke must be among the checks they produce. |
| `required-checks` | settle: checks that must have succeeded on the head. Default `ci-ok web-smoke`. |
| `owner` | settle: who a red comment mentions, such as `@Cubeage/studio`. |
| `max-wait-minutes` | settle: how long a required check may stay unfinished. Default 360. |
| `after-merge-workflows` | settle: workflows started on the default branch after the merge. |

## Tests

`python -m unittest tests.test_keel_repin`: pin discovery and rewrite, the lock refresh of a crate
held at two Keel commits (against real cargo when it is installed), the draft naming a private
repository the build could not read, poll (ahead, behind, pinned,
pull request exists), the build check, the draft pull request, supersession of older repin pull
requests, CI dispatch order, workflow files named and never edited, settle (merges only on every
required check green on the exact head and pinned to it; waits on a missing or unfinished check; red
on any failure, a stranger's commit or a draft means no merge; one comment per head; the newest run of
a name decides; another App's check does not count; the queue retry; the after-merge dispatch), no
approve call anywhere, the permission scopes, the 10-minute cadence, and no GitHub-hosted runner label. A consumer run:
`workflow_dispatch` the title's `keel-repin.yml` with `dry_run` true.
