# SylphxAI GitHub Organization Configuration

This repository owns SylphxAI organization-level GitHub configuration:
community health files, the organization profile, issue and pull request
templates, brand/company references, repository templates, reusable workflows,
workflow templates, and shared GitHub Actions.

## Lifecycle

- State: `production`
- Layer: `tooling`
- Machine manifest: [`.doctrine/project.json`](./.doctrine/project.json)
- Vendor-neutral project manifest:
  [`project.manifest.json`](./project.manifest.json)

## Goals

- Centralize GitHub organization defaults and repository bootstrap templates.
- Provide reusable workflows and shared actions that repositories consume
  through documented GitHub workflow/action references.
- Own source-pinned organization required workflows where a repository must
  not control the check that judges its own supply-chain boundary.
- Own the source-pinned merge-queue barrier that makes evaluate-mode public-
  skills canaries safe without a target-controlled or operator-timed hold.
- Own the protected executor that reconciles the public-skills organization
  ruleset from Doctrine desired state without executing Doctrine code.
- Keep shared process in one place so repositories do not copy or fork it.

## Non-Goals

- Owning product code, package APIs, runtime behavior, deployments, or release
  evidence for individual repositories.
- Owning another repository's project goal, lifecycle, boundary, or adoption
  state.
- Bypassing consumer repository branch protection, merge queues, required
  checks, or production proof.

## Boundary

This repository owns organization-level GitHub surfaces only. Consumer
repositories use those surfaces through GitHub's public workflow/action/template
and organization-ruleset mechanisms. A narrowly target-scoped required workflow
may live here when external ownership is the security property; product facts
and delivery evidence still belong to the consuming repository.

## Public Surfaces

- `profile/README.md`
- `.github/CODE_OF_CONDUCT.md`
- `.github/CONTRIBUTING.md`
- `.github/SECURITY.md`
- `.github/ISSUE_TEMPLATE/*`
- `.github/PULL_REQUEST_TEMPLATE.md`
- `.github/workflows/adr29-admission.yml`
- `.github/workflows/release.yml`
- `.github/workflows/publish-npm.yml`
- `.github/workflows/groundatlas.yml`
- `.github/workflows/public-skills-admission.yml`
- `.github/workflows/public-skills-merge-queue-barrier.yml`
- `.github/actions/adr29-admission/action.yml`
- `.github/actions/setup-changesets-publisher/action.yml`
- `templates/`
- `brand/`
- `COMPANY.md`
- `policies/public-skills-admission.json`
- `policies/public-skills-merge-queue-barrier.json`
- `policies/public-skills-activation-attestation-ruleset.json`
- `scripts/public-skills-admission.mjs`
- `scripts/public-skills-merge-queue-barrier.mjs`
- `scripts/public-skills-ruleset-executor.py`

The public-skills surface emits the stable target context
`public-skills-external-admission/pass`. Its executable contract and
source-first SHA ratchet are documented in
[`docs/specs/public-skills-external-admission.md`](./docs/specs/public-skills-external-admission.md)
and the admission-mechanics decision is
[`docs/adr/ADR-34-public-skills-external-admission.md`](./docs/adr/ADR-34-public-skills-external-admission.md).
The replacement target identity, clean snapshot, and independent executor trust
split are accepted in
[`docs/adr/ADR-36-public-skills-cleanroom-control-plane.md`](./docs/adr/ADR-36-public-skills-cleanroom-control-plane.md).
The independent activation hold emits
`public-skills-merge-queue-barrier/pass`; its source-owned state machine and
provider-owned queue boundary are specified in
[`docs/specs/public-skills-merge-queue-barrier.md`](./docs/specs/public-skills-merge-queue-barrier.md).
Its organization rule is not yet Git-owned: this source slice authorizes no
manual console or OAuth/API creation, and launch remains blocked until a
canonical Doctrine sibling contract is reconciled through the existing
protected executor and fixed lock with exact ID, source-SHA, active/effective
and recovery evidence.

## Delivery

This repository has a lightweight local GroundAtlas project-control workflow.
It validates `project.manifest.json`, keeps `.doctrine/project.json` as a
Sylphx Doctrine adapter, uploads GroundAtlas reports as evidence only, and runs
a non-mutating syntax check for the shared Changesets publisher.

Changes merged to `main` are consumed directly by GitHub as organization
defaults, reusable workflows, workflow templates, and composite actions. There
is no separate runtime deploy for this repository. Production proof is GitHub
main readback plus successful local project-control CI. When a reusable
workflow or shared action public contract changes, proof must also include
successful consumer use of the changed public surface.

For the public-skills required workflow, production proof additionally needs
the merged source SHA bound in the organization ruleset, successful target PR
and `merge_group` runs for repository ID `1297840366`, and an uploaded JSON
admission report. Exact commit/tree/ref graph and full-tree pinning mean target
history, identity, or content edits must follow a source-policy update and
ruleset-SHA ratchet first. Organization-rule mutations additionally require a
canonical Doctrine-main record, protected executor-main byte identity, a
unique source-owned fenced apply lock, repeated pre-mutation head/live
readback, exact post-mutation/effective-rules readback, and a permanent
provider-hosted activation-attestation ref. The lock claim binds the exact
Doctrine revision, desired payload, planned action, and pre-readback revision.
After the lock is released and absence is confirmed, a deterministic annotated
tag binds the full lock lifecycle, real provider request ID, pre/post state,
and canonical source-owned attestation-ruleset policy. The separate active
zero-bypass tag ruleset makes those nonce-scoped refs immutable. Out-of-band
ruleset mutation is an incident: GitHub exposes no conditional ruleset PUT, so
every supported writer must share the one durable lock and attestation
protocol.
The merge-queue barrier additionally needs an evaluate-to-active zero-bypass
organization workflow rule pinned to its exact merged source SHA. Proof needs a
target pull-request identity run; a controlled empty/same-tree canary that
merges while the barrier is evaluate-only; a second controlled canary whose
now-required barrier failure is rejected/dequeued by GitHub before external
activation; and a final read-only merge-group run that passes only after
external admission is exact active/effective. The workflow never mutates queue
state.
