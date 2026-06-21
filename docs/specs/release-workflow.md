# Release Reusable Workflow Contract

## Surface

`SylphxAI/.github/.github/workflows/release.yml@main`

This reusable workflow publishes package releases through the organization
release path. Consumer repositories call it from a thin repo-local workflow.

## Inputs

- `working-directory`: repository subdirectory for install, build, and publish
  commands. Defaults to `.`.
- `build`: canonical build command. Defaults to `bun run build`. Skipped when
  `artifact` is set.
- `prebuild`: legacy alias for `build`. Do not use in new callers. If both
  `build` and `prebuild` are set, `prebuild` wins to preserve existing caller
  behavior.
- `artifact`: optional artifact pattern. When set, the workflow downloads
  matching artifacts and skips the build command.
- `prepublish`: optional command to run after build or artifact download and
  before publishing.
- `postpublish`: optional command to run after a successful publish.

## Secrets

- `NPM_TOKEN`: optional npm publish token consumed by `SylphxAI/bump`.
- `SLACK_WEBHOOK`: optional release notification webhook.

## Caller Permissions

Consumer workflows must grant the reusable workflow the token permissions it
needs. The thin repo-local caller should include:

```yaml
jobs:
  release:
    permissions:
      actions: write
      contents: write
      pull-requests: write
    uses: SylphxAI/.github/.github/workflows/release.yml@main
```

GitHub does not let a reusable workflow raise permissions above the caller's
token scope. Missing caller permissions fail during workflow startup before any
job log exists.

## Contract Rules

- Unknown inputs must not be introduced in repo-local callers.
- New callers must use `build`, not `prebuild`.
- Backwards-compatible aliases may be added centrally when they prevent
  startup failures across existing consumers.
- Callers must grant `actions: write`, `contents: write`, and
  `pull-requests: write` unless this workflow's publish implementation is
  changed to need less.
- Removing an input requires an org audit proving no repository still uses it.
- Repo-specific release behavior belongs in the consumer repository or a
  dedicated adapter, not in this shared workflow.
