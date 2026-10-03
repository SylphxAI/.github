# SylphxAI/.github

Shared GitHub configuration for the SylphxAI organization: the
[organization profile](profile/README.md), default community health files
(code of conduct, contributing guide, security policy, issue and pull request
templates), and reusable GitHub Actions.

## Shared actions

Use one from another repository with
`uses: SylphxAI/.github/.github/actions/<name>@<commit>` (or `@main`).

| Action | What it does |
| --- | --- |
| [metadata-sync](.github/actions/metadata-sync/README.md) | Offline required-region/field patching with whole-plan validation and deterministic check/write; caller owns rendering ([anymd/repomap adoption](.github/actions/metadata-sync/README.md#adoption-in-anymd-and-repomap)) |
| [brand](.github/actions/brand/README.md) | Builds brand assets or checks provenance hashes and surface copies, with caller-owned masters and data |
| [review-stamp-gate](.github/actions/review-stamp-gate/README.md) | Requires trusted successful head reviews; data maps platform and security/money/migration classes to Ops and other changes to the owning lane's independent final reviewer |
| [ci-ok](.github/actions/ci-ok/action.yml) | One required check that waits for every other GitHub Actions check on the commit and fails if any failed |
| [needs-pass](.github/actions/needs-pass/action.yml) | The aggregate verdict of a workflow. A skipped job never counts as passing a required check outside `merge_group`: list PR-time jobs in `required-unless-merge-group` so a `workflow_dispatch` run cannot post a green check over a red one |
| [secret-scan](.github/actions/secret-scan/action.yml) | Runs gitleaks over only the commits a push or pull request adds |
| [plain-language](.github/actions/plain-language/action.yml) | Warns about coined terms on the lines a pull request adds |
| [identifiers](.github/actions/identifiers/action.yml) | Fails when a change adds an id generator, or a text or serial primary key, that is not a UUIDv7 |
| [zh-hant](.github/actions/zh-hant/action.yml) | Fails when a change adds a Simplified-only character to Traditional Chinese text |
| [git-app-credentials](.github/actions/git-app-credentials/action.yml) | Creates a job-scoped GitHub App token for private git and cargo fetches |
| [cache-toolchain](.github/actions/cache-toolchain/action.yml) | Dependency cache keyed per toolchain (cargo, bun, npm, pnpm, gradle, unity) |
| [setup-sylphx-cli](.github/actions/setup-sylphx-cli/action.yml) | Installs a pinned `@sylphx/cli` |
| [setup-changesets-publisher](.github/actions/setup-changesets-publisher/action.yml) | Installs the Changesets publish command used by release workflows |

Reusable workflows are in [.github/workflows](.github/workflows); the CI fast-path recipe (`changes.yml`, `cache-toolchain`, `ci-fast-path` starter) is [docs/ci-template.md](docs/ci-template.md).
[disarm-auto-merge-on-push](docs/disarm-automerge-on-push.md) clears an
arm predating a new push without dequeuing an entry already on that head.

## Repository settings

Company repositories delete a pull request's head branch when it merges
(`delete_branch_on_merge=true`). The policy and the script that applies it are
in [docs/repository-settings.md](docs/repository-settings.md) and
[scripts/set-delete-branch-on-merge.sh](scripts/set-delete-branch-on-merge.sh).
