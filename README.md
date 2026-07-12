# SylphxAI GitHub Organization Configuration

This repository owns SylphxAI organization-level GitHub configuration:
community health files, the organization profile, reusable workflows, workflow
templates, shared GitHub Actions, brand references, and repository bootstrap
templates.

Start with [PROJECT.md](./PROJECT.md) for the human project boundary and
[`project.manifest.json`](./project.manifest.json) for the vendor-neutral
machine-readable project control file.

The organization-owned public-skills gate is documented in
[`docs/specs/public-skills-external-admission.md`](./docs/specs/public-skills-external-admission.md).
It validates the target repository as inert Git data from a source-pinned
required workflow; the target never executes its own admission code.
The same specification documents the independently owned, dry-run-by-default
organization-ruleset executor. It consumes only the canonical Doctrine
default-branch record as inert JSON, never executes Doctrine code or schema,
and serializes every authorized write with a unique fenced Git-ref lock.

Generated `.groundatlas*` reports are evidence/navigation only. They are not the
source of truth for organization policy, reusable workflow contracts, or
consumer repository delivery proof.
