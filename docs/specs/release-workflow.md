# Release Reusable Workflow Contract

## Surface

`SylphxAI/.github/.github/workflows/release.yml@main`

This reusable workflow publishes npm package releases through the organization
release path. Consumer repositories call it from a thin repo-local workflow.

## Release Model

Changesets owns release intent, version PR generation, changelog updates, and
GitHub Release creation. The default publish command is
`sylphx-changesets-publish`, a manager-aware publisher installed by the shared
`setup-changesets-publisher` action.

`@sylphx/bump` is retired and must not be used by callers.

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
  Postpublish commands receive `GH_TOKEN` / `GITHUB_TOKEN` scoped to the
  workflow token so repo-owned follow-up workflows can be triggered through
  `gh workflow run` without adding another secret.

`publish-npm.yml` also accepts advanced inputs for direct callers:

- `version-command`: defaults to the caller-provided Changesets version command.
- `publish-command`: defaults to `auto`, which runs
  `sylphx-changesets-publish`. Override only for a repo-owned publisher that
  performs equivalent package-manager-aware packing, tarball audit, and tarball
  publication.
- `npm-registry`: defaults to `https://registry.npmjs.org`.
- `create-github-releases`: defaults to `true`.

## Secrets and Authentication

- `NPM_TOKEN`: optional npm publish token. Prefer npm trusted publishing through
  GitHub Actions OIDC where package configuration supports it.

The shared publisher publishes audited tarballs with `npm publish <tarball>`.
It still bridges the workflow-scoped `NODE_AUTH_TOKEN` provided by
`actions/setup-node` to `NPM_CONFIG_TOKEN` / `npm_config_token` when neither is
set, but npm is the registry upload client so the artifact that passed audit is
the artifact that reaches npm.
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
      id-token: write
    uses: SylphxAI/.github/.github/workflows/release.yml@main
```

GitHub does not let a reusable workflow raise permissions above the caller's
token scope. Missing caller permissions fail during workflow startup before any
job log exists. `id-token: write` is required for trusted publishing and harmless
for token-based fallback publishes.

## Workspace Publish Safety

The default publisher must materialize workspace protocol ranges from the
current local workspace manifest versions, then pack every unpublished package
before publication and inspect the packed `package/package.json`. If any
dependency field still contains `workspace:`, the workflow must fail before
`npm publish` is allowed to mutate the registry. Publication must use the exact
tarball that passed the audit; no package manager may perform a second implicit
pack during registry upload.

Workspace range materialization is source-tree temporary and restored in a
`finally` path after audit/publish. The durable source of truth remains the
Changesets version PR plus workspace manifests; the registry receives only
consumer-installable package metadata:

- `workspace:*` -> the exact current local package version.
- `workspace:^` -> `^<current local package version>`.
- `workspace:~` -> `~<current local package version>`.
- `workspace:<explicit range>` -> `<explicit range>` with the protocol removed.
- Unsupported path-style workspace specs fail closed before publication.

Expected behavior by package manager:

- Bun workspaces: pack with `bun pm pack`, then publish the audited tarball with
  `npm publish <tarball>`.
- pnpm workspaces: pack with `pnpm pack`, then publish the audited tarball with
  `npm publish <tarball>`.
- npm workspaces: fail if packed metadata still contains `workspace:` because
  npm does not materialize those ranges.
- Yarn workspaces: pack with `yarn pack`, then publish the audited tarball with
  `npm publish <tarball>`.

## Contract Rules

- Unknown inputs must not be introduced in repo-local callers.
- New callers must use `build`, not `prebuild`.
- Backwards-compatible aliases may be added centrally when they prevent startup
  failures across existing consumers.
- Callers must grant `actions: write`, `contents: write`, `pull-requests: write`,
  and `id-token: write` unless this workflow's publish implementation is changed
  to need less.
- Removing an input requires an org audit proving no repository still uses it.
- Repo-specific release behavior belongs in the consumer repository or a
  dedicated adapter, not in this shared workflow.
- Repositories must not use `@sylphx/bump`, `SylphxAI/bump`, or direct
  `changeset publish`/`bun publish`/`npm publish` from a workspace package
  unless an equivalent pack audit proves the exact tarball being uploaded
  cannot contain `workspace:` ranges.
