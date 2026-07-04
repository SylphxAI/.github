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
mechanisms. Repo-specific behavior belongs in the consuming repository or in a
tenant/product adapter.

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
- `.github/actions/adr29-admission/action.yml`
- `.github/actions/setup-changesets-publisher/action.yml`
- `templates/`
- `brand/`
- `COMPANY.md`

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
