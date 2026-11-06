# Legacy cross-workflow verdict

This action supports legacy repositories whose required checks span independent
workflows. Prefer the strict [`ci-ok`](../ci-ok) action with explicit `needs:`
dependencies; that dependency graph is the target completion boundary.

A completed failed, cancelled, timed-out, action-required, startup-failed or
stale check fails immediately on the next successful poll, even while other
checks remain queued or running. Failed empty gating workflow suites also fail
immediately. Success still requires two consecutive complete, passing polls.
Skipped and neutral checks pass unless listed in `required`.

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
          expected: build,tests,optional-lint
          required: build,tests
```

`expected` must name the complete set of gated **check display names**, including
conditionally skipped jobs, for this event. It is mandatory and cannot include
the gate itself or ignored checks. Missing expected checks keep the verdict
pending until they register or the timeout fails closed. An incomplete inventory
never establishes that an expected check ran. If path filters omit a workflow
entirely, the caller must derive the expected set for that change without
omitting any required validation. Observed nonignored checks still count even
when absent from `expected`.

Keep existing advisory exclusions in `ignore`; the calling check defaults to
`ci-ok` and can be changed with `job-name`. Only GitHub Actions checks count
unless `all-apps` is true. Superseded workflow attempts do not count, and API
budget exhaustion past the deadline fails closed. `timeout-minutes` defaults
to 110; the default poll interval is 20 seconds (60 on a low API budget).

This action cannot infer checks that a caller omits from its declared expected
set. Keep that declaration aligned with the workflow definitions, just as the
strict verdict requires its caller to declare every required dependency.
