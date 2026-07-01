# ADR-0002: Org-wide release tooling — retire @sylphx/bump, standardize on Changesets

- Status: Accepted
- Date: 2026-06-27
- Updated: 2026-07-01
- Owner: platform / release engineering

## Context

The org previously ran multiple release mechanisms in parallel:

1. `@sylphx/bump` — a homegrown semantic-commit release runner and GitHub Action.
2. Repo-local `changesets/action@v1` workflows, sometimes publishing directly with `changeset publish`, `bun publish`, or `npm publish`.
3. Custom package publish workflows for repos with generated package artifacts.

The Bump model created two strategic problems:

- Release intent was inferred from commits instead of explicit package release notes.
- Workspace publishing behavior was owned inconsistently by repo-local scripts, so some immutable npm versions leaked `workspace:*` dependency ranges into registry metadata.

A `workspace:*` dependency in source manifests is valid monorepo intent. A `workspace:*` dependency in a published npm package is consumer-breaking.

## Decision

1. **Bump is retired.** Do not add new `@sylphx/bump` package dependencies, `SylphxAI/bump` action references, or semantic-commit auto-bump release paths. The Bump repository is archived, the npm package is deprecated, and the action fails fast with a retirement message.

2. **Changesets is the canonical release-intent and version-PR layer.** A package version changes only when a changeset file or an explicit repo-owned release process asks for it. No changeset means no package bump.

3. **The central reusable release workflow is the canonical Sylphx publish path.** Repos should call:

   ```yaml
   jobs:
     release:
       permissions:
         actions: write
         contents: write
         pull-requests: write
         id-token: write
       uses: SylphxAI/.github/.github/workflows/release.yml@main
       secrets: inherit
   ```

   `release.yml@main` delegates to `publish-npm.yml@main`, which uses `changesets/action@v1` for version PRs / GitHub Releases and the Sylphx manager-aware publisher for actual npm publication.

4. **Publication must audit the exact packed artifact.** The Sylphx publisher detects the workspace package manager, materializes internal `workspace:*`, `workspace:^`, and `workspace:~` ranges from local package versions, packs each package, reads `package/package.json` inside the tarball, fails if any dependency field still contains `workspace:`, then publishes the same audited tarball with `npm publish <tarball>`.

5. **Repo-local direct publish shortcuts are not the org standard for workspace repos.** Do not use direct `changeset publish`, `bun publish`, `npm publish`, or `pnpm publish` for Bun/Yarn/npm workspaces unless the repo owns an equivalent tarball materialization/audit path. Fail-closed scripts that point contributors to the central workflow are acceptable.

6. **Every Changesets package repo carries `.changeset/config.json`.** The org default is public packages with the repo default branch as `baseBranch`, unless the repo documents a narrower package policy.

7. **Custom generated-package workflows must meet the same artifact rule.** A repo with generated npm directories may keep a repo-owned workflow only if it proves the packed/generated artifact contains no `workspace:` metadata and does not mask publish failures without a registry readback.

## Consequences

- Release cadence is explicit and contributor-visible: changeset files drive version PRs, changelogs, tags, and GitHub Releases.
- Caller workflows must grant write permissions to the release job because org-level default workflow permissions stay read-only.
- Published registry metadata is the production boundary. Bad immutable versions require a forward-fix release or explicit npm deprecation; archiving the source repo is not remediation.
- Final delivery proof must separate version PR merge, release workflow success, packed artifact audit, npm registry readback, consumer install smoke, and any external runner or npm permission blockers.
- New agents should use the `sylphx-release-publish` Codex skill and its audit script before changing package release automation.
