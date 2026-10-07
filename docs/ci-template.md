# CI template: fast paths for every repository

The org sweep found the same waste across most repositories: superseded runs
that keep running, docs-only pull requests that run the whole suite, no
dependency cache, heavy jobs that start before the cheap checks pass, and large
runners on light jobs. This page is the recipe; the starter is
[`workflow-templates/ci-fast-path.yml`](../workflow-templates/ci-fast-path.yml).
The merge-queue policy itself is [optimistic-merge.md](optimistic-merge.md); a
repository on optimistic merge already has the shape below, so it adopts only
the pieces it lacks.

## Pieces

| Piece | Use |
| --- | --- |
| [`workflows/changes.yml`](../.github/workflows/changes.yml) | Reusable path filter. Outputs `code`, `docs`, `workflows`. Gate heavy jobs on `needs.changes.outputs.code == 'true'`. A merge group is diffed (base to head commit) like its pull request; pushes, and any diff that cannot be computed, report everything changed. Override the `code` input (a JSON array of globs) when the repository's code is narrower. |
| [`actions/cache-toolchain`](../.github/actions/cache-toolchain/action.yml) | Cache preset keyed per toolchain, OS, arch and lockfile: `cargo` (registry), `bun`, `npm`, `pnpm`, `gradle`, `unity` (Library). |
| [`actions/rust-sccache`](../.github/actions/rust-sccache/action.yml) | Compiled Rust output cache. Use it with `cache-toolchain` `cargo`. |
| [`actions/needs-pass`](../.github/actions/needs-pass/action.yml) | The `ci-ok` verdict: skipped passes, failed or cancelled fails. |
| `docker/build-push-action` with `cache-from/to: type=gha` | Image builds. |

## Concurrency

Put this at the workflow level. A new push supersedes the pull request's run; a
merge-group or trunk run is never cancelled.

```yaml
concurrency:
  group: ${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}
```

## Triggers

`pull_request`, `merge_group` and `push: branches: [main]` only. A bare `push`
on every branch plus `pull_request` runs each commit twice. For docs, rely on
the `changes` job rather than trigger-level `paths-ignore`: a skipped required
workflow never reports, which blocks the merge; a skipped job does report.

## Gating

1. `changes` first (seconds, standard runner).
2. Cheap gate - format, lint, typecheck, unit tests - `needs: changes`.
3. Heavy jobs - release build, e2e, iOS, Android, matrices - `needs: [changes, gate]`, so a lint failure never pays for a build.
4. `ci-ok`: `if: always()`, `needs:` every job above, then `needs-pass`. Make
   it the only required check. Skipped jobs (a docs-only change) count as
   success; a failed or cancelled one fails; list `changes` as `required` so a
   broken filter never reads as green.

## Which events run the heavy jobs

Follow [optimistic-merge.md](optimistic-merge.md): the merge group runs the
gate only; a ready pull request runs the heavy jobs its change affects; the
trunk and the nightly schedule run everything. A heavy job that cannot run on
every ready pull request (a device matrix, a store build) is gated on
`github.event_name == 'merge_group' || github.event_name == 'schedule'` or on
`workflow_dispatch`, and the PR path keeps the light checks. Say so in the
workflow: a skipped heavy job is only safe when something still runs it before
release.

## Runners

- Light jobs (`changes`, lint, docs, scripts): `sylphx-linux-standard`.
  Never `-xlarge` or `-2xlarge`.
- Every `if: always()` verdict job (`ci-ok`, `verified`, any `source-ci/pass`
  alias) runs on `sylphx-linux-control`, the reserved gate pool, on every
  event: it still runs after its run is cancelled, and the run keeps its
  concurrency group until it does, so on a busy build pool it holds the next
  run. Keep `always()`; never `!cancelled()` on a verdict (a skipped required
  check counts as passing). Any other job that would run after a cancel uses
  `!cancelled()`.
- Merge-group runs use the `-merge` lane (`sylphx-linux-standard-merge`,
  `sylphx-linux-xlarge-merge`) so they never queue behind the pull-request
  backlog: `runs-on: ${{ github.event_name == 'merge_group' && '...-merge' || '...' }}`.
- xlarge only per job, after a measured need (a compile that is CPU-bound and
  more than 10 minutes on standard).
- Set `timeout-minutes` on every job.

## Cache keys

`cache-toolchain` keys are `<toolchain>-<os>-<arch>-<extra-key>-<hash of
lockfiles>` with a restore prefix, so a lockfile change restores the nearest
cache and saves a new one. Pass `extra-key` for a matrix leg. Unity's Library
cache is keyed by `Packages/manifest.json`, `packages-lock.json` and
`ProjectVersion.txt`.

## Third-party actions

Pin by full commit SHA with the version in a trailing comment. Callers pin this
repository's workflows and actions by commit too.
