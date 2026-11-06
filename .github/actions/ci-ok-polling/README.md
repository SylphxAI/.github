# Legacy cross-workflow verdict

This action supports legacy repositories whose checks span independent
workflows. Prefer the strict [`ci-ok`](../ci-ok) action with explicit `needs:`
dependencies; that dependency graph is the target completion boundary.

A completed failed, cancelled, timed-out, action-required, startup-failed or
stale check fails immediately on the next successful poll, even while other
checks remain queued or running. Failure does not wait for a stable inventory.
Failed empty gating workflow suites and completed non-successful `required`
checks also fail immediately.

Success retains the last polling revision's protections: every observed,
nonignored check must complete, skipped and neutral checks pass unless
explicitly required, and two consecutive complete polls must report the same
check names. Pending-only inventories keep waiting. Superseded workflow
attempts do not count; startup rejections, missing required checks and an empty
inventory fail closed. API budget exhaustion past the deadline fails closed,
and a low budget slows polling. The default interval is 20 seconds (60 on a
low budget), after an initial 45-second registration delay.

```yaml
permissions:
  actions: read
  checks: read
  contents: read
  pull-requests: read
jobs:
  ci-ok:
    runs-on: sylphx-linux-control
    steps:
      - uses: SylphxAI/.github/.github/actions/ci-ok-polling@<full-release-sha> # main
        with:
          timeout-minutes: "110"
          ignore: advisory-lint
```

Existing polling callers change the action path and immutable SHA. Keep their
existing inputs: `job-name` (default `ci-ok`) excludes the calling check,
`ignore` lists advisory checks, `required` names checks that must succeed,
`all-apps` includes check runs outside GitHub Actions, and
`allow-none-on-pull-request` permits an empty inventory only on pull requests.
Contribution metadata and added-commit secret scanning remain enabled.

## Completion boundary limitation

A stable check inventory cannot prove that a workflow registered every required
check. This action preserves the legacy completion boundary; it does not infer
an absent check that the caller omitted from `required`. Use the strict
dependency-based verdict when migrating a repository's completion boundary.
Do not adopt this legacy action for new workflows.
