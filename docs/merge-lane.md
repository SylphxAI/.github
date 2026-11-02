# Required merge-lane routing

A merge group must not wait behind pull-request builds when its organization
already serves a same-size merge pool. Both shared verdict actions (`ci-ok`
and `needs-pass`) enforce R8 before they can return success. Custom verdicts
can include the standalone action in their **existing required job**:

```yaml
permissions:
  contents: read
steps:
  - uses: SylphxAI/.github/.github/actions/merge-lane@<full-release-sha>
```

Run the verdict on `pull_request` and `merge_group`, on
`sylphx-linux-control`. No caller checkout is needed. When the tested commit already exists in the
caller checkout, source is read directly from its immutable Git objects, not
mutable worktree files, so no source API read is needed. Python 3 and pip are
required; PyYAML 6.0.3 is installed into a runner-temporary directory.
Publishing a shared action does not update old immutable pins or make an
optional job required. Adoption must update the pin or add the standalone step
and read the repository's real required-check chain back.

## What the check proves

The check reads the `.github/workflows` tree from the tested immutable commit,
verifies the commit identity and visibility, and refuses unreadable, binary,
truncated or symlink workflow sources. Native PR checks use `GITHUB_SHA` (the
tested merge revision), not the PR head. Merge groups use their `head_sha`.
A code-validation dispatch first validates its `code-*` target through the
shared contribution-policy contract, then inspects `code-merge`; an ordinary
manual dispatch is not a pre-merge verdict.

The shared parser follows local reusable workflows with caller conditions,
inputs/defaults, and matrix dimensions/include/exclude. It evaluates known
merge-event and visibility branches; unknown conditions remain runnable.
Both block and flow YAML work. Job conditions can exclude merge groups; step
conditions cannot exclude the job's runner allocation. A fallback to a plain
pool still fails even when another branch names a merge pool. The selected
merge twin must keep the original size class.

[`policy/merge-runner-labels.json`](../policy/merge-runner-labels.json) records
the per-organization serving contract and its immutable upstream source.
Only a served twin is required. A plain class with no served twin passes with
a notice, while selecting an unserved twin fails. Routing never provisions a
scale set, changes its quota or silently downsizes a build.

The fleet audit, workflow-lint and required actions share
[`scripts/merge_lane.py`](../scripts/merge_lane.py). R8 remains blocking in
workflow-lint's `report-only` mode; that mode only softens legacy timeout and
failure-propagation findings.

## Exclusions and boundaries

Delivered customer repositories (`sylphx_delivery=delivered`) and repositories
in the policy's `hands-off` class are excluded. The property read retries at most three times. If still unreadable, it warns
and inspects source as non-delivered: clean source passes and a violation
still fails. Hands-off exclusion happens before API reads. Source reads also
retry at most three times, except explicit rate limits are not retried.
An unreadable source refuses the required verdict with a distinct API/source
failure annotation, not a lane-violation claim. At the required-check boundary
this is a failed job and the queue waits; it never silently becomes success.
The optional workflow-lint customer-protection wrapper still skips on
unreadable properties; that skip is not proof of coverage. Only an explicit, complete and unexpired
`static-runner-policy` entry waives required R8. Other audit-only exemptions
do not waive it. No static-runner-policy entries are currently needed.

The job token can read its own repository, not arbitrary private reusable
workflow repositories. This required action checks the complete caller tree
and its local call graph; it does **not** claim to inspect external reusable
source with an unavailable credential. External reusable providers need their
own required guard, and external pin adoption needs source verification.
The 2026-11-02 fleet census independently expanded 63 external references from
44 immutable source bundles and found no additional plain merge paths; that
is a dated census, not future external-pin enforcement.

This rule checks merge-group allocation. It does not classify arbitrary shell
commands as deployment jobs or provision shipping capacity. Shipping workflow
routing must also preserve the same-size serving contract: use a static merge
pool only when the job truly cannot run on `pull_request`; otherwise use an
event-appropriate selector.

## Tests

```sh
python3 -m unittest discover -s tests -p 'test_merge_lane*.py'
python3 -m unittest discover -s tests -p test_workflow_contract.py
python3 -m unittest discover -s tests -p test_audit_optimistic_merge.py
```

Tests cover executable regressions, per-organization unavailable twins,
visibility, wrong event/size, fallback branches, local reusable calls,
numeric/boolean expression semantics, immutable tree binding, dispatches,
read failures and the required-action wiring.
