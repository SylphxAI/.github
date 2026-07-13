---
slug: public-skills-rename-lifecycle-fence
---

# ADR-DRAFT: Public Skills rename lifecycle fence

## Status

Draft. The filename and heading must receive the allocator-backed pull-request
number before merge.

## Decision record

- Date: 2026-07-13
- Decision owner: SylphxAI
- Extends: [`ADR-36-public-skills-cleanroom-control-plane.md`](./ADR-36-public-skills-cleanroom-control-plane.md)
- References: [`ADR-43-public-skills-merge-queue-barrier.md`](./ADR-43-public-skills-merge-queue-barrier.md)
- Specification: [`../specs/public-skills-external-admission.md`](../specs/public-skills-external-admission.md)

## Context

GitHub check-run job URLs contain the repository name observed when the run was
created. The target keeps one immutable numeric/node identity while moving from
its controlled cleanroom name to its final name. Requiring only the final name
deadlocks valid pre-rename evidence; accepting either name independently lets
mixed or stale evidence cross lifecycle phases.

A repository rename can also occur between an initial identity read and a
ruleset write. GitHub provides no conditional organization-ruleset update that
atomically binds the repository name. Repeated reads detect persistent change,
but the final read/write interval is coordinated only when every supported
writer shares one fence.

## Decision

The schema-v5 executor accepts exactly the two controlled repository names in
check-run URLs. Every URL must be the exact GitHub Actions job URL for its
sealed run and job. One evidence record must be homogeneous: all staging-name
URLs or all final-name URLs. Mixed names, foreign/lookalike names, partial URLs,
redirect-shaped variants, and mismatched run/job identities fail closed.

While the live provider name is staging, only homogeneous staging evidence is
valid. After the provider name is final, homogeneous staging evidence remains
valid only for a complete verified transition, with the explicit paired
recovery state retained as the bounded emergency exception. Homogeneous final
evidence is also valid after that complete transition. The immutable provider
repository ID and node ID are always cross-bound with the admitted live name;
the name never substitutes for numeric identity.

The executor snapshots the complete provider target identity and requires every
subsequent target read to equal it. It re-runs the lifecycle check around live
observations, before every no-write return, inside the held fence immediately
before a provider mutation, and across post-write and effective-rule readback.
Any observed name transition fails closed.

Every schema-v5 organization-ruleset mutation requires the existing fixed
annotated-tag fence in source repository ID `1091169653` at
`refs/tags/sylph-locks/public-skills-ruleset-executor`. The exact repository,
ref, executor commit, acquired/pending lifecycle, tag object, and live ownership
must be verified immediately around the final target snapshot.

The future schema3 repository-rename writer MUST acquire the same fence before
changing the target name and hold it through provider readback. Until that
writer implements and proves this shared-fence contract, repository rename and
public cutover remain blocked. Executor-side rereads alone do not close the
final read/write race against a non-cooperating writer.

The current target graph, refs, file allowlist, and file digests remain owned
only by `policies/public-skills-admission.json`. This ADR records the decision
and does not duplicate that mutable snapshot.

## Consequences

- Valid historical staging evidence survives the controlled rename only after
  the complete verified or explicit paired-recovery boundary.
- A record cannot combine evidence captured under both repository names.
- Persistent mid-run name changes cannot produce a successful no-op report or
  an authorized ruleset request.
- Full rename/write serialization is a cross-writer property; out-of-band
  provider administrators remain incident authority outside this contract.
- Source-policy changes remain source-first and require the existing admission
  and lifecycle evidence gates.

## Verification

The executable contract and freshness routes live in:

- `scripts/public-skills-ruleset-executor.py`;
- `tests/test_public_skills_ruleset_executor.py`;
- `policies/public-skills-admission.json`; and
- `docs/specs/public-skills-external-admission.md`.

Required local gates are:

```sh
python3 -B tests/test_public_skills_ruleset_executor.py
PUBLIC_SKILLS_CANDIDATE=/path/to/fresh-cleanroom-clone \
  node --test tests/public-skills-admission.test.mjs
node --test tests/public-skills-merge-queue-barrier.test.mjs
node --test tests/groundatlas-boundary.test.mjs
git diff --check
```
