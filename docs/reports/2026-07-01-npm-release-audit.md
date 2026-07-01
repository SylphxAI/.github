# SylphxAI npm Release Tooling Audit - 2026-07-01

## Scope

- GitHub organization: `SylphxAI`
- Active repositories inspected: 107
- Archived repositories observed: 23
- Public `@sylphx/*` or `@sylphxai/*` package manifests read from source: 169
- Evidence sources: default-branch repository trees, package manifests,
  `.changeset` state, lockfiles, release workflows, and npm registry readback.

## Root Cause

Changesets remains correct for release intent and generated version PRs. The
unsafe boundary is publish-time materialization. `changeset publish` can delegate
to npm for non-pnpm repositories; npm does not materialize `workspace:` ranges
when packing or publishing, so Bun or Yarn workspaces can publish package
metadata that npm consumers cannot install.

## Central Fix

`SylphxAI/.github/.github/workflows/release.yml@main` now delegates publication
to the manager-aware `sylphx-changesets-publish` command installed by
`setup-changesets-publisher`.

The command temporarily materializes `workspace:` ranges from current local
workspace package versions, packs every unpublished package, and fails before
registry mutation if the packed `package/package.json` still contains
`workspace:` in dependency metadata.

Follow-up validation on `code`/`codec` showed why this must be owned by the
shared publisher rather than by Bun alone: Bun removed the `workspace:` protocol
but selected stale registry versions for some internal dependencies. The
publisher now derives `workspace:*`, `workspace:^`, and `workspace:~` from the
workspace package versions in the checked-out version PR before `bun publish` or
any other manager publish command runs.

The same follow-up release rehearsal exposed a separate Bun authentication
boundary: `actions/setup-node` provides `NODE_AUTH_TOKEN`, while `bun publish`
expects `NPM_CONFIG_TOKEN` in automation. The shared publisher now bridges the
workflow-scoped token for Bun without adding a second secret.

## Fleet Findings

| Finding | Count |
| --- | ---: |
| Repositories with `@sylphx/bump` / `SylphxAI/bump` references | 17 |
| Public package manifests with source `workspace:` dependencies | 20 |
| Bun workspace release paths using `changeset publish` | 13 |
| Direct `npm publish` paths with workspace protocol risk | 2 |
| npm workspaces that cannot materialize `workspace:` safely | 1 |

## Repositories Requiring Repo-Local Follow-up

| Repository | Risk |
| --- | --- |
| `platform` | Bun workspace, `changeset publish`, direct `npm publish` workflows |
| `hookyard` | Bun workspace, `changeset publish`, manual `npm publish`, Bump references |
| `code` | Broken latest registry metadata; central workflow caller |
| `codec` | Broken latest registry metadata; central workflow caller |
| `reify` | Active workflow still runs `bunx @sylphx/bump` |
| `gust` | Direct `npm publish` for generated/native packages; Bump references |
| `coderag`, `rapid`, `flow`, `cat`, `silk`, `bun-workflow-test`, `pura`, `tsnum`, `synth` | Bun workspace release scripts mention `changeset publish`; central callers become safe after the shared workflow fix, but repo-local scripts/docs should be cleaned up |
| `flux` | npm workspace with public `workspace:` source ranges; must not publish through npm without artifact transformation |

## Already Broken Published Versions

npm registry readback showed live `workspace:*` metadata in these latest
versions:

- `@sylphx/code@1.0.0`
- `@sylphx/code-client@1.0.0`
- `@sylphx/code-server@1.0.0`
- `@sylphx/codec@1.0.0`
- `@sylphx/codec-cli@1.0.0`

Those immutable versions require forward-fix package releases. Optional npm
`deprecate` should be handled through the normal registry ownership path after
fixed versions are published.

## Bump Retirement

`SylphxAI/bump` is archived and `@sylphx/bump` is deprecated on npm. No new
workflow should use Bump. Existing references are either historical docs or
repo-local workflow/package dependencies that must be removed during repo-local
cleanup.
