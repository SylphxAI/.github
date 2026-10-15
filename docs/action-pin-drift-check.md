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
`.github` workflow, so one mistake can disarm it everywhere at once. A caller
that triggers it on `workflow_run` still records a run when every job skips — a
run with conclusion `skipped` looks like the trunk was handled when nothing ran.

`watcher --org ORG` fails a repository whose trunk had a `Verify` run conclude
`failure` with no non-skipped `Red main` run inside `RED_MAIN_WINDOW_MINUTES`
(30) minutes of it. On 2026-10-14 `SylphxAI/keel` had three consecutive trunk
`Verify` failures and every `Red main` run since was `skipped`; the
same shape was present on a handful of other repositories.

`red-main` is a `workflow_call` handler that takes a `mode` input; a caller that
passes none skips every job while still recording a run. The check anchors its
window on `run_started_at` (the moment the `Verify` run began, queue waits
excluded), so a merely queued or slow run does not read as unhandled.

## Tests

`tests/test_action_pin_drift_check.py` runs on a fake GitHub: a pin past the
limit fails with file, line and days; a pin at or inside the limit passes; the
head commit is never behind; an unknown commit fails; each distinct pin is read
once; a red trunk with only skipped `Red main` fails; a non-skipped `Red main`
inside the window passes.
