# ADR-34: Public Skills External Admission

## Status

Accepted in PR #34. Target-specific identity and snapshot clauses are
superseded by the cleanroom-ratchet ADR introduced with the current change.

## Decision record

- Date: 2026-07-11
- Decision owner: SylphxAI
- Original pull request: <https://github.com/SylphxAI/.github/pull/34>
- Current amendment: [`ADR-36-public-skills-cleanroom-control-plane.md`](./ADR-36-public-skills-cleanroom-control-plane.md)
- Specification: [`../specs/public-skills-external-admission.md`](../specs/public-skills-external-admission.md)

## Context

PR #34 established that the target repository cannot own the only check that
decides whether its own history, allowlist, provenance, and workflow bytes are
acceptable. A candidate that can edit both payload and judge controls its own
admission.

The first target snapshot was later quarantined. Its historical identifiers are
not repeated as current desired state. The amendment ADR and source-owned policy
hold the replacement target facts.

## Decision

`SylphxAI/.github` owns the target-scoped required workflow, inert-data
validator, and declarative snapshot policy:

- `.github/workflows/public-skills-admission.yml`
- `scripts/public-skills-admission.mjs`
- `policies/public-skills-admission.json`

The organization ruleset binds the workflow by source repository ID, path, and
an exact source commit. The workflow runs for `pull_request` and
`merge_group`, while a numeric repository selector ensures source-repository
pull requests skip the target-only job and repository renames cannot escape it.

The workflow checks out complete candidate history without persisted
credentials, separately checks out the source repository at
`github.workflow_sha`, and executes only the source-owned validator.
Candidate package managers, hooks, scripts, actions, binaries, and interpreters
are never invoked.

The policy pins target numeric/node identity; every approved commit, tree,
ordered parent list, ref and target; the exact eight public skill IDs and
provenance classes; every HEAD path and SHA-256 digest; executable exceptions;
generic public-boundary markers; and secret signatures. Every ordinary
reachable commit must be an exact approved graph record. Annotated tags and
unknown branches are forbidden.

GitHub-generated event commits are the only dynamic exception. At most one
unknown commit may exist; it must be the event HEAD, carry the pinned baseline
tree, use the source-approved parent set for the explicit event, and appear only
through a narrow pull-request or merge-queue ref.

## Update and ratchet protocol

1. Prepare and independently audit a private candidate.
2. Update the source-owned policy and adversarial fixtures through the protected
   `SylphxAI/.github` delivery path.
3. Merge the source change and ratchet the organization rule to that exact
   source SHA.
4. Re-run target pull-request, merge-group, and negative canaries.
5. Merge the target only after active enforcement and effective-rules readback.

Changing the target first correctly deadlocks on the old snapshot. Mutable
source refs and manual status forgery are forbidden.

## Consequences

- A target candidate cannot weaken or execute its own judge.
- Exact full-tree pinning makes legitimate public changes source-first.
- Repository names remain lifecycle labels; numeric identity is authoritative.
- Recovery is a protected source/desired-state forward ratchet, never a bypass.
