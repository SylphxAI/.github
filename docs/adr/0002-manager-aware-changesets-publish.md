# ADR 0002: Use Manager-Aware Publish After Changesets Versioning

## Status

Accepted

## Context

SylphxAI repositories use Changesets for release intent, generated version PRs,
changelogs, tags, and GitHub releases. That part is the source of truth and
should stay compatible with standard Changesets workflows.

The unsafe boundary is publication for workspaces that use package-manager
protocols such as `workspace:*`, `workspace:^`, and `workspace:~`. The
Changesets CLI has historically delegated publication to npm unless it detects
pnpm. npm does not materialize workspace protocol ranges during `npm pack` or
`npm publish`, so a Bun or Yarn workspace can publish a package whose registry
metadata still contains `workspace:` ranges. npm consumers then fail with
`EUNSUPPORTEDPROTOCOL`.

The previous reusable workflow workaround rewrote `package.json` files in the
working tree before running `changesets/action`. That made the release path
harder to reason about because the source tree, version PR, packed artifact,
and registry metadata were no longer separated.

## Decision

The organization release workflow keeps `changesets/action` as the release
intent and version PR mechanism. The default publish command is now
`sylphx-changesets-publish`, installed by
`SylphxAI/.github/.github/actions/setup-changesets-publisher@main`.

The publisher:

- detects the repository package manager from `packageManager` and lockfiles;
- discovers public root and workspace packages;
- skips versions that already exist on npm;
- temporarily materializes `workspace:` dependency ranges from current local
  workspace package versions before packing or publishing, then restores the
  source manifests;
- publishes unpublished packages with the matching package manager (`bun
  publish` for Bun workspaces, `pnpm publish` for pnpm workspaces, etc.);
- packs each candidate first and reads `package/package.json` from the tarball;
- fails before publication if any packed dependency field still contains a
  `workspace:` protocol;
- bridges the workflow-scoped `NODE_AUTH_TOKEN` to `NPM_CONFIG_TOKEN` /
  `npm_config_token` for Bun publication when no npm config token is already
  set;
- prints `New tag: <name>@<version>` after each successful publish so
  `changesets/action` continues to create the expected tags and GitHub
  releases.

`NPM_TOKEN` is optional in the reusable workflow. Repositories should prefer npm
trusted publishing through GitHub Actions OIDC when configured; token-based
publishing remains a fallback for packages not yet migrated.

## Consequences

- Changesets remains the compatible release intent and version PR interface.
- Bun workspaces publish through Bun, but no longer rely on Bun alone to choose
  internal package versions; the shared publisher materializes local workspace
  versions first and supplies Bun's expected automation token environment.
- pnpm workspaces remain package-manager-native and gain the same local-version
  materialization plus tarball gate.
- npm workspaces that still contain `workspace:` fail safely before immutable
  broken package versions reach npm.
- Bump is not part of the organization release path; consumers should not add
  `@sylphx/bump` or `SylphxAI/bump` to new workflows.
