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
| Merge group | gate lanes only (target p90 under 5 min); while the trunk is red and its way back is in motion, only a revert or the labelled fix is admitted | `ci-ok` |
| Push to the trunk | the full suite over every commit since the last verified one | none; `verified` marks the commit |
| `verify.yml` fails on the trunk | red-main handler: trace and revert (never a rerun; a flake is quarantined through the quarantine list) | - |

Gate lanes: format, lint, typecheck, workflow parse, generated-code and
contract drift, the unit tests the change affects, and - only when the change
touches the repository's migration globs - the migration lanes: lint and
integrity (atlas lint and `atlas.sum`, drizzle checks) and the database-backed
migration tests. DDL cannot be undone by a revert, so a migration is
exercised before it reaches the trunk. A change to `sylphx.toml`, a `package.json`, a
Dockerfile or the stack baseline also runs the stack-conformance lane
([stack-conformance.md](stack-conformance.md)). Everything else -
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
   that runs tests uploads a JUnit report as `junit-<lane>` (failing and passing
   testcases); the handler names failing tests and each test's trace window
   from it. The upload is a diagnostic: it carries `continue-on-error: true`
   and `retention-days: 3`, so a spent organization artifact quota never turns
   a passing lane red ([run-store](run-store.md)). Its input `path` is the
   report file only: a working tree copied with it, a `**/*.bundle` or a
   hand-entry that drops a large blob in `workflow-templates` and uploads it,
   *does* spend the quota and holds the whole organization's CI down until the
   window recalculates (OzyrixLtd, 2026-10-07). This document is the complete
   set of steps that stand between the copy and a working pipeline; a lane's own
   material is the caller's, install steps included, and is never added here.
   A file or directory another job of the same run needs is a hand-over: use
   [`run-store`](run-store.md), never `upload-artifact`.
4. **`.github/workflows/red-main.yml`** from
   [`workflow-templates/red-main.yml`](../workflow-templates/red-main.yml),
   unchanged. In the organization that holds the builder App
   (`SYLPHX_BUILDER_APP_ID` variable, `SYLPHX_BUILDER_PRIVATE_KEY` secret) it
   reverts as `on_red` says (absent: `revert`; `notify` is an explicit
   opt-out). It never reruns. Anywhere else it runs in
   token mode: a comment on the affected pull requests,
   never a pull request of its own - a person reverts. The App key never
   leaves its organization. Token split: every Actions call (runs, jobs,
   artifacts, dispatch of the verify workflow) uses the caller's
   `github.token`, so the caller grants `actions: write`; the builder App
   installation needs no `actions` permission, only contents, issues,
   pull-requests and workflows write (a verify or revert branch can point at
   a commit with older workflow files), and is used for what must start CI (verify and revert
   branches, pull requests, enqueue). If the App mint or the grant probe
   fails, the handler comments the missing grant on `ops-issue` (the caller
   grants `issues: write`), writes it to the step summary and fails the job;
   it never fails silently.
   **Trunk**: the handler guards the repository's default branch, read from
   the event payload, so a repository on `master` is classified like one on
   `main`. The `trunk` input names a different branch. The starter's `if:`
   compares the failed run's branch with the same default branch; list that
   branch under `push` in `verify.yml` too.
   **Key in an environment**: a repository may hold the key in a GitHub
   environment (deployments limited to the trunk and release tags) instead of
   a repository secret. The caller sets `with: environment: <name>` and drops
   the `secrets:` pass-through; the handler job enters that environment and
   reads `SYLPHX_BUILDER_PRIVATE_KEY` from it (a `uses:` job cannot declare
   `environment:` itself). The default, empty, keeps the passed-secret path.
5. **Labels** the handler uses but never creates, and silently skips when
   absent: `flake`, `quarantine`, `auto-revert`, `queue-jump:red-main`, and
   `main-red-fix` (below). Create them in the same change.
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
   the queue. The queue's own parameters (HEADGREEN and the rest) come from
   [merge-queue-settings.md](merge-queue-settings.md), not from the ruleset by hand.

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

## Stop the line on a red trunk

This is Chromium's tree closure for our merge queue: a failing tree-closer
builder closes the tree, the commit queue's tree status check holds every
ordinary change until it reopens, and only a change carrying `No-Tree-Checks:
true` (the revert, or the fix that reopens the tree) lands meanwhile.

The starter gate has a `main-state` job (merge groups only) that runs
[`main-red-gate`](../.github/actions/main-red-gate/action.yml). The trunk is
red when its newest conclusive push run of the verify workflow failed
(cancelled, skipped and superseded runs are passed over, as in owner
standards/dx.md); a later success clears it. While red, a merge group is
admitted when it is the way back to green:

- a revert: branch `auto-revert/*` (the red-main handler's), a title starting
  `Revert`, or the `auto-revert` / `queue-jump:red-main` labels;
- a live-outage fix, labelled `queue-jump:outage` (the handler never reverts
  one either);
- the fix, when a person labels the pull request `main-red-fix`.

Any other pull request fails `main-state`, so `ci-ok` fails, and it leaves the
queue, but only while the way back is in motion: a newer run of the verify
workflow on the trunk is still running, or a revert or fix pull request (not a
draft) is open. When nothing is in motion the group is admitted with a
warning. The red-main handler reverts only after the same failure repeats on a
second completed run, and never reverts an infrastructure failure, a
`timed_out` run or one that did not start; a queue that stayed shut on the
first red would wait for a run nothing can start, and the next change landing
is that run. A person can always pass a change by labelling it `main-red-fix`.

The gate fails open: a verify history, pull request or pull request list it
cannot read admits with a warning, so it never jams the queue on its own
fault. The starter ships `mode: observe` (prints what it would refuse and
refuses nothing): read a few merge groups, including one on a red trunk, then
set `enforce`. The job needs `pull-requests: read` beside the gate's other
read permissions. A check on the trunk that is not the verify workflow (a
benchmark or nightly) never makes the trunk red here.

## The shared pieces

- [`main-red-gate`](../.github/actions/main-red-gate/action.yml): the red-trunk
  admission rule above; `mode`, `verify-workflow` and `fix-label` inputs.
- [`ci-range`](../.github/actions/ci-range/action.yml): the range and the lane
  selection, the same answer in the gate and in verify. Lanes are
  `name: path globs`; a change under `.github/` runs every lane. Use its
  `base` output for affected-only builds (`turbo run --affected` with
  `TURBO_SCM_BASE`, `cargo nextest run -p` on the changed crates, `nx affected
  --base`). A push/dispatch base is the newest first-parent ancestor whose
  authenticated full post-main `verified` job succeeded: GitHub Actions App,
  approved workflow path, event `push`, tracked branch, same repository and
  exact SHA, with matching latest-attempt job/check membership. Run, job and
  check-run IDs are separate identities: the job's strictly validated
  `check_run_url` supplies the check ID, fetched through a locally constructed
  API path; the authenticated check's details URL must name the actual job ID.
  The newest approved run wins before its latest attempt is read, so an
  older run's attempt cannot mask a newer run's failure. Whole-workflow success is
  not proof; optional publication failure does not erase verified.
  Recording, diagnostic and merge-group runs can never supply a baseline or
  mask a genuine post-main failure. The shared red-main handler embeds the
  exact same helper (a source-equality regression prevents drift) for event
  selection, the same-SHA green guard and revert baseline.
  Unknown/incomplete reads fail closed: Verify runs all lanes, there is no
  trusted baseline or deploy proof, and no destructive revert is authorized.
  They do not silence red-main recovery: eligible runs with unavailable proof
  stay active for investigation or escalation. Partial/null
  producer metadata is unknown, never a positively established ineligible
  producer; partial newer history cannot expose an older proof. Only an
  authenticated success satisfies the same-SHA green guard. A failure from an
  older run cannot authorize a revert: the same-SHA green guard reads the
  newest approved same-SHA producer and stops on success. Unknown proof
  escalates without a revert. Previous-run history/proof unavailability also
  escalates; it is not a genuinely absent prior run or an unconfirmed failure.
  `mode: run` exposes a `verdict` without builder secrets and returns `unknown`
  on provider exceptions instead of failing the preflight job. An authenticated
  non-proof producer returns `ineligible`; callers must retain handling for
  unknown, never treat it as success or terminal no-action. Destructive revert
  requires authenticated failure plus a trusted authenticated baseline.
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

  It also refuses a timing or performance budget before merge
  ([`perf_gate.py`](../.github/actions/workflow-lint/perf_gate.py)): in a
  workflow triggered by `pull_request`, `pull_request_target`, `merge_group`
  or `workflow_call`, `PERF_ENFORCE` other than `0`, Lighthouse (`lhci`),
  `hyperfine`, `k6 run` or the chat perf run fail the lint. Shared runners
  move wall-clock numbers with load, so timing is judged after merge, as a
  median against a stored baseline; before merge a workflow may only measure
  and print. A delivered customer repository (`sylphx_delivery` =
  `delivered`, read from the event payload or the repository's property
  values) skips this rule.
- [`red-main.yml`](../.github/workflows/red-main.yml): the reusable handler.

## Lifecycle-only alerts (no repair)

Set `mode: lifecycle` to reconcile one owned alert per repository, workflow
file and aggregate check name without dispatching, quarantining or
reverting anything. This explicit opt-in does not require the optimistic-merge
manifest declaration. Existing callers default to `mode: repair` unchanged.

The caller must trigger on **completed runs, including successes**, pass the
exact run id **and attempt**, and pass App credentials explicitly. The App
installation needs `issues: write` on the calling repository; run/job reads
use the caller's `github.token` with `actions: read`, never the App token.
App-token fallback is intentionally unavailable. Create `owner:ops`
in the caller repository before adoption. Replace `<lifecycle-pin>` with the
reviewed full commit SHA, and `Verify` / `verify.yml` / `verified` with the
caller's workflow display name / filename / aggregate job name.

```yaml
name: CI alert lifecycle
on:
  workflow_run:
    workflows: [Verify]
    types: [completed]
    branches: [main]
permissions:
  contents: read
  actions: read
jobs:
  alert:
    if: >-
      github.event.workflow_run.event == 'push'
      && github.event.workflow_run.head_branch == 'main'
      && github.event.workflow_run.head_repository.full_name == github.repository
    uses: SylphxAI/.github/.github/workflows/red-main.yml@<lifecycle-pin>
    with:
      mode: lifecycle
      repository: ${{ github.repository }}
      verify-workflow: verify.yml
      verify-check-name: verified
      head-sha: ${{ github.event.workflow_run.head_sha }}
      run-id: ${{ format('{0}', github.event.workflow_run.id) }}
      run-attempt: ${{ format('{0}', github.event.workflow_run.run_attempt) }}
      result: ${{ github.event.workflow_run.conclusion == 'success' && 'success' || github.event.workflow_run.conclusion == 'failure' && 'failure' || github.event.workflow_run.conclusion == 'cancelled' && 'cancelled' || 'unknown' }}
      app-id: ${{ vars.SYLPHX_BUILDER_APP_ID }}
    secrets:
      app-private-key: ${{ secrets.SYLPHX_BUILDER_PRIVATE_KEY }}
```

Do not add a failure-only `if:` to the caller: success events are the recovery
signal. Only same-repository `push` runs on `main` are eligible; fork PRs,
non-main runs, dispatches and merge groups cannot mutate lifecycle alerts.
The caller guard above saves a job, but is not the authority: event binding
and every run/attempt API re-authentication enforce all three provenance fields.
Run and attempt reads authenticate the event again immediately before
each write, including the named aggregate job's conclusion. Lifecycle imports
the canonical `ci-range/post_main.py` counted reader from a sparse checkout of
this reusable workflow's exact SHA (`job.workflow_sha`), with no persisted
credentials. Run history and jobs require stable `total_count` envelopes and
complete distinct coverage. Issues use GraphQL's `totalCount` connection through
the same reader because REST issue lists provide no total. Each issue page must
also have `hasNextPage == (cumulative raw nodes < totalCount)`; a contradictory
continuation flag invalidates enumeration before any mutation. The shared five-page
limit fails closed, as do missing counts, short pages, duplicates, GraphQL errors,
and missing/null/blank status or conclusion on a potentially relevant run.
No such read can produce a recovery comment or close. The handler lists
all issue pages and states, matches both the App author id and a hidden stable
identity fingerprint, and retains the last represented event and failure in
the issue marker. Ordering is `(run id, attempt)`, not issue creation time or
event delivery order. A newer authenticated success comments with its green
run link and closes only that owned identity issue; later failures reopen the
same issue. Cancelled and unknown events advance its ordering record without
closing it. An unavailable or mismatched proof logs the reason and stops
without recovery or other destructive action. Unrelated flake/red-main issues
are never touched. Calls share the existing per-repository concurrency group
with `queue: max`: [GitHub's concurrency contract](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency)
allows up to **100 pending** jobs/runs, then cancels additional arrivals.
Queue arrival order is not Verify run order. Immediately before each recovery
comment or close, the handler lists the eligible completed main Verify runs
and re-authenticates the latest `(run id, attempt)` through the run/attempt and
aggregate-job APIs. Only that latest successful run can recover: an older green
cannot close over a newer red even if the red handler never ran. Cancelled and
unknown conclusions are not eligible recovery evidence. A list/read failure
leaves the alert open; no parallel writer should edit lifecycle markers.

## Rules kept from the July rollout

- A running verify on the trunk is never cancelled; a newer push waits and
  covers what it carries (`concurrency: cancel-in-progress: false`).
- Verify cost follows cycles, not commits: one run covers the whole range since
  the last verified commit, never `HEAD~1`.
- A culprit is traced among the unverified commits at once; when it cannot be
  named with certainty, the whole window is reverted in one pull request. A
  caller that sets `revert-window: false` reverts only a traced culprit and
  reports a window on its pull requests instead; one whose suite lanes run
  longer than 20 minutes raises `candidate-timeout-minutes` (at most 60), or
  every trace ends inconclusive.
- The trace window of a unit that breaks inside an already red trunk starts at
  the newest earlier completed push run of the verify workflow where that unit
  did not fail, not at the last fully verified commit. A job that succeeded
  clears its units; a job that failed clears a test only when its `junit-<lane>`
  report shows the test passing (a report that lists failures only clears a
  test it does not list). Only units that failed on two consecutive runs count,
  the aggregate `verified` job is not a unit, the candidate runs dispatch only
  those units' lanes, and a candidate counts as failed only if one of those
  units failed in it. With no such run in the last 100, the fully verified
  commit stays the baseline.
- A failing unit is a test of a job, from the job's `junit-<lane>` report. A
  job that publishes no report is one unit named by how it failed: its
  conclusion, its failed steps and its error lines (the check run's failure
  annotations, every `##[error]` line, with timestamps, hashes and durations
  masked). Two runs, or a run and a trace candidate, failed the same way only
  when the same test failed, or the same job failed at the same step with the
  same error lines; the job name alone never matches, and the aggregate
  `verified` job is never a unit. A job that failed both times at different
  steps (one step stopping the job before the other runs) confirms nothing,
  and a candidate that failed a traced job some other way is inconclusive,
  never the culprit. A step that ends in the runner's bare "Process completed
  with exit code N." is told apart by its step only; print the failing check
  as an `::error::` line (or publish `junit-<lane>`) so two failures of one
  step are told apart too. Steps and error lines that cannot be read stop the
  handler without a revert, as any unread proof does.
- A flake is quarantined first, in its own source (`#[ignore = "quarantined
  <date>: <reason> (<issue>, owner <lane>)"]`, or a comment above
  `test.skip(`), never retried in the queue.

## Runners

Merge-group jobs run on the merge lane: `runs-on: ${{ github.event_name ==
'merge_group' && 'sylphx-linux-standard-merge' || 'sylphx-linux-standard' }}`
(`-xlarge-merge` for xlarge jobs), so a merge group never waits behind the
pull-request backlog. The gate starter already uses it.

The verdict jobs (`ci-ok`, `verified`) run on `sylphx-linux-control`, the
reserved gate pool, on every event; it has no `-merge` twin. A verdict keeps
`if: always()`, so a cancelled run still reports a failure instead of a
skipped check, which GitHub counts as passing. It therefore runs after every
cancel, and the cancelled run holds its concurrency group until it has: on a
busy build pool that left a pull request's next run pending with no jobs
(SylphxAI/agents#4222, 2026-10-02).

Private repositories run every job on our runners: `sylphx-linux-standard`
for most lanes, `sylphx-linux-xlarge` for heavy compiles, `sylphx-linux-large`
between. Public repositories may use GitHub's standard hosted runners, which
are free for them. Never larger or GPU hosted runners.

## Conformance check

[`scripts/audit_optimistic_merge.py`](../scripts/audit_optimistic_merge.py)
reads every non-archived repository of the organizations in
[`policy/optimistic-merge.json`](../policy/optimistic-merge.json) (read-only:
a few batched GraphQL queries and one compare read per distinct pin) and
reports it against these rows. It exits 1 on any FAIL outside a named
exemption and writes the whole report with `--json`.

| Row | Holds when |
| --- | --- |
| R1 | `sylphx.toml` has `[ci] merge = "optimistic"` and an explicit `on_red` |
| R2 | `ci.yml` runs on `merge_group` and has the `ci-ok` job |
| R3 | `verify.yml` runs on push to the default branch, has a `verified` job and does not cancel a trunk run |
| R3b | every other workflow that runs on push to the default branch is called from `verify.yml`, or carries the comment `# optimistic-merge: advisory`; otherwise its red never reaches the handler |
| R4 | `red-main.yml` calls the shared handler pinned to a full SHA at or after the policy floor, and its `if:` follows the default branch |
| R5 | `ci.yml` has `main-state` on `main-red-gate`, pinned at or after the floor, and `ci-ok` needs it |
| R6 | the default branch has a merge queue, `ci-ok` is required (where R2 applies) and `verified` is not |
| R7 | `on_red` is `revert` (or `revert_pr_unarmed`) where the builder App reaches the repository, `notify` elsewhere |
| R8 | in every workflow that runs on `merge_group`, each job on `sylphx-linux-standard` or `sylphx-linux-xlarge` selects its `-merge` twin on `merge_group` (the expression under Runners); verdict jobs on `sylphx-linux-control`, jobs whose `if:` keeps `merge_group` out, and runners chosen by `matrix`/`inputs` are out of scope |

Unreadable is FAIL. A repository whose rows are waived by a policy
exemption reports EXEMPT; an exemption carries a class, reason, owner and a
review date, and one past its date stops applying. Hands-off repositories
and fully waived ones are listed by name only; their files are not read.
Move `pin_floor` forward in the policy when a fix every caller needs lands:
every pin behind it then fails R4 and R5 until it is repinned.

## Read back after landing

- The first merge group passes with only the gate lanes, and the first push
  run of `Verify` ends with a `verified` check run on the trunk.
- `gh workflow run red-main.yml` classifies the newest failed verify run (a
  rehearsal; it acts only as `on_red` allows).
- Report: gate p90, pull request CI wall p50/p90 and arm-to-merge p50/p90,
  before and after.
