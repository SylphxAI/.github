# ADR-34: Public Skills External Admission

## Status

Accepted (PR #34)

## Decision record

- Date: 2026-07-11
- Decision owner: SylphxAI
- Pull request: <https://github.com/SylphxAI/.github/pull/34>
- Work item: `SylphxAI/skills-private#115`
- Specification: [`../specs/public-skills-external-admission.md`](../specs/public-skills-external-admission.md)

## Context

The repository with numeric identity `1297721845` is the fresh-history public
skills foundation. Its staging slug is `SylphxAI/skills-public-staging`; its
final slug is `SylphxAI/skills`. The public repository cannot be the only owner
of the check that decides whether its own history, allowlist, provenance, and
workflow bytes are acceptable. A candidate that could edit both the payload and
the only validator would control its own judge.

The old `SylphxAI/skills` repository has a different numeric identity and must
remain private after becoming `SylphxAI/skills-private`. The public foundation
must never acquire that repository's history, private skill IDs, operational
metadata, or protected procedures.

## Decision

`SylphxAI/.github` owns a target-scoped required workflow, zero-dependency
validator, and declarative policy:

- `.github/workflows/public-skills-admission.yml`
- `scripts/public-skills-admission.mjs`
- `policies/public-skills-admission.json`

The organization ruleset binds the workflow by source repository ID, path, and
an exact source commit. The workflow runs for `pull_request` and `merge_group`,
but its job executes only when `github.repository_id == 1297721845`. Therefore
ordinary `SylphxAI/.github` pull requests skip the target-only job, while an
event for the target identity cannot escape it by renaming the repository.

The workflow checks out the candidate with full reachable history and without
persisted credentials. It separately checks out `SylphxAI/.github` at
`github.workflow_sha`. It executes only the source-owned validator. Candidate
package managers, hooks, scripts, actions, binaries, and interpreters are never
invoked.

The policy pins the target numeric and node IDs; every approved commit ID,
tree ID, and ordered parent list; every allowed ref and target; the exact eight
public skill IDs and provenance classes; every allowed HEAD path and SHA-256
digest; executable-file exceptions; content-boundary markers; and secret
signatures. Every non-synthetic reachable commit must be an exact approved
record. Annotated tags and unknown branches are forbidden.

GitHub-generated candidate commits are the only dynamic exception. At most one
unknown commit may exist, it must be the event HEAD, `github.event_name` must be
`pull_request` or `merge_group`, its tree must equal the pinned baseline tree,
and its ordered parents must equal a source-approved parent set for that exact
event. The pull-request shape is the two-parent `[base main, approved PR head]`
merge ref. Merge-group policy also admits the one-parent `[base main]` shape
used by squash queues. Any dynamic ref must match the narrow pull or
`gh-readonly-queue` patterns and point to that exact event HEAD.

The validator scans every reachable commit, tree entry, path, ref, tag object,
and unique blob; rejects graph or ref drift, extra roots, private commits,
deleted-only files, symlinks, submodules, binary/LFS payloads, unsafe paths,
unapproved executable modes, secrets, private markers, and semantic
admissions/catalog drift; and emits `public-skills-external-admission/pass`
plus a JSON evidence artifact.

## Update and ratchet protocol

Exact full-tree pinning is deliberate. A legitimate public-candidate change,
including the final staging-to-canonical identity edits, follows this order:

1. Prepare the target candidate and compute its exact commit/tree/parent graph,
   refs, HEAD path set, and SHA-256 file digests without merging it.
2. Update this source-owned policy and its adversarial fixtures through the
   normal `SylphxAI/.github` delivery path.
3. Merge that source change and ratchet the organization required-workflow
   binding to the resulting immutable source SHA.
4. Re-run the target pull-request and merge-group candidate against the new
   source SHA; only then may it merge.

Changing the target first would correctly deadlock on the old snapshot.
Pointing the ruleset at a branch or mutable tag would let source bytes change
without a GitOps decision and is forbidden.

## Consequences

- A target candidate cannot weaken or execute its own judge.
- Repository renames do not change the identity gate.
- Any public byte change is a two-repository, source-first ratchet. This adds a
  deliberate coordination step in exchange for a fail-closed commercial and
  supply-chain boundary.
- The workflow is target-specific organization governance, not the source of
  truth for the target product. Skills, catalog claims, releases, and runtime
  delivery evidence remain owned by the target repository.
- Recovery before a target merge is source-policy/ruleset rollback. Recovery
  after a bad merge is a source-ratchet plus target forward fix or source
  revert; manual status forgery is not a recovery path.
