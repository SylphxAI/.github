# ADR-43: Public Skills provider-owned merge-queue barrier

## Status

Accepted in PR #43.

## Decision record

- Date: 2026-07-12
- Decision owner: SylphxAI
- Pull request: <https://github.com/SylphxAI/.github/pull/43>
- Extends: [`ADR-36-public-skills-cleanroom-control-plane.md`](./ADR-36-public-skills-cleanroom-control-plane.md)
- Specification: [`../specs/public-skills-merge-queue-barrier.md`](../specs/public-skills-merge-queue-barrier.md)

## Context

The external-admission required workflow needs a successful `merge_group`
canary while its organization rule is still in GitHub `evaluate` mode. An
evaluate rule runs but does not require its check, so it cannot itself prevent
the launch candidate from merging before activation evidence is sealed.

The first barrier design tried to solve that gap by creating the barrier rule
as active and having its job call `dequeuePullRequest`. That design is rejected
for three coupled reasons:

1. active-from-creation prevents a safe evaluate-mode proof of the barrier;
2. allowing the barrier to pass when external admission fails creates an
   avoidable authorization race; and
3. `dequeuePullRequest` accepts only a pull-request ID, not the observed queue
   entry or candidate SHA as a compare-and-swap precondition. A stale job can
   therefore dequeue a successor queue entry after regrouping.

The queue must be controlled by GitHub's required-check semantics, not by a
workflow-side mutation with weaker identity.

## Decision

Create a second organization required-workflow rule named
`public-skills-merge-queue-barrier`. It is source-pinned to
`.github/workflows/public-skills-merge-queue-barrier.yml` in repository ID
`1091169653`, targets only repository ID `1297840366` and its default branch,
has no bypass actors, and follows a protected `evaluate` to `active` lifecycle.
It never modifies repository ruleset `18817644`.

The barrier job is read-only. It has `actions: read`, `checks: read`,
`contents: read`, and `pull-requests: read`; it has no write permission, no
GitHub App credential, and no queue mutation API. It checks out only the
protected source repository at `github.workflow_sha` and never executes target
candidate bytes.

Pull-request events validate exact target, same-repository, base, head,
synthetic merge and GraphQL identities, then pass without observing admission
state. Merge-group events validate the provider queue ref, exact candidate,
base/main, queue-entry and pull-request identities, then wait for exactly one
source-pinned external-admission check and its exact run and job.

The merge-group decision is conjunctive:

| Exact external check | Exact external rule, target-effective on two identical reads | Barrier result |
| --- | --- | --- |
| `success` | `active` | pass |
| `failure` | any | fail |
| `success` | absent, `evaluate`, or changed between reads | fail |
| missing, duplicate, non-terminal, foreign producer/run/job/SHA, drift, or provider error | any | fail closed |

Every failing result leaves queue ownership to GitHub. The barrier never tries
to identify or dequeue a current or successor entry. When the barrier rule is
active, GitHub's required-check handling rejects and removes the failing merge
group under provider-owned queue state. Provider dequeue/readback is rollout
evidence, not an action performed by this workflow.

## Activation sequence

The target remains private and launch remains blocked throughout this sequence.

1. Merge the source workflow, controller and policy.
2. Publish a canonical Doctrine follow-on record that owns the barrier rule's
   exact source SHA, provider ID, lifecycle, active/effective readback and
   recovery state. The existing protected executor and fixed lock are the only
   writer.
3. Reconcile the barrier rule in `evaluate`, with no bypass.
4. Enqueue a controlled empty or same-tree canary pull request. Its external
   admission check succeeds, while the barrier check intentionally fails
   because external admission is not active/effective. Because the barrier is
   still evaluate-only, GitHub may merge this no-product-change canary. Capture
   the PR run, merge-group run, rule suite and merged provider state as the
   barrier's evaluate evidence.
5. Ratchet the barrier rule to `active` under the same executor, lock,
   post-readback and provider-attestation boundary.
6. Enqueue a second controlled empty or same-tree canary while external
   admission remains evaluate. The external check succeeds and the now-required
   barrier fails. Capture GitHub-owned rejection/dequeue evidence proving the
   candidate did not merge; the barrier job performs no mutation.
7. Complete the external-admission evidence set and ratchet its exact rule to
   active.
8. Enqueue a fresh canary. Both exact checks must succeed and the barrier must
   pass without any mutation. A separate negative control must make both the
   external check and barrier fail.

No canary result may be reused across rule revisions, source SHAs, candidate
SHAs or provider rule-suite IDs.

## Current delivery boundary

This source slice does not create, update or authorize the barrier organization
rule. No canonical Doctrine follow-on contract exists yet, so the rule is not
Git-owned and rollout is blocked. Manual console or OAuth/API creation would
create split-brain authority and is forbidden. Local tests prove only the
future runtime behavior; they do not prove that a provider rule exists, is
active, or is effective.

## Recovery

If external admission is absent, evaluate, downgraded, repinned, bypassed or
otherwise drifts, the active barrier fails every new merge group and GitHub
owns rejection/dequeue. Recovery repairs the owning Doctrine record through
the existing fenced executor and requires fresh source-SHA, effective-rule and
canary evidence before the barrier can pass again.

If the barrier rule itself drifts, launch stays blocked outside the queue until
Doctrine reconciliation restores and re-proves it. Recovery-only enforcement
downgrades remain explicit Doctrine transitions; there is no workflow-side
repair, re-enqueue or queue mutation.

## Consequences

- A stale workflow run cannot remove a successor queue entry.
- The job token is read-only and carries no queue-write authority.
- External failure always fails the barrier, producing conservative duplicate
  failures instead of a permissive single-failure optimization.
- Evaluate and active behavior are both proven before launch.
- GitHub provider state, rather than workflow narrative, proves rejection and
  dequeue.
- Source updates require a source-first merge and Doctrine SHA ratchet.

## Alternatives considered

### Workflow-side `dequeuePullRequest`

Rejected. Pull-request ID is not a candidate- or queue-entry-CAS boundary, so a
stale job can act on successor state.

### Active barrier from creation

Rejected. It skips evaluate-mode proof and creates an activation deadlock for
the first controlled canary.

### Pass the barrier when external admission fails

Rejected. Both predicates are mandatory; optimizing for one visible failed
check weakens the authorization model.

### Target-owned hold or manual queue pause

Rejected. The target could change its own hold, and an operator timing step is
not deterministic no-human delivery.

## Primary-source evidence

- GitHub Docs, `available-rules-for-rulesets.md` at docs commit
  `f19a0135b2fe88a1ca17efbadb1d2bf14eb332b4`: evaluate-mode required workflows
  run without becoming required and can later be switched active.
- GitHub Docs, `troubleshooting-rules.md` at the same commit: ruleset workflows
  require `merge_group`, must not use `cancel-in-progress`, and should be
  source-pinned.
- GitHub's GraphQL schema exposes `dequeuePullRequest` by pull-request ID only;
  it provides no candidate-SHA or observed queue-entry CAS. This is evidence
  for removing the mutation surface, not for using it.

## Verification

```sh
node --test tests/public-skills-merge-queue-barrier.test.mjs
node --check scripts/public-skills-merge-queue-barrier.mjs
actionlint .github/workflows/public-skills-merge-queue-barrier.yml
git diff --check
```

Provider completion remains blocked until the canonical Doctrine contract is
published and integrated into the existing executor/lock. It then requires
evaluate-canary merge evidence, active required-failure plus GitHub-owned
rejection/dequeue evidence, external activation evidence, and a final
active/active read-only passing canary.
