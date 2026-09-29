# Optimistic merge: adopting it in a repository

The merge queue proves a change can land; the full suite proves the trunk
after it lands. Queue time drops to the fast gate, and only verified commits
deploy. The rule is owner `standards/dx.md` (Merge queue); the platform
contract - what `verified` means to Release, what the red-main handler may do -
is `docs/services/hosting/verified-commits.md` in `SylphxAI/cloud`. This page
is the recipe. One pull request per repository carries all of it, so there is
no half state.

## What runs where

| Event | Runs | Required check |
| --- | --- | --- |
| Draft pull request | gate lanes only - the draft is the compiler | `ci-ok` |
| Pull request marked ready | gate lanes + the full suite the change affects | `ci-ok` |
| Merge group | gate lanes only (target p90 under 5 min) | `ci-ok` |
| Push to the trunk | the full suite over every commit since the last verified one | none; `verified` marks the commit |
| `verify.yml` fails on the trunk | red-main handler: rerun, quarantine a flake, or trace and revert | - |

Gate lanes: format, lint, typecheck, workflow parse, generated-code and
contract drift, the unit tests the change affects, and - only when the change
touches the repository's migration globs - the migration lanes: lint and
integrity (atlas lint and `atlas.sum`, drizzle checks) and the database-backed
migration tests. DDL cannot be undone by a revert, so a migration is
exercised before it reaches the trunk. Everything else -
integration and database tests, browser and device matrices, release builds,
proofs - is a suite lane.

## The pull request

1. **Declare it** in the repository's `sylphx.toml` (the only surface; a
   repository with none adopts a minimal one):

   ```toml
   version = "1"

   [ci]
   merge = "optimistic"
   on_red = "notify"   # "revert" once the builder App holds the write grant
   ```

2. **`.github/workflows/ci.yml`** from
   [`workflow-templates/optimistic-gate.yml`](../workflow-templates/optimistic-gate.yml):
   the gate lanes, a `suite` job that calls `verify.yml` on ready pull
   requests, and the `ci-ok` aggregate. Keep the job name the ruleset
   requires.
3. **`.github/workflows/verify.yml`** from
   [`workflow-templates/optimistic-verify.yml`](../workflow-templates/optimistic-verify.yml):
   the suite lanes and the aggregate job named exactly `verified`. Each lane
   that runs tests uploads a JUnit report as `junit-<lane>`; the handler names
   flaky tests from it.
4. **`.github/workflows/red-main.yml`** from
   [`workflow-templates/red-main.yml`](../workflow-templates/red-main.yml),
   unchanged. In the organization that holds the builder App
   (`SYLPHX_BUILDER_APP_ID` variable, `SYLPHX_BUILDER_PRIVATE_KEY` secret) it
   reruns, quarantines and reverts as `on_red` says. Anywhere else it runs in
   token mode: rerun, flake issue and a comment on the affected pull requests,
   never a pull request of its own - a person reverts. The App key never
   leaves its organization. Token split: every Actions call (runs, jobs,
   artifacts, rerun, dispatch of the verify workflow) uses the caller's
   `github.token`, so the caller grants `actions: write`; the builder App
   installation needs no `actions` permission, only contents, issues and
   pull-requests write, and is used for what must start CI (verify and revert
   branches, pull requests, enqueue). If the App mint or the grant probe
   fails, the handler comments the missing grant on `ops-issue` (the caller
   grants `issues: write`), writes it to the step summary and fails the job;
   it never fails silently.
   **Key in an environment**: a repository may hold the key in a GitHub
   environment (deployments limited to the trunk and release tags) instead of
   a repository secret. The caller sets `with: environment: <name>` and drops
   the `secrets:` pass-through; the handler job enters that environment and
   reads `SYLPHX_BUILDER_PRIVATE_KEY` from it (a `uses:` job cannot declare
   `environment:` itself). The default, empty, keeps the passed-secret path.
5. **Labels** the handler uses but never creates, and silently skips when
   absent: `flake`, `quarantine`, `auto-revert`, `queue-jump:red-main`.
   Create them in the same change.
6. **Pin** every `SylphxAI/.github/...@main` in the starters to the commit you
   adopt.
7. **Rust repositories**: add `.github/workflows/sylphx-check.yml`, a copy of
   [`workflow-templates/rust-check.yml`](../workflow-templates/rust-check.yml),
   unchanged. It lets an agent run `cargo check` on CI instead of the desk
   (`sylphx build check`, or until the CLI ships
   `gh workflow run sylphx-check.yml -R ORG/REPO -f ref=BRANCH -f packages="a b"`);
   see [rust-check.md](rust-check.md).
8. **Ruleset**: `ci-ok` stays the only required check. `verified` is never a
   required check: it exists only after a merge, so requiring it deadlocks
   the queue.

`verified` needs only the gating suite lanes. A report-only lane (for example
one that runs only quarantined tests) stays out of its `needs` and uses
`continue-on-error`, because the handler also wakes on any failed Verify run.
A lane that runs `--include-ignored` must skip the quarantined tests by name,
or the marker does not hold there.

**Deploying only verified commits** is a separate, later opt-in: add the
`verified` required proof to the environment's delivery policy only after
Release keeps waiting releases instead of dropping them on each new commit
(cloud#10373 live in production). Until then the declaration speeds the
queue, and deploys stay as they are.

## The shared pieces

- [`ci-range`](../.github/actions/ci-range/action.yml): the range and the lane
  selection, the same answer in the gate and in verify. Lanes are
  `name: path globs`; a change under `.github/` runs every lane. Use its
  `base` output for affected-only builds (`turbo run --affected` with
  `TURBO_SCM_BASE`, `cargo nextest run -p` on the changed crates, `nx affected
  --base`).
- [`needs-pass`](../.github/actions/needs-pass/action.yml): the `ci-ok` and
  `verified` verdict. Skipped passes; failed or cancelled fails; `required:
  plan` makes a broken plan a failure. `required-unless-merge-group: suite`
  (the PR-time jobs) makes a skipped job a failure on every event but
  `merge_group`, so a dispatch run cannot post a green verdict over a red one.
- [`rust-sccache`](../.github/actions/rust-sccache/action.yml): the Rust
  compile cache. On Sylphx runners it uses the org's own prefix of the
  in-cluster object store through the per-org credential the platform puts
  on every runner (short-lived, scoped to `sccache/<installation id>/`; no
  secret, and no org can read or poison another's entries). Elsewhere, the
  GitHub Actions cache (no secret; its default per-repository limit evicts
  old entries, and it is never raised - spend stays $0). The
  `s3-access-key` / `s3-secret-key` inputs (the organization secrets
  `SYLPHX_CI_CACHE_*`) still work until they are retired. Entries are
  prefixed by repository.
- [`workflow-lint`](../.github/actions/workflow-lint/action.yml): pinned
  actionlint, the gate's workflow parse. Declare self-hosted runner labels in
  `.github/actionlint.yaml` (`self-hosted-runner: labels:`), never with an
  ignore pattern. `ignore` takes one regular expression per line and `args`
  one argument per line, never shell-quoted and never split on spaces:

  ```yaml
  - uses: SylphxAI/.github/.github/actions/workflow-lint@<pin>
    with:
      ignore: |
        property "workflow_sha" is not defined
  ```
- [`red-main.yml`](../.github/workflows/red-main.yml): the reusable handler.

## Rules kept from the July rollout

- A running verify on the trunk is never cancelled; a newer push waits and
  covers what it carries (`concurrency: cancel-in-progress: false`).
- Verify cost follows cycles, not commits: one run covers the whole range since
  the last verified commit, never `HEAD~1`.
- A culprit is traced among the unverified commits at once; when it cannot be
  named with certainty, the whole window is reverted in one pull request.
- A flake is quarantined first, in its own source (`#[ignore = "quarantined
  <date>: <reason> (<issue>, owner <lane>)"]`, or a comment above
  `test.skip(`), never retried in the queue.

## Runners

Merge-group jobs run on the merge lane: `runs-on: ${{ github.event_name ==
'merge_group' && 'sylphx-linux-standard-merge' || 'sylphx-linux-standard' }}`
(`-xlarge-merge` for xlarge jobs), so a merge group never waits behind the
pull-request backlog. The gate starter already uses it.

Private repositories run every job on our runners: `sylphx-linux-standard`
for most lanes, `sylphx-linux-xlarge` for heavy compiles, `sylphx-linux-large`
between. Public repositories may use GitHub's standard hosted runners, which
are free for them. Never larger or GPU hosted runners.

## Read back after landing

- The first merge group passes with only the gate lanes, and the first push
  run of `Verify` ends with a `verified` check run on the trunk.
- `gh workflow run red-main.yml` classifies the newest failed verify run (a
  rehearsal; it acts only as `on_red` allows).
- Report: gate p90, pull request CI wall p50/p90 and arm-to-merge p50/p90,
  before and after.
