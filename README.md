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
| [ci-ok](.github/actions/ci-ok/action.yml) | One required check that waits for every other GitHub Actions check on the commit and fails if any failed, a workflow failed to start, or no check ran |
| [main-red-gate](.github/actions/main-red-gate/action.yml) | Stop the line: while the trunk's newest conclusive Verify run is red and its way back is in motion, a merge group is admitted only for a revert, a live-outage fix or a pull request labelled `main-red-fix`; a red trunk with nothing in motion admits with a warning |
| [needs-pass](.github/actions/needs-pass/action.yml) | The aggregate verdict of a workflow. A skipped job never counts as passing a required check outside `merge_group`: list PR-time jobs in `required-unless-merge-group` so a `workflow_dispatch` run cannot post a green check over a red one |
| [secret-scan](.github/actions/secret-scan/action.yml) | Runs gitleaks over only the commits a push or pull request adds |
| [plain-language](.github/actions/plain-language/action.yml) | Warns about coined terms on the lines a pull request adds |
| [identifiers](.github/actions/identifiers/action.yml) | Fails when a change adds an id generator, or a text or serial primary key, that is not a UUIDv7 |
| [chat-senders](.github/actions/chat-senders/README.md) | Fails when a change adds a direct Telegram, Slack or Discord chat-API host outside the action's allow-list; products send chat through Notify |
| [stack-conformance](docs/stack-conformance.md) | Fails a change that adds a departure from the default stack, or an agent-runtime part Sylphx Agents owns that `policy/agent-runtime.json` does not allow until a date |
| [zh-hant](.github/actions/zh-hant/action.yml) | Fails when a change adds a Simplified-only character to Traditional Chinese text |
| [git-app-credentials](.github/actions/git-app-credentials/action.yml) | Creates a job-scoped GitHub App token for private git and cargo fetches |
| [cache-toolchain](.github/actions/cache-toolchain/action.yml) | Dependency cache keyed per toolchain (cargo, bun, npm, pnpm, gradle, unity) |
| [run-store](docs/run-store.md) | Hands a file or directory between the jobs of a run through the BuildCache gateway, keyed by run id; CI never depends on GitHub artifact storage |
| [setup-keel-tools](docs/setup-keel-tools.md) | The keel CLI at the title's `KEEL_PIN` and the wasm-bindgen CLI at its `Cargo.lock` version, cached per pin so a warm run takes seconds |
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
