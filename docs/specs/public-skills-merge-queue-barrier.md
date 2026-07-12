# Public Skills provider-owned merge-queue barrier specification

Decision record:
[`ADR-DRAFT-public-skills-merge-queue-barrier.md`](../adr/ADR-DRAFT-public-skills-merge-queue-barrier.md).
The ADR filename must receive its allocator-backed pull-request number before
merge.

## Contract identity

| Fact | Required value |
| --- | --- |
| Source repository | `SylphxAI/.github`, ID `1091169653`, node `R_kgDOQQntdQ` |
| Target repository | ID `1297840366`, node `R_kgDOTVt47g` |
| Allowed target names | `SylphxAI/skills-public-cleanroom`, `SylphxAI/skills` |
| Target branch | default branch `main` |
| Barrier workflow | `.github/workflows/public-skills-merge-queue-barrier.yml` |
| Controller | `scripts/public-skills-merge-queue-barrier.mjs` |
| Runtime policy | `policies/public-skills-merge-queue-barrier.json`, schema 2 |
| Required job | `public-skills-merge-queue-barrier/pass` |
| Guarded ruleset | organization ruleset `18831380`, `public-skills-external-admission` |
| Guarded workflow | `.github/workflows/public-skills-admission.yml` at `f29ea0026e7e018f1cd8777983e548b90c23b569` |
| Guarded job | `public-skills-external-admission/pass` |

The policy owns executable runtime constants. Doctrine owns desired/live
organization rule IDs, source SHAs, lifecycle and transition evidence. The
barrier lifecycle is exactly `evaluate` then `active`, with no bypass actors.

## Delivery state

The barrier organization rule is not yet Git-owned because its canonical
Doctrine follow-on schema/record has not been published. This slice performs
no provider mutation and authorizes no manual console or OAuth/API writer.
Launch remains blocked until the canonical record binds the provider rule ID
and source SHA, the existing protected executor reconciles it under the
existing fixed lock, and lifecycle plus effective/recovery evidence is sealed.

## Workflow contract

The workflow must:

- trigger on `pull_request` and `merge_group`, without path filters or
  `cancel-in-progress`;
- skip only source repository ID `1091169653`;
- use `ubuntu-24.04`, a bounded timeout and full-SHA action pins;
- grant only `actions: read`, `checks: read`, `contents: read`, and
  `pull-requests: read`;
- checkout only `SylphxAI/.github` at `github.workflow_sha`, with depth one and
  persisted credentials disabled;
- never checkout or execute target candidate code;
- expose no queue mutation, write token, App secret or re-enqueue path; and
- upload the JSON report on success or failure.

GraphQL is used only for the named read query
`PublicSkillsBarrierSnapshot`. The controller contains no GraphQL mutation and
never calls `dequeuePullRequest`.

## Pull-request state

For `pull_request`, the controller requires:

- runtime, event and GraphQL repository identities equal the exact numeric,
  node and admitted current/final target;
- provider ref `refs/pull/<number>/merge` binds the payload number;
- base and head repositories both equal the target;
- base ref is `main`, and head ref/SHA are provider-shaped;
- `github.sha` equals `pull_request.merge_commit_sha`; and
- GraphQL shows the same open, unmerged PR, base, head and default branch.

The result is a read-only identity pass. Admission gating is evaluated only on
`merge_group`.

## Merge-group state

For `merge_group.checks_requested`, the controller requires:

- base ref `refs/heads/main`, exact full base/head SHAs and runtime candidate;
- head ref `refs/heads/gh-readonly-queue/main/pr-<positive-number>-<hex>`;
- the referenced open same-repository pull request is currently in the queue;
- current default-branch SHA and queue base commit equal the event base SHA;
  and
- GraphQL head/base repository and PR identities match the event.

No later action uses only the PR ID. A regrouped successor cannot be mutated by
this job because the job has no mutation capability.

## External-check observation

The controller polls at most 120 times at five-second intervals for exactly one
check named `public-skills-external-admission/pass` on the exact candidate SHA.
It must be completed with `success` or `failure` and owned by GitHub Actions App
ID `15368`.

The details URL, run and job must bind the admitted target, run ID, candidate,
`merge_group` event, exact required job and source workflow path. A source SHA
suffix, when present, must equal the pinned guarded source SHA. Absence waits;
duplication, neutral conclusions, foreign producer/run/job/head/event/source or
incomplete pagination fail closed.

Only `success` satisfies the barrier. A valid `failure` is evidence of an exact
external rejection and must also make the barrier fail.

## Effective-rule observation

The controller reads target-effective rulesets by numeric repository ID. The
guarded external rule is admitted only when exactly one inherited rule has ID
`18831380` and detailed readback proves:

- organization source `SylphxAI`, branch target, enforcement `active`;
- no bypass actors and `current_user_can_bypass: never`;
- exact default-branch and, when projected, repository selector; and
- one workflow rule with exact source repository ID, path, ref, SHA and
  `do_not_enforce_on_create: false`.

The active/effective read is repeated. Both reads must be active and have the
same canonical digest. Absence is a normal unsatisfied predicate; a same-name
foreign ID or detailed semantic mismatch is drift and fails immediately.

## Decision contract

The merge-group check passes only when both predicates are true:

1. exact external check conclusion is `success`; and
2. exact external rule is active/effective on two identical reads.

Every other state returns a failing barrier report or fails closed on contract
error. The report always states:

```json
{
  "queueMutation": {
    "owner": "github-provider",
    "attempted": false
  }
}
```

The CLI exits nonzero for a well-formed rejected report as well as for an
unexpected contract/provider error. A rejected report therefore remains rich
evidence while the required check is red.

## Provider-owned lifecycle evidence

The target remains private and product launch remains blocked while evidence is
collected.

1. Reconcile the barrier rule in `evaluate` through the canonical Doctrine
   record, existing executor and fixed lock.
2. Enqueue one controlled empty or same-tree canary. External admission must
   succeed; the barrier must fail because external admission is not yet
   active/effective. Since the barrier is evaluate-only, GitHub may merge this
   no-product-change canary. Capture exact PR/run/job/rule-suite/merge evidence.
3. Ratchet the barrier rule to `active` with exact post/effective readback.
4. Enqueue a second controlled empty or same-tree canary before external
   activation. External admission succeeds; the required barrier fails.
   Capture GitHub-owned rejection/dequeue and unchanged-main evidence. No
   workflow mutation is part of this proof.
5. Activate external admission through its own evidence protocol.
6. Enqueue a fresh canary and prove both checks pass read-only.
7. Run the negative control and prove both checks fail.

The two lifecycle canaries must use distinct PRs, candidate SHAs and rule-suite
IDs. They cannot substitute for one another or for the external-admission
synthetic rule-suite proof.

## Evidence and recovery

The report records source/target/event identity, initial and pre-decision
snapshot digests, external check/run/job, effective-rule reads, the conjunctive
decision and immutable `mutationAttempted: false`. It never records tokens or
authorization headers.

Provider rollout evidence must separately bind the barrier rule ID and source
SHA, evaluate canary merge, active required-failure, GitHub-owned queue
rejection/dequeue, final active/active pass, and recovery state.

If external admission later drifts, the active barrier fails and GitHub owns
queue removal. If the barrier itself drifts, launch remains blocked outside the
queue until the same Doctrine executor/lock restores and re-proves it. The
controller never repairs rules, mutates queue state or re-enqueues a PR.
