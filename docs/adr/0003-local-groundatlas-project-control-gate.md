# ADR 0003: Add Local GroundAtlas Project-Control Gate

## Status

**Retired / superseded** (2026-07-18)

Superseded by Control Plane
[ADR-0014](https://github.com/SylphxAI/control-plane/blob/main/docs/adr/ADR-0014-groundatlas-product-retirement-cp-ingestion.md)
(GroundAtlas independent product thesis rejected; scanning/orientation becomes
Control Plane **Repository Ingestion**).

This repository no longer dogfoods the released GroundAtlas package or action.
Local non-GA validation continues in `.github/workflows/project-control.yml`
(Changesets publisher syntax + public-skills unit controls only).

## Context (historical)

`SylphxAI/.github` is an organization-control repository. It owns community
health files, reusable workflows, workflow templates, and shared composite
actions, but it previously had no repo-local CI. That made changes to the
control-plane repository rely on downstream consumer failures for feedback.

The GroundAtlas fleet rollout needed this repository to dogfood the same
vendor-neutral `project.manifest.json` contract as product and library
repositories without turning Sylphx Doctrine into a public default or making
generated `.groundatlas*` reports authoritative.

## Decision (historical)

Add:

- a vendor-neutral `project.manifest.json`;
- a local `GroundAtlas` workflow that runs on `pull_request`, `push` to `main`,
  and `merge_group`;
- the released `SylphxAI/groundatlas@v0.1.2` action with
  `groundatlas@0.1.2`;
- assertions that `project.manifest.json` is selected, `.doctrine/project.json`
  is only an adapter, strict fleet status has no warnings or blockers, and
  generated maps remain evidence/navigation only;
- a repo-local boundary test for the manifest, Doctrine adapter, and workflow
  pin;
- a non-mutating syntax check for the manager-aware Changesets publisher.

This did not change any reusable workflow or shared action public contract. It
only added a repo-local validation surface for this repository's own changes.

## Retirement decision

- Remove `.github/workflows/groundatlas.yml` and
  `tests/groundatlas-boundary.test.mjs`.
- Remove `groundatlas:fleet` and other Yes-class package/action dogfood commands.
- Keep residual `project.manifest.json` as a local identity surface only; do not
  treat GroundAtlas as a required product gate.
- Point repository-scanning / orientation capability to Control Plane Repository
  Ingestion (ADR-0014). Do not archive `SylphxAI/groundatlas` from this repo.

## Consequences

- Organization configuration no longer has a Yes-class reverse dependency on
  the GroundAtlas package or action.
- `.doctrine/project.json` remains the Sylphx Doctrine adapter and local
  governance catalog.
- Consumer repositories still own their own project manifests, delivery proof,
  branch protection, merge queues, deployments, and release evidence.
- Main readback plus local project-control CI (non-GA) remain the proof surface
  for future changes in this repository; consumer-use evidence is still
  required when a reusable workflow/action contract changes.
