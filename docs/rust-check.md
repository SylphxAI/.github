# Remote cargo check

`.github/workflows/rust-check.yml` is the one reusable `cargo check` job: it
checks out a ref, installs the Rust toolchain the workspace pins, starts the
shared sccache, runs `cargo check --locked` for the given packages (optionally
`--tests`), and prints cargo's output between `@@sylphx-check-begin` and
`@@sylphx-check-end`, then `@@sylphx-check-exit=<code>`.

It runs on `sylphx-linux-standard`, the class of a ready pull request; it does
not use the merge-gate class.

## Adopt it in a repository

Copy `workflow-templates/rust-check.yml` to
`.github/workflows/sylphx-check.yml` (the CLI dispatches that file name) and
make the two sccache secrets available to the repository
(`RGW_S3_ACCESS_KEY`, `RGW_S3_SECRET_KEY`); without them the check compiles
cold. Then, from any machine:

    sylphx build check --repo ORG/REPO --ref my-branch -p my-crate [--tests]

Inputs never reach a shell: they travel as environment variables and are
matched against a strict pattern first.
