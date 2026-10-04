# Remote cargo check

`.github/workflows/rust-check.yml` is the one reusable `cargo check` job: it
checks out a ref, installs the Rust toolchain the workspace pins, starts the
org's own sccache, runs `cargo check --locked` for the given packages
(optionally `--tests`), and prints cargo's output between `@@sylphx-check-begin` and
`@@sylphx-check-end`, then `@@sylphx-check-exit=<code>`.

It runs on `sylphx-linux-standard`, the class of a ready pull request; it does
not use the merge-gate class.

## Adopt it in a repository

Copy `workflow-templates/rust-check.yml` to
`.github/workflows/sylphx-check.yml` (the CLI dispatches that file name)
unchanged - it pins the reusable workflow by commit, as every caller must; on
Sylphx runners it needs no secret. Then, from any machine:

    sylphx build check --repo ORG/REPO --ref my-branch -p my-crate [--tests]

Compile cache, first that applies (the
[`rust-sccache`](../.github/actions/rust-sccache/action.yml) action):

1. BuildCache. The job exchanges its GitHub OIDC identity for a short-lived
   run token scoped to the organization's own cache namespace, so an org reads
   and writes only its own entries and can never read or poison another org's.
   No secret to set. It needs `id-token: write` (see below).
2. The organization's own cache user, when the caller passes its
   `SYLPHX_CI_CACHE_ACCESS_KEY` / `SYLPHX_CI_CACHE_SECRET_KEY`.
3. `Swatinem/rust-cache` on GitHub's own Actions cache: per repository, no
   cost, keyed by toolchain, `Cargo.lock` and the `tests` input, and saved
   even when the check fails so a broken first run still warms the next. It
   saves from every ref (the check runs on branches, and a branch cannot read
   another branch's cache), and GitHub evicts least recently used entries
   inside its 10 GB per-repository quota.

sccache entries are keyed under `rustc`, shared by the org's repositories, and
the job ends with `sccache --show-stats` whenever BuildCache or the static keys
carried it (the action's `backend` output is `buildcache` or `static`). The
platform's `ci-sccache` key is never a repository's cache: the `RGW_S3_*`
secrets are ignored.

## Permissions the caller must grant

The reusable job requests `id-token: write` (with `contents: read`) so the
compile cache can use BuildCache. A reusable workflow cannot hold more
permission than its caller gives it, so a caller that moves its pin to a commit
with this requirement must grant `id-token: write` on the calling job (the
starter's `check` job, next to its `uses:`), or GitHub refuses to start the
workflow.

Inputs never reach a shell: they travel as environment variables and are
matched against a strict pattern first.

## Private git dependencies

A workspace that depends on private git repositories passes them as
`git-deps` (one `OWNER/REPO` per line) and its own reader App as
`GIT_DEPS_APP_ID` / `GIT_DEPS_APP_KEY`. For each owner the job mints a
read-only token for exactly those repositories and fetches through it; no
platform credential is involved. Without a toolchain file the check uses
stable; with `rust-toolchain(.toml)` it uses the pinned toolchain.
