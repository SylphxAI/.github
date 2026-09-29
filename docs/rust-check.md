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

1. The org's own sccache prefix. Every Sylphx runner carries its org's cache
   credential, minted by the platform for that runner (`SYLPHX_SCCACHE_*`):
   short-lived, and scoped to `sccache/<installation id>/` of the platform
   cache bucket, so an org reads and writes only its own entries and can never
   read or poison another org's. No secret to set.
2. The organization's own cache user, when the caller passes its
   `SYLPHX_CI_CACHE_ACCESS_KEY` / `SYLPHX_CI_CACHE_SECRET_KEY`.
3. `Swatinem/rust-cache` on GitHub's own Actions cache: per repository, no
   cost, keyed by toolchain, `Cargo.lock` and the `tests` input, and saved
   even when the check fails so a broken first run still warms the next. It
   saves from every ref (the check runs on branches, and a branch cannot read
   another branch's cache), and GitHub evicts least recently used entries
   inside its 10 GB per-repository quota.

sccache entries are keyed under `rustc`, shared by the org's repositories, and
the job ends with `sccache --show-stats`. The platform's `ci-sccache` key is
never a repository's cache: the `RGW_S3_*` secrets are ignored.

Inputs never reach a shell: they travel as environment variables and are
matched against a strict pattern first.
