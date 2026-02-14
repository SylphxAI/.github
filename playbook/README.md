# SylphxAI Development Playbook

Internal development workflow and standards for all SylphxAI projects.

## Contents

1. [Branch Strategy](branch-strategy.md) — How we use branches
2. [Environments](environments.md) — Production, preview, databases
3. [Coding Standards](coding-standards.md) — Stack, linting, conventions
4. [CI/CD](ci-cd.md) — Automated testing, deployment, migrations
5. [New Project Setup](new-project.md) — Checklist for starting a new project

## Quick Reference

| Rule | Detail |
|------|--------|
| Production branch | `main` |
| Development branch | `dev` |
| PR target | Always `dev`, never `main` |
| Merge to main | Only by promoting `dev` → `main` |
| Package manager | Bun |
| Linter/formatter | Biome |
| ORM | Drizzle |
| Migrations | Atlas |
| Hosting | Vercel |
| Database | Neon (Postgres) |
| Merge strategy | Squash merge |

## Per-Project Context

Each repo has a `CLAUDE.md` at root with project-specific details (architecture, routes, schemas, etc.). This playbook covers the shared workflow across all projects.
