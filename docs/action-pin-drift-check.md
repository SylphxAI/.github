# Shared-action pin provenance and drift

Every caller of `SylphxAI/.github` must use a full commit SHA followed by
`# main`, for example:

```yaml
- uses: SylphxAI/.github/.github/actions/needs-pass@<40-hex> # main
```

Renovate uses the comment to follow the branch. A bare SHA can keep resolving
while receiving no updates. A recent commit can also come from an unmerged
branch, or predate the action it names. Resolving a commit alone proves neither
that it was released on main nor that GitHub can load the target.

## What it decides

The shared parser reads `.github/workflows/` and nested `.github/actions/` YAML
files. Each `SylphxAI/.github` reference must have a full 40-hex SHA and an
external `# main` comment, including a reference to the current head. For quoted
values the comment belongs outside the quotes. Branches, tags, short SHAs and
malformed refs fail.

The checker captures shared `main` once as an immutable SHA. Every pin must be
an ancestor of that anchor, and its Git tree must contain the exact reusable
workflow file or the action directory's `action.yml` or `action.yaml`. A
feature-branch commit fails even when it is recent and contains the action.
Missing main, compare or tree evidence fails with the acquisition reason;
truncated trees fail too. Proof is cached per SHA and main anchor.

The source repository `SylphxAI/.github` has a narrow exception to avoid updating
its own pins after every release: its SHA references need no `# main` comment,
and `@main` resolves to the captured immutable main anchor. Ancestry and target
checks still apply. Other source branch/tag refs fail. External callers cannot
use this exception.

The central audit and standalone `check` also fail pins more than `DRIFT_DAYS`
(7) days behind main's committer date. A week allows Renovate to open and land
its grouped update while exposing stale shared logic within a working week.
The caller gate enforces format, ancestry and target existence without an age
limit. It uses the same pure `validate_pin_provenance` validator as the audit;
`main_marker_errors` remains available for format-only inspection.

## Caller gate and local use

The shared `stack-conformance` action checks its caller's own Git objects,
including private repositories. It uses the existing `github.token` to acquire
proof from the public shared source, so it needs no cross-repository App or
private-repository credential. Unavailable proof makes the gate fail and is
recorded under `shared_pin_provenance` in its JSON report.

Default CLI execution also acquires live proof through `gh`:

```bash
python3 .github/actions/stack-conformance/stack_conformance.py check \
  --repo CALLER --repository ORG/REPO
python3 scripts/action_pin_drift_check.py check \
  --dir CALLER --repository ORG/REPO
```

For explicit offline review, add `--shared-source /path/to/shared-checkout` to
either command. This requires a complete, nonshallow local shared checkout and
`refs/remotes/origin/main`; it never substitutes local HEAD for a missing main
anchor. Output and the caller report identify the proof as an offline
`origin/main` snapshot and print its full anchor SHA. This proves that snapshot,
not live freshness. Git lazy fetching is disabled. The published action always
uses live proof.

```bash
python3 scripts/action_pin_drift_check.py audit --org ORG --public-only
python3 scripts/action_pin_drift_check.py watcher --org ORG --public-only
```

Pass each organization with `--org`. The audit skips archived and hands-off
repositories. It includes the shared source with the exception described above.
An unreadable repository or organization listing fails; an acquisition failure
cannot become a zero-repository pass. All GitHub operations are read-only.

## Scheduled coverage and credentials

`.github/workflows/action-pin-drift.yml` runs every Monday at 04:27 UTC and on
`workflow_dispatch`, using `sylphx-linux-standard`. Both audits pass
`--public-only` and use the job's read-only `github.token`. Their reports cover
public, non-archived repositories only. Private callers enforce their own pins
through `stack-conformance`; the central audit claims no private coverage. An
authorized local audit can omit `--public-only` to read repositories visible to
its existing identity.

The obsolete `CUBEAGE_CI_READER_APP_ID` / `CUBEAGE_CI_READER_APP_KEY` mint path
was removed. Scheduled run
[40270369197](https://github.com/SylphxAI/.github/actions/runs/40270369197)
on 2026-10-26 failed at `Mint read-only token` because its `client-id` (or
deprecated `app-id`) input was empty, skipping the audit. Those App inputs are
no longer used, and the old App and organization settings are left for their
owner to clean up. A missing job token fails before acquisition with
`read-only GitHub token is unavailable`.

## Watch the watcher

The same script has a second audit, `watcher`, for a different silent failure:
`red-main` is the fleet's safety net for a red trunk, and it is a shared
`.github` workflow, so one mistake can disarm it everywhere at once.

The caller's guard is exactly:

```
github.event_name == 'workflow_dispatch'
|| (github.event.workflow_run.conclusion == 'failure' && github.event.workflow_run.head_branch == 'main')
```

so a run appears for a manual dispatch or for a `Verify` that concluded
`failure` on the trunk. But **a green conclusion is not a repair**: the
classification step can emit `verdict=infra`, or the handler can stop on an
already-in-hand revert, after which the repair steps are skipped and the run
still ends `success`. A run can also be recorded with conclusion `skipped`,
which is not a response at all.

`watcher --org ORG` therefore keys on the repair, not the conclusion. It fails a
repository whose trunk had a `Verify` run conclude `failure` with no `Red main`
run that reached its repair step (`Revert the culprit or report it`, which runs a
revert or the report path for a migration hold, a protected pull request or an
infra verdict) inside `RED_MAIN_WINDOW_MINUTES` (30) minutes of it. On
2026-10-14 `SylphxAI/keel` had three consecutive trunk `Verify` failures, and
every `Red main` run since either was `skipped` or ended green at classification
without a repair; the same shape was present on other repositories.

The window anchors on `run_started_at` (the moment the `Verify` run began, queue
waits excluded), so a merely queued or slow run does not read as unhandled. One
`actions/runs/<id>/jobs` read per candidate run is what proves the repair.

## Tests

`tests/test_action_pin_drift_check.py` covers full-SHA/comment rules, recent
feature commits, missing target files, both action metadata extensions, exact
workflow paths, cached proof, missing acquisition, drift age and public scope.
It executes both audit shell steps with an empty token to verify clear failure.

`tests/test_shared_pin_provenance_gate.py` executes the real caller CLI against
complete local Git fixtures: a private caller passes with valid proof; a
feature SHA, absent target, unavailable live source and missing offline main
anchor fail. Source comment and `@main` exceptions still require target and
ancestry proof. The action entrypoint also rejects a missing token before reads.

The watcher tests distinguish a skipped or classification-only Red main run
from one that reached its repair step inside the response window.
