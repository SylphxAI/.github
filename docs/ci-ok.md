# Required job verdict

A green `ci-ok` means every declared required job ran and succeeded. The gate
uses GitHub's job dependency graph, with no check-inventory polling or settle
timeout. An absent, skipped, cancelled, failed or unfinished required job cannot
produce a successful verdict.

```yaml
jobs:
  build:
    # Build steps...
  tests:
    # Test steps...
  ci-ok:
    needs: [build, tests]
    if: ${{ always() }}
    runs-on: sylphx-linux-control
    timeout-minutes: 5
    permissions:
      contents: read
      pull-requests: read
    steps:
      - uses: SylphxAI/.github/.github/actions/ci-ok@<full-release-sha> # main
        with:
          needs: ${{ toJSON(needs) }}
          required: build,tests
```

`required` contains YAML **job IDs**, not check display names. It is mandatory
and nonempty, as is `needs`. Every required job must appear directly in the
verdict job's `needs:`. A missing required ID fails even if all supplied results
are green. A successful cleanup job does not establish that its dependencies
succeeded. A required lane must preserve failures rather than mask them with
`continue-on-error` or an unconditional success command.

The action uses the existing `needs-pass` result policy. Explicitly required
jobs must succeed. Other dependencies may skip for intentional path filtering,
but their failures, cancellations and unknown results still fail the gate.
Contribution metadata and added-commit secret scanning remain part of the
verdict.

GitHub keeps the verdict pending while a needed job runs. `if: always()` lets
it report failure after a dependency failed or skipped. A cancelled required
dependency never passes. If the verdict itself is cancelled, it does not publish
success. The required check must be this verdict job.

## Several workflows

Expose each required workflow through `workflow_call` and call it as a job in
the gate workflow. Include the calling job in `ci-ok.needs` and `required`.
Where that workflow contains conditional jobs, give it its own strict verdict
so a skipped required inner job cannot become a successful reusable-workflow
result. Preserve existing pull-request and merge-group triggers as appropriate.
A workflow that remains independent cannot be awaited through `needs:`.

An existing required external status, such as a preview build, also needs an
explicit completion boundary. Retain a proof job that requires that named
status to succeed on the exact head, and include the proof job in the gate.
An absent or still-running external status cannot establish success. Replacing
the inventory action must preserve this obligation.

Reusable workflows inherit the caller's `github.workflow` context. Give their
concurrency groups distinct identities or remove a redundant callee group;
using the caller's cancellation group can cancel the caller itself.

## Upgrade from the polling action

Upgrade the workflow and immutable shared pin in the same pull request. Add
all required dependencies, `if: always()`, `needs: ${{ toJSON(needs) }}` and the
explicit `required` job IDs before adopting this version. Old immutable pins
keep their previous behavior until that caller is migrated.

The old `ignore`, `job-name`, `allow-none-on-pull-request`, `timeout-minutes`
and `all-apps` inputs are removed. An empty required set is never an opt-in
success. Advisory workflows stay outside the declared required set; they do
not establish that an absent required job ran.

Two stable inventory reads do not prove completeness. The former action could
see one green check twice, return success, and miss a required failure that
registered on the next read. Increasing its settle time cannot eliminate that
race. The dependency graph supplies the completion boundary.

GitHub's primary contracts: [job dependencies and `always()`](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#jobsjob_idneeds),
[the direct `needs` context](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts#needs-context),
and [reusable-workflow concurrency](https://docs.github.com/en/actions/reference/reusable-workflows-reference).
