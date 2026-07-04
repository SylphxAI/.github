# ADR 0003: Add Local GroundAtlas Project-Control Gate

## Status

Accepted

## Context

`SylphxAI/.github` is an organization-control repository. It owns community
health files, reusable workflows, workflow templates, and shared composite
actions, but it previously had no repo-local CI. That made changes to the
control-plane repository rely on downstream consumer failures for feedback.

The GroundAtlas fleet rollout needs this repository to dogfood the same
vendor-neutral `project.manifest.json` contract as product and library
repositories without turning Sylphx Doctrine into a public default or making
generated `.groundatlas*` reports authoritative.

## Decision

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

This does not change any reusable workflow or shared action public contract. It
only adds a repo-local validation surface for this repository's own changes.

## Consequences

- Organization configuration dogfoods GroundAtlas as a normal repository.
- `.doctrine/project.json` remains the Sylphx Doctrine adapter and local
  governance catalog.
- Consumer repositories still own their own project manifests, delivery proof,
  branch protection, merge queues, deployments, and release evidence.
- Main readback plus the local GroundAtlas workflow become the first proof
  surface for future changes in this repository; consumer-use evidence is still
  required when a reusable workflow/action contract changes.
