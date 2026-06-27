# ADR-0002: Org-wide release tooling — retire @sylphx/bump, standardize on Changesets

- Status: Accepted
- Date: 2026-06-27
- Owner: platform / release engineering

## Context

The org ran two release mechanisms in parallel:

1. `@sylphx/bump` — a homegrown changelog/release runner (`SylphxAI/bump`,
   `#!/usr/bin/env node`), consumed two ways:
   - as a **composite action** inside the reusable workflow
     `SylphxAI/.github/.github/workflows/release.yml@main` (most repos call this);
   - **directly** on a self-hosted runner (only `synth`).
2. `changesets/action@v1` — used by a few repos via a standalone, self-contained
   `release.yml` (e.g. `pdf-reader-mcp`), plus a reusable
   `publish-npm.yml@main` that already wrapped it.

bump is a 0-star tool we no longer want to maintain. The decision is to retire it
and standardize the whole org on **Changesets**, the off-the-shelf standard.

Two latent failures surfaced during the migration and are the reason this ADR
exists (so new repos don't repeat them):

- **`startup_failure` on every reusable-workflow caller.** The org default
  `default_workflow_permissions` is **`read`**. The reusable release job requests
  `contents: write` + `pull-requests: write` (Changesets must commit version
  bumps and open the "Version Packages" PR). A reusable workflow can never hold
  more permission than its caller grants, so callers that omit a `permissions:`
  block fail **at workflow load time** — before any step runs.
- **`There is no .changeset directory in this project`.** Many repos never had
  `.changeset/config.json`. Once the reusable workflow runs Changesets, that file
  is mandatory.

## Decision

1. **Reusable workflow delegates to Changesets.** `release.yml@main` becomes a
   thin compatibility shim that maps the legacy bump inputs
   (`working-directory`, `build`/`prebuild`, `artifact`, `prepublish`,
   `postpublish`, …) onto `publish-npm.yml@main`, with
   `version-command: bunx @changesets/cli version` and
   `publish-command: bunx @changesets/cli publish`. Existing callers keep their
   `uses:` line unchanged and are transparently migrated off bump.

2. **Every caller grants release permissions.** The `release` job that calls the
   reusable workflow must declare:

   ```yaml
   jobs:
     release:
       permissions:
         actions: write          # only needed if postpublish triggers a workflow
         contents: write
         pull-requests: write
       uses: SylphxAI/.github/.github/workflows/release.yml@main
       secrets: inherit
   ```

   (We keep the org default at `read` — least privilege — rather than flipping
   the org to `write`. The grant is scoped to the single release job.)

3. **Every package repo carries `.changeset/config.json`** (org standard:
   `access: public`, `baseBranch` = the repo's default branch).

4. **`synth` (self-hosted, Rust/wasm) gets a self-contained Changesets
   `release.yml`** — keeps the Rust/wasm toolchain + build, adds `setup-node`
   (the self-hosted runner ships bun but no node), swaps the bump step for
   `changesets/action@v1`, preserves the Slack notifications (now reading
   `steps.changesets.outputs.publishedPackages`).

5. **Retire `@sylphx/bump`** once it has no remaining consumers: deprecate on npm
   and archive the repo.

## Consequences

- Release cadence is now changeset-driven: a release happens only when a
  `.changeset/*.md` file lands and the generated "Version Packages" PR merges.
  Contributors must add changesets. No spurious releases when there are none.
- New repos using the reusable workflow MUST include the `permissions:` block and
  a `.changeset/config.json`, or they hit the two failures above. This ADR is the
  reference; consider encoding both as a repo-template / CI lint.
- `changeset publish` publishes any public package whose local version is not yet
  on npm. For repos already published this is a no-op; a repo whose version ran
  ahead of npm gets a catch-up publish (intended). A **never-published** repo
  would get a first-time public release — treat that as a deliberate product
  decision, not an automatic side effect.
