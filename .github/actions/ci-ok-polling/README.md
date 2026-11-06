# Legacy cross-workflow verdict

This action supports legacy repositories whose checks span independent
workflows. Prefer the strict [`ci-ok`](../ci-ok) action with explicit `needs:`
dependencies; that dependency graph is the target completion boundary.

A completed failed, cancelled, timed-out, action-required, startup-failed or
stale check fails immediately on the next successful poll, even while other
checks remain queued or running. Failure does not wait for a stable inventory.

Success behavior is unchanged from the earlier cross-workflow action: every
observed, nonignored check must complete, skipped and neutral checks pass, and
two consecutive complete polls must report the same check names. Pending-only
inventories keep waiting. The polling interval defaults to 20 seconds, after an
initial 45-second registration delay.

```yaml
permissions:
  checks: read
jobs:
  ci-ok:
    runs-on: sylphx-linux-control
    steps:
      - uses: SylphxAI/.github/.github/actions/ci-ok-polling@<full-release-sha> # main
        with:
          timeout-minutes: "110"
          ignore: advisory-lint
```

Existing callers only change the action path and immutable SHA. `job-name`
(default `ci-ok`) excludes the calling check, `ignore` lists advisory checks,
and `all-apps` optionally includes check runs outside GitHub Actions.

## Completion boundary limitation

A stable check inventory cannot prove that a workflow registered every required
check. This action intentionally preserves the legacy success behavior; it
does not repair that limitation. Use the strict dependency-based verdict when
migrating a repository's completion boundary. Do not adopt this legacy action
for new workflows.
