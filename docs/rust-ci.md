# Rust CI on the shared compile cache

[`rust-ci.yml`](../.github/workflows/rust-ci.yml) is the standard Rust gate:
`cargo fmt --check`, `cargo clippy` and `cargo test` on one Sylphx runner,
with every rustc call served from the organization's compile cache through
[`actions/rust-sccache`](../.github/actions/rust-sccache/action.yml). The
decision and its reasons are
[ADR 0004](adr/0004-fleet-rust-compile-cache.md).

## Adopt

1. Copy [`workflow-templates/rust-ci.yml`](../workflow-templates/rust-ci.yml)
   to `.github/workflows/ci.yml`, or merge its `rust` job into the gate you
   have. Replace `@main` with the commit of this repository you adopt.
2. Keep all three triggers. The `push` to main is what warms the cache that
   pull requests and the merge queue read.
3. Pass the organization secrets `SYLPHX_CI_CACHE_ACCESS_KEY` and
   `SYLPHX_CI_CACHE_SECRET_KEY` by name, and make sure the repository is in
   the secrets' visibility list. Without them the action falls back to the
   repository's GitHub Actions cache, which works but is slower and capped.
4. Make `ci-ok` the required check.

A repository whose gate has bespoke jobs (device lanes, release builds,
generated code) keeps its own jobs and uses the same cache directly: the
`rust-sccache` step with `key-prefix: rustc`, after the toolchain and before
the first cargo command, on every Rust job, with a `push: branches: [main]`
run of those jobs.

## Inputs

| Input | Default | Use |
| --- | --- | --- |
| `workspace` | `.` | Cargo workspace directory |
| `runner-class` | `standard` | `xlarge` only after a measured need; a merge group uses the class's `-merge` lane |
| `fmt` | `true` | `cargo fmt --all -- --check` |
| `clippy-args` | `--workspace --all-targets -- -D warnings` | empty skips clippy |
| `test-args` | `--workspace` | empty skips tests |
| `locked` | `true` | `--locked` on clippy and test |
| `apt-packages` | empty | system packages the build needs |
| `git-deps` | empty | private git dependencies, as in [rust-check.md](rust-check.md) |
| `timeout-minutes` | `45` | job timeout |

## Reading the cache

Each run's summary has a "Rust compile cache" block: backend, compile
requests, hits, misses and the hit rate. A pull request that changes one
crate should hit on every crate it does not reach. Low hit rates usually mean
one of:

- the main run is missing, so nothing warms the cache;
- a step writes a different `RUSTFLAGS`, feature set or profile than main's run;
- the toolchain is `stable` without a pin and moved since main's last run;
- the crate's build script generates code from files outside the crate,
  which sccache cannot see (build such a job with `RUSTC_WRAPPER=` instead).

## What the cache does not do

It does not cache build-script execution, linking, or test runs; cargo still
links every binary and runs every test. Dependency downloads come from the
registry each run. The bound is the bucket's 14-day expiry and the
organization's object-store quota.
