# Shared-action pin drift check

Every repository pins the shared actions in `SylphxAI/.github` by commit
(`uses: SylphxAI/.github/.github/actions/needs-pass@<40-hex> # main`). A pin that stays
put keeps running the old logic, so a fix in `.github` reaches nobody until each
pin moves. On 2026-10-15 the fleet held six different `needs-pass` SHAs from
2026-09-29 to 2026-10-04: a cancelled-lane verifier fix (`.github#258`) had
landed, and no repository was running it. Nothing turned red, because a stale
commit still resolves.

`action_pin_drift_check.py` fails a SHA pin without the trailing `# main`
version comment, or one more than `DRIFT_DAYS` (7) days behind
`SylphxAI/.github` main. Renovate uses the comment to follow the branch; a bare
SHA can resolve successfully while receiving no updates.

## What it decides

For every `uses: SylphxAI/.github/.github/{actions,workflows}/<name>@<40-hex>` in
a repository's `.github/workflows/` and nested `.github/actions/`, it first
requires `# main` after the SHA (outside quotes, if the value is quoted).
Repinning must preserve that comment. It then reads the pinned commit's
committer date and `.github` main's, and fails when the pin is more than
`DRIFT_DAYS` days older. `DRIFT_DAYS` is a named constant in the script with its
reasoning: a week is long enough for Renovate to open and land its grouped pull
request after a `.github` commit, and short enough that a broken gate is caught
within a working week rather than found by an unrelated incident.

A pin equal to `.github` main's head still needs `# main`; its date is never read. An unreadable
commit fails (a 404 is a drift we cannot measure, not a pass). A pin to a branch
or a tag is not judged here.

## Using it

```
action_pin_drift_check.py check --dir REPO   judge one checked-out repository
action_pin_drift_check.py audit --org ORG    judge every non-archived repository of ORG (read-only)
action_pin_drift_check.py watcher --org ORG  the watch-the-watcher audit below (read-only)
```

Pass each organization with `--org`; the scheduled workflow names the four
organizations in `policy/optimistic-merge.json`. The audit skips hands-off
repositories and the shared source repository `SylphxAI/.github` itself.
Tracking its own `main` would create an endless update chain: each self-pin
update advances the branch it is trying to follow. This exception applies to
the fleet pin audit, not the red-main watcher. Caller admission checks should
apply the same source exception.

`audit` exits 1 on an unmarked, drifted or unreadable pin/repository. A failed
organization listing is unreadable, never a zero-repository pass. Partial
GraphQL errors and truncated action trees fail closed. It is read-only:
GitHub reads through the `gh` CLI. The pure `main_marker_errors(files)` function
shares the parser and marker rule with offline caller admission checks, without
requiring a token or reading commit dates.

## Scheduled coverage and credentials

`.github/workflows/action-pin-drift.yml` runs every Monday at 04:27 UTC and on
`workflow_dispatch`, using `sylphx-linux-standard`. Both audits pass
`--public-only` and use the job's read-only `github.token`. The report and job
summary explicitly cover public, non-archived repositories only. Private
repositories require a separately authorized cross-repository identity; this
workflow does not claim coverage of them. An authorized local audit can omit
`--public-only` to check all repositories visible to that identity.

The obsolete `CUBEAGE_CI_READER_APP_ID` / `CUBEAGE_CI_READER_APP_KEY` mint path
is removed. The scheduled run
[40270369197](https://github.com/SylphxAI/.github/actions/runs/40270369197)
on 2026-10-26 failed at `Mint read-only token`: the action reported that its
`client-id` (or deprecated `app-id`) input was empty, and the pin audit was
skipped. Those inputs are no longer used. The company identity policy allows
the existing `sylphx-ai` App with Broker-held signing, not a new Reader App or
a private key added here. Public reads need neither. If the job token is
missing, either audit fails before acquisition with
`read-only GitHub token is unavailable; public audit cannot run`.

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

`tests/test_action_pin_drift_check.py` runs on a fake GitHub: a bare SHA fails
even at the current head; quoted pins accept an external `# main`; a marked pin
past the limit fails with file, line and days; a marked pin at or inside the
limit passes; an unknown commit fails; each distinct pin is read once; nested
composite actions are read; public scope excludes private repositories and the
source repository; unreadable inputs fail. It also executes both audit shell
steps with an empty token to prove a clear failure before acquisition.
A red trunk with only a skipped `Red main` fails; a green `Red main` that
stopped at classification is not a repair and fails; a run that reached the
repair step inside the window passes; a late repair outside the window fails.
