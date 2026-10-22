# Shared-action pin drift check

Every repository pins the shared actions in `SylphxAI/.github` by commit
(`uses: SylphxAI/.github/.github/actions/needs-pass@<40-hex>`). A pin that stays
put keeps running the old logic, so a fix in `.github` reaches nobody until each
pin moves. On 2026-10-15 the fleet held six different `needs-pass` SHAs from
2026-09-29 to 2026-10-04: a cancelled-lane verifier fix (`.github#258`) had
landed, and no repository was running it. Nothing turned red, because a stale
commit still resolves.

`action_pin_drift_check.py` turns that silence into a failure. It fails a pin
that is more than `DRIFT_DAYS` (7) days behind `SylphxAI/.github` main.

## What it decides

For every `uses: SylphxAI/.github/.github/{actions,workflows}/<name>@<40-hex>` in
a repository's `.github/workflows/` and `.github/actions/`, it reads the pinned
commit's committer date and `.github` main's, and fails when the pin is more than
`DRIFT_DAYS` days older. `DRIFT_DAYS` is a named constant in the script with its
reasoning: a week is long enough for Renovate to open and land its grouped pull
request after a `.github` commit, and short enough that a broken gate is caught
within a working week rather than found by an unrelated incident.

A pin equal to `.github` main's head is current and never read. An unreadable
commit fails (a 404 is a drift we cannot measure, not a pass). A pin to a branch
or a tag is not judged here.

## Using it

```
action_pin_drift_check.py check --dir REPO   judge one checked-out repository
action_pin_drift_check.py audit --org ORG    judge every non-archived repository of ORG (read-only)
action_pin_drift_check.py watcher --org ORG  the watch-the-watcher audit below (read-only)
```

`audit` reads the organizations in `policy/optimistic-merge.json`, skips
hands-off repositories (the same policy `workflow_pin_check.py` uses), and exits
1 on any drifted pin. It is read-only: REST reads through the `gh` CLI.

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

`tests/test_action_pin_drift_check.py` runs on a fake GitHub: a pin past the
limit fails with file, line and days; a pin at or inside the limit passes; the
head commit is never behind; an unknown commit fails; each distinct pin is read
once; a red trunk with only a skipped `Red main` fails; a green `Red main` that
stopped at classification is not a repair and fails; a run that reached the
repair step inside the window passes; a late repair outside the window fails.
