# Agent Instructions - SylphxAI GitHub Organization Configuration

Start with the upstream doctrine:

- Doctrine repo: <https://github.com/SylphxAI/doctrine>
- Local project identity: [PROJECT.md](./PROJECT.md)
- Machine manifest: [`.doctrine/project.json`](./.doctrine/project.json)

This repository owns organization-level GitHub defaults, shared templates,
reusable workflows, and shared GitHub Actions. Do not put product-specific or
repository-specific behavior here unless it is genuinely an organization-wide
default.

Before changing reusable workflows, workflow templates, or composite actions,
verify the consumer contract and update this repository's ADR/spec surface if
the public workflow/action contract changes.
