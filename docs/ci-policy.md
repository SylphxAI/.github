# Short CI policy check

`ci-policy` runs the shared [contribution metadata check](contribution-policy.md)
followed by [added-commit secret scanning](secret-scan.md). It uses the same
programs, token environment variables and `--fetch --pre-merge-only` scanner
flags as the existing shared verdict actions. A failure in either step fails
the calling job; a metadata failure stops the scanner from starting.

It does **not** wait for other checks, evaluate job dependencies, enforce
merge-lane routing, or publish an aggregate verdict. Keep the repository's
required CI verdict and all existing required checks. A successful policy check
alone does not establish that compilation or tests ran.

```yaml
jobs:
  ci-policy:
    runs-on: sylphx-linux-control
    timeout-minutes: 10
    permissions:
      contents: read
      pull-requests: read
    steps:
      - uses: SylphxAI/.github/.github/actions/ci-policy@<full-release-sha> # main
```

Run it on both `pull_request` and `merge_group`. The immutable Git ranges and
current PR metadata are acquired with the read-only job token; a caller checkout
is not needed. No `checks: read` or `actions: read` permission is needed for check
inventory polling because this action does none. Unreadable metadata or Git
objects, invalid exceptions and scanner failures retain the existing fail-closed
behavior. The scanner never executes the target repository's code. On other
events, each shared program keeps its existing event handling; a skipped
pre-merge scan is not evidence that the head passed policy.

If the policy job also validates local dependency results or selected lanes,
retain that proof and its existing `needs` and conditional behavior in the
caller. Do not remove an old verdict until its replacement is observed and
required on the exact PR and merge-group heads. This action adds a separate
policy building block; it changes no existing shared action or caller pin.

The `contribution-scope` input defaults to `all`. If a caller already has a
separately required PR metadata workflow that runs on title/body edits, set
`contribution-scope: commits` in the code job. This uses the contribution
validator's existing commits-only mode; commit identity and secret scanning
remain required. Otherwise keep `all` and ensure metadata edits rerun the policy
job. A text correction must not require a new code commit to recover CI.

The repository's Project control workflow invokes the local action with
`contribution-scope: commits` on PRs, merge groups and main pushes. The separate
required PR metadata workflow handles mutable text, including edited events.
The required tests job supplies the live consumer receipt in addition to the
focused shell-contract tests.
