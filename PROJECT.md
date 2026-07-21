# SylphxAI GitHub Organization Configuration

This repository owns organization-level GitHub configuration: community health
files, the organization profile, issue and pull-request templates, brand
references, repository templates, reusable workflows, and shared GitHub
Actions.

## Boundary

It owns reusable GitHub surfaces that serve more than one repository. Product
code, repository-specific policy, runtime state, delivery evidence, Skills
admission, and Sylphx Enact state stay with their owning repositories.

Public surfaces:

- `profile/README.md` and `.github/*` community health files
- `.github/workflows/adr29-admission.yml`
- `.github/workflows/release.yml`
- `.github/workflows/publish-npm.yml`
- `.github/actions/*`
- `templates/`, `brand/`, and `COMPANY.md`

The historical public-Skills cleanroom and external-admission decisions remain
in `docs/adr/`, but their target-specific workflows, policies, executors, and
source-inspection tests are retired. `SylphxAI/skills` now owns its admission
and static instruction authority.

## Delivery

Changes merged to `main` are consumed directly by GitHub. There is no separate
runtime deploy. `.github/workflows/project-control.yml` validates the shared
Changesets publisher; reusable workflow or action changes also require a
successful consumer invocation.

Repository scanning belongs to [Sylphx Enact Repository
Ingestion](https://github.com/SylphxAI/enact/blob/main/docs/specs/repository-ingestion.md).
GroundAtlas package/action dogfood is retired; the accepted historical decision
remains in `docs/adr/0003-local-groundatlas-project-control-gate.md`.
