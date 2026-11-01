# Keel repin

A title that builds on Keel polls Keel's tags itself. When a new `keel-verified-*` tag is ahead of
its pin, it opens the pull request that moves the pin. No central bot, no cross-org App, and no
dependence on someone remembering.

## The flow

Each title carries one small workflow, `keel-repin.yml` (from `workflow-templates/keel-repin.yml`),
that runs every 10 minutes on our own runners (so a repin pull request exists within 15 minutes of a
tag) and calls the `keel-repin` action in three modes:

1. **poll** reads the newest `keel-verified-*` tag. It waits until that release's
   `channel.json` maps the exact tag and peeled source commit to a version and both
   `sylphx-keel-engine` and `sylphx-keel-cli` exist, non-yanked, on crates.io. A tag alone
   does not start migration. A repository with public facade dependencies needs no Git
   Keel pin. The bot retains its read-only App for the private tag/release metadata;
   installing and compiling public packages needs no Keel reader. Provenance pins provide ancestry
   checks; registry-only consumers compare exact versions to avoid a downgrade.
   An existing branch or open/merged pull request for the tag stops another submission.
2. **repin** reads the same publication mapping before changing source. It installs
   `cargo install sylphx-keel-cli --version =VERSION --locked` into an isolated root,
   checks that executable's version, then runs `keel migrate --registry VERSION` and
   `keel migrate --check --registry VERSION` in a temporary tree of active Rust sources,
   Cargo manifests and `keel.toml` files. Successful source changes and generated Rust
   files are copied back; evidence and receipt paths are never supplied to the migrator. There is no
   version calculation from a source checkout, CLI from PATH, or private engine source checkout.
   The published migrator owns manifest and Rust API migration, including the single
   facade contract and target/feature activation; the action does not synthesize imports.
   Existing repository hooks run first for additional title assets, then the public
   migration applies to their output too.

   The action resolves each tracked Cargo lock without compiling, refusing any remaining
   Git Keel source, non-public Keel package, or different Keel version. It updates source
   provenance and inserts `ARG SYLPHX_GIT_COMMIT_SHA` immediately after `FROM` in each
   Docker stage that invokes `keel pack`, directly or through tracked scripts. A global
   ARG does not enter a stage, so it does not replace the stage declaration. Existing
   stage declarations remain unchanged, and server/runtime stages are not selected by
   comments about a pack. This travels with the dependency bump before the source-aware
   packer is used.

   Migration, lock and build failures remain visible and never qualify for merging.
   A failure before any source change fails without trying to create an empty pull
   request. Changed source with a failed preflight opens a draft naming the diagnostic.
   The action pushes `chore/keel-repin-<tag>`, opens the pull request, then starts its CI
   with `workflow_dispatch`.

Why the dispatch: a pull request opened with the workflow token gets only a `pull_request` run
that GitHub holds for a maintainer (conclusion `action_required`, a check suite with no check
runs; GitHub's behaviour since 2026-06-11), so nothing would check it unless someone clicks. The
dispatch needs no click and the held run is left alone: it posts no check run, so `settle` (which
reads check runs only) neither waits on it nor counts it red. The dispatched run's check runs attach to the branch head, which is the pull
request head, and a ruleset's required checks match by check name and the GitHub Actions app, not
by event, so `check` and `ci-ok` on that commit satisfy them. Every workflow named in
`ci-workflows` must list `workflow_dispatch`; the action waits for each run to appear on the head
before it starts the next, so an aggregate `ci-ok` workflow listed last sees the others.

When adopting separate PR metadata validation, install its dispatchable
`pr-metadata.yml`, list it before the code workflow in `ci-workflows`, and
include `pr-metadata` in `required-checks`. A code-only dispatch cannot satisfy
the separate required metadata check. The shared starter includes both.

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

`settle` looks at every open pull request from a `chore/keel-repin-<tag>` branch (or a
`chore/keel-repin-<layer>-<sha>` branch for the kit and engine layers) and merges one only when
all of these hold, judged on the pull request's **exact head commit**. A branch under the prefix that
is neither a tag nor a layer commit (a hand-made `chore/keel-repin-smoke-checks-*`, say) is named and
skipped, so it neither settles nor stops the repin beside it:

- every check in `required-checks` (default `ci-ok web-smoke`) has run on that commit, finished, and
  succeeded. A check that has not appeared or not finished keeps the pull request waiting; a run that
  ended `skipped` counts as absent, because GitHub reports a job skipped by its own `if:` that way and a
  skipped required check has verified nothing;
- no check at all on that commit failed, was cancelled or timed out, required or not;
- only runs of the GitHub Actions app count, so another App cannot post a passing check under the name;
- of several runs of one name (a re-run), the newest decides;
- the pull request is not a draft, and every commit after the bot's own on it is the bot's own. The pull
  request's commit list is read once with the head it already fetched; a commit from anyone else is a
  person's push to the branch, and it hands the merge back to a person;
- the merge call is pinned to the judged commit (`--match-head-commit`), so a push after the verdict
  voids it. There is no admin or bypass merge. In a repository with a merge queue the pull request is
  queued (`--auto`) and the queue's own gate still runs.

`web-smoke` is the title's own check that its packed web build boots in a browser to a drawn frame, with
the boot splash visible at once and no black screen. A title with no `web-smoke` check is never merged
by the bot: the pull request waits (told as a stall, below), then turns red after `max-wait-minutes`
(default 360). A title with no web build lists only its aggregate check in `required-checks`.

**A newer repin closes the older one.** When the newest `keel-verified-*` tag is ahead of the pin, the
title's poll opens the repin for that tag (`workflow-templates/keel-repin.yml` has no `tag` input set, so
`poll` always resolves the newest verified tag). `settle` then closes an older open repin pull request on
the same repository with one comment naming the newer one, so only the newest repin stays live and a title
never carries two red repin pull requests. The older one's branch is kept, so closing and deleting the
branch is what has the next poll rebuild that tag.

**A stalled check is waiting, not red.** An unfinished check whose runner has not picked the job up is the
queue, not the change: nothing about the pull request is wrong. `settle` treats a pending verdict whose
oldest unfinished check has waited `STALL_MINUTES` (20) as stalled, leaves the pull request open, and adds
one comment saying the runner has not picked the job up. The next scheduled run reads the same head and
merges it as soon as the checks are green.

**A red pull request** (a failed check, a required check that never appeared, a draft, a refused merge) is
left open and gets one comment naming the failing check and mentioning `owner`. The comment is keyed by
the head commit and a one-word kind (`red-`, `stalled-`, `foreign-`, `draft-`, `refused-`), so the same
condition seen on every run adds no second comment, a head whose reason changes is told once for the new
kind, and a new head that is red again gets a new one.

**After the merge.** A merge made with the workflow token starts no `push` workflow, so a title that
deploys on push lists its deploy workflow in `after-merge-workflows`; the bot starts it on the default
branch once it reads the pull request as merged.

**Settled means live.** A merge is not the end: the repin is settled only when the deployed title runs
the merged Keel. With `live-url` set, every `settle` run reads `<live-url>/VERSION.json` (Keel's packer
writes `keel`, the full commit the pack was built with) and compares it with the commit the newest
merged repin pull request names in its body. Equal: settled. Missing (no `keel` field, `unknown`, not
JSON, or the host unreachable) or a different commit: once `live-grace-minutes` (default 60) have
passed since the merge, the run fails and that pull request gets one comment mentioning `owner`; the
comment carries the live value, so the same wrong value on every run adds no second comment, and a new
wrong value gets a new one. Within the grace the deploy is still expected and the run passes with a
notice. A title with no merged repin pull request is not compared.

## Where the bot does not merge on its own

The action implements one rule for every title; a title that must not have its pin merged by a machine
turns off its own `settle` job in its `keel-repin.yml`, rather than the action carrying a list of
exceptions. Two kinds of title do that today:

- **A title with real players.** The Fun titles (瘋系列), HK Mahjong Tycoon and Big2 Tycoon HK have real
  users, and a change that reaches one takes one independent review and a guest-continuity check before
  it ships ([SylphxAI/owner](https://github.com/SylphxAI/owner) `AGENTS.md`, "Real users"). `Cubeage/
  fun-big2-keel`, `fun-mahjong-keel`, `block-sort-keel`, `godsmith-keel` and `planet-post-keel` therefore
  carry `if: ${{ vars.KEEL_REPIN_AUTOMERGE == 'true' }}` on `settle`, with the guest-continuity line in
  their comment; the bot still polls, opens the pull request and starts CI, and a person merges it. The
  `KEEL_REPIN_AUTOMERGE` repository variable is unset in each of them, so `settle` is skipped.
- **A delivered customer repository.** No work goes into one unless the customer asks, so a repository
  whose organization custom property `sylphx_delivery` is `delivered` must not carry `keel-repin.yml` at
  all. The property is an organization setting an action cannot read at run time; it is enforced where a
  title is registered (the workflow file is added), and the settle job's `if:` is the switch if one is
  ever registered. No `delivered` repository carries the workflow today.

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

- The exact crates.io `sylphx-keel-engine` version in a dependency, renamed dependency,
  or workspace dependency. The matching public migrator upgrades it.
- Legacy Git Keel revisions/tags, `deps/keel.rev`, and `KEEL_PIN`. Revision files record
  the source commit after migration; they are not a second implementation dependency.
- `deps/keel.version` records the same published version used by the facade and CLI.
- SHA and version text rewrites select only dependency rows in Cargo manifests and
  declared provenance files (`deps/keel.rev`, `deps/keel.version`, `KEEL_PIN`, or the
  corresponding kit/engine revision files). Cargo resolves lockfiles separately.
  Old full or abbreviated SHAs in other source, documentation or receipts stay unchanged.
- Evidence and receipt path components, including dated receipts and `docs/parity`,
  are excluded from pin discovery, rewriting, migration and lock refresh. Build output
  and vendored sources are excluded too. Docker pack-stage ARG insertion remains a
  separate adoption step and also excludes historical evidence.

The token never edits workflow files and lists any that still name an old revision.
A `tools/repin_keel.sh <tag>` hook may prepare title assets; it does not replace the
public facade migration. Shared-layer tuple hooks keep their existing arguments.

## Shared kit and engine layers

The same action follows shared-layer main commits with `layer: kit` (Cubeage/cubeage-kit)
or `layer: engine` (Cubeage/tycoon-engine). Keep `layer` identical in the poll and
repin calls, and pass the poll's `tag` output into repin: it is the full main SHA,
so the build uses the commit that was polled even if main moves meanwhile.
The default `layer: keel` uses the public verified-tag migration above.

Poll fetches the source commits and requires main to be ahead of every consumer pin.
Each target has one branch, `chore/keel-repin-kit-<sha>` or
`chore/keel-repin-engine-<sha>`; an existing branch or open/merged PR stops a repeat.
A newer layer PR closes older PRs of the same layer only after checking ancestry;
kit repins never close engine or Keel-tag repins.
The reader App must also be installed on the selected Cubeage source repository;
the action mints a read-only source token for both poll and repin. Other private
build dependencies still belong in `extra-read-repos`.

- **Kit:** reads the Keel revision from the target kit commit, then moves the kit
  and that Keel revision together. With `tools/repin_keel.sh`, invokes the existing
  tuple contract, `tools/repin_keel.sh <keel-sha> <kit-sha>`. Otherwise rewrites the
  tracked Cargo rev rows, `deps/kit.rev`, Keel pins and their tracked references,
  and refreshes the layer and Keel packages in each non-vendored `Cargo.lock`.
  A registry-based kit supplies its exact public facade version and source provenance;
  the matching public CLI migrates the title before the tuple lock resolution. Legacy
  Git kit sources retain their tuple hook behavior. When present,
  `scripts/vendor-private.sh` runs after that default tuple rewrite
  and lock refresh. A lock containing a different or second Keel source makes
  the PR a failed draft.
- **Engine:** rewrites Cargo rev rows and `ENGINE_REV`, then invokes
  `tools/vendor_engine.sh` or `scripts/vendor-private.sh` with no arguments, after
  the pins move. `ENGINE_REPO` points at a temporary checkout of the target engine
  for hooks that export from a local clone. Cargo git locks are refreshed too.
  The hook owns the vendored tree; no generic vendor exporter is added.

A failed hook, lock refresh or build still opens a draft with the error tail and
mentions `owner`. Settle uses the same exact-head CI gate for all three sources.
Its optional live Keel check includes kit tuples (which name their Keel commit)
and excludes engine-only repins, which do not change the live Keel revision.

`python3 -m unittest discover -s tests -p test_layer_repin.py` exercises local
main commits, a real Cargo kit/Keel lock resolution with one Keel, both engine
hook paths, one PR on repeat and an owned draft on a failed build.

## Inputs worth knowing

| Input | Meaning |
| --- | --- |
| `mode` | `poll`, `repin` or `settle`. |
| `layer` | `keel` (default), `kit` or `engine`; use the same source for poll and repin. |
| `tag` | A Keel tag, or `latest` (the newest `keel-verified-*`); for shared layers, `latest` follows main and a full SHA freezes the target. |
| `dry-run` | Print the pull request it would open and the workflows it would start; push nothing. Rebuilds even if the branch exists. |
| `reader-app-id`, `reader-app-key` | A read-only App for private Keel tag/release metadata, kit/engine sources, or extra build dependencies. Public CLI installation and facade compilation need none. |
| `extra-read-owner`, `extra-read-repos` | Private repositories of one owner that the lock refresh or the build fetches, such as a title kit; list every private git dependency of every tracked `Cargo.lock`. |
| `check-command`, `check-dir` | The build check and its directory. Default `cargo check`. Keel-tag repins always apply and check `keel migrate --registry VERSION` from the repository root first, even with a custom or empty build check. |
| `ci-workflows` | Workflow files started on the branch, in order. Default `ci.yml`. The web smoke must be among the checks they produce. |
| `required-checks` | settle: checks that must have succeeded on the head. Default `ci-ok web-smoke`. |
| `owner` | Who a failed-build draft or red settle comment mentions, such as `@Cubeage/studio`. |
| `max-wait-minutes` | settle: how long a required check may be absent before the pull request turns red. |
| `stall-minutes` | settle: how long an unfinished check may wait before its run says the runner has not picked the job up. Default 20; the pull request is never merged on a stalled check and never called red for one. |
| `after-merge-workflows` | settle: workflows started on the default branch after the merge. |
| `live-url` | settle: the deployed title's host; the merged repin is settled only when its `VERSION.json` `keel` field is the merged commit. Empty: no live check. |
| `live-grace-minutes` | settle: how long after the merge the deploy may take before a wrong live Keel fails the run. Default 60. |

## Tests

`python -m unittest tests.test_keel_repin`: pin discovery and rewrite, the lock refresh of a crate
held at two Keel commits (against real cargo when it is installed), the draft naming a private
repository the build could not read, poll (ahead, behind, pinned,
pull request exists), the target release's migration check (a stand-in CLI refusal names the
Android signing migration and prevents the build check), the build check, the draft pull request, supersession of older repin pull
requests, CI dispatch order, workflow files named and never edited, settle (merges only on every
required check green on the exact head and pinned to it; waits on a missing or unfinished check; a
skipped required check counts as absent; a check nobody has picked up says so and stays open rather
than turning red; red on a failure, a stranger's commit or a draft means no merge; one comment per
head and kind; the newest run of a name decides; another App's check does not count; the queue retry;
the after-merge dispatch), no approve call anywhere, the permission scopes, the 10-minute cadence, and
no GitHub-hosted runner label. A consumer run:
`workflow_dispatch` the title's `keel-repin.yml` with `dry_run` true.
