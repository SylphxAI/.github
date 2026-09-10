# SylphxAI GitHub Organization Configuration

Organization profile, community health files, templates, reusable workflows,
and shared GitHub Actions.

See [PROJECT.md](./PROJECT.md) for the boundary and delivery contract.

Static instructions and their admission live in
[`SylphxAI/skills`](https://github.com/SylphxAI/skills). Live work and
repository ingestion live in Control Plane. This repository does not duplicate
either authority.

## Repository settings

Company repositories run with **auto-delete of merged PR head branches**
(`delete_branch_on_merge=true`): a merged PR is terminal, and the PR record is
the recovery home for its head branch. Policy and the sweep tool live in
[docs/repository-settings.md](./docs/repository-settings.md) and
[`scripts/set-delete-branch-on-merge.sh`](./scripts/set-delete-branch-on-merge.sh).
