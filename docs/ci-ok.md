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
      checks: read
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
Contribution metadata, added-commit secret scanning and the required
[merge-lane check](merge-lane.md) remain part of the verdict. The latter reads
workflow source at the tested commit without a caller checkout and refuses
merge-group jobs on a plain build pool when a same-size merge twin is served.

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

## Exact-head review on the merge queue

On `merge_group`, `ci-ok` requires every PR whose current title starts with
`[review]` to have a completed `Sylphx PR Review` Check Run from App `1299545`,
with conclusion `success`, on the exact PR head used to build the group.
There is no override. Missing, pending, neutral, failed or cancelled reviews
refuse the entire group. A success on an older head does not authorize a new
head. If several canonical runs exist, the newest run determines the verdict.

The existing contribution-policy resolver reads paginated merge-queue entries
and intersects their synthetic commits with the immutable group commit range.
It checks original PR heads and refuses incomplete membership. Entries that
merged while a later group builds are resolved through commit/PR associations.
It does not depend on `autoMergeRequest` or infer membership from the queue ref.
Read errors, incomplete Check Run pagination, and head or title movement fail
closed. Failures name the PR number, full head SHA and observed review state.
The caller's token now also needs `checks: read`.

Pull-request-event CI is unchanged: review remains asynchronous there. Non-
`[review]` PRs need no review. This applies even when `separate-pr-metadata` is
true; that option does not disable merge-group review admission. Callers only
receive this enforcement after adopting the new immutable action pin.
