# Project-Control Gate Spec

## Status

**Retired for GroundAtlas product dogfood** (2026-07-18).

Per-repo GroundAtlas package/action dogfood is no longer required. Capability
home: Control Plane **Repository Ingestion**
([ADR-0014](https://github.com/SylphxAI/control-plane/blob/main/docs/adr/ADR-0014-groundatlas-product-retirement-cp-ingestion.md)).

Residual local validation (non-GA) lives in
`.github/workflows/project-control.yml`.

## Goal

Give `SylphxAI/.github` a lightweight, non-mutating repository-local validation
gate for organization-control changes — without a Yes-class reverse dependency
on the GroundAtlas package or action.

## Scope

The gate validates this repository only:

- local project identity may live in `project.manifest.json` and
  `.doctrine/project.json`;
- shared workflow/action contracts remain owned by their ADRs and specs;
- GroundAtlas fleet reports, package pins, and `uses: SylphxAI/groundatlas@…`
  are **not** acceptance criteria.

The gate does not own consumer repository manifests, branch protection,
deployments, releases, runtime behavior, production proof, or Control Plane
Repository Ingestion implementation.

## Workflow Contract

`.github/workflows/project-control.yml` runs on:

- `pull_request`;
- `push` to `main`;
- `merge_group`.

It must:

1. check out the repository;
2. set up Node.js 22.14.0;
3. run `node --check` against
   `.github/actions/setup-changesets-publisher/changesets-publish.mjs`;
4. run `node --test tests/public-skills-admission.test.mjs`;
5. run `node --test tests/public-skills-merge-queue-barrier.test.mjs`;
6. run `python3 -B tests/test_public_skills_ruleset_executor.py`.

It must **not** install or invoke `groundatlas`, call
`SylphxAI/groundatlas@…`, or upload `.groundatlas*` fleet artifacts as a
required gate.

## Acceptance

- The Changesets publisher syntax check passes.
- Public-skills unit control suites that still apply pass.
- No reusable workflow/action public input, output, permission, or behavior is
  changed by this local gate.
- No Yes-class GroundAtlas package/action reverse dependency remains in this
  repository's workflows, commands, or required CI.

## Historical note

Earlier revisions of this gate dogfooded `groundatlas@0.1.2` and asserted
manifest selection via `ga fleet`. That product packaging is rejected under
Control Plane ADR-0014; do not reintroduce required per-repo GA dogfood.
