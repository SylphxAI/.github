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
3. Nothing else is needed for the compile cache: the starter's `rust` job
   grants `id-token: write`, and the job exchanges its GitHub OIDC identity
   for a short-lived BuildCache token (see below). Keep passing the
   organization secrets `SYLPHX_CI_CACHE_ACCESS_KEY` and
   `SYLPHX_CI_CACHE_SECRET_KEY` by name while a fallback is wanted: they
   carry the job when BuildCache is not available to it. Without them the
   action falls back to the repository's GitHub Actions cache, which works but
   is slower and capped.
4. Make `ci-ok` the required check.

A repository whose gate has bespoke jobs (device lanes, release builds,
generated code) keeps its own jobs and uses the same cache directly: the
`rust-sccache` step with `key-prefix: rustc`, after the toolchain and before
the first cargo command, on every Rust job, with a `push: branches: [main]`
run of those jobs. Add `permissions: id-token: write` (with `contents: read`)
to each job that uses the action, or to the workflow. Without it the job has
no OIDC identity, BuildCache is skipped with one warning, and the action falls
back to the static secrets (`static`), then the Actions cache (`gha`). Pull
requests from forks never get `id-token: write` and take the same fallback.

## The compile cache backend

The `rust-sccache` action picks the first backend that works, and writes it to
the job summary and to its `backend` output (`buildcache`, `static`, `gha` or
`none`):

1. **`buildcache`.** The job requests a GitHub OIDC token (audience
   `sylphx-build-cache`) and exchanges it at the BuildCache gateway
   (`build-cache-url`, default `https://build-cache.sylphx.net`;
   `build-cache-network` `public` or `cluster`) for a run token. The gateway
   maps the repository's owner to the organization and scopes the token by
   the event: `merge_group`, `schedule` and a push to `main` write the
   protected scope (and read only it); every other event, pull requests
   included, writes a dev scope and reads protected and dev. A pull request
   therefore reads everything main wrote and can never poison it. The token
   is masked, goes only to the sccache server, and is never written to
   `$GITHUB_ENV`, an output or the summary. `build-cache: 'false'` skips this
   backend.
2. **`static`.** The organization's own object-store user, from the secrets.
3. **`gha`.** The repository's Actions cache.
4. **`none`.** The job compiles cold.

A failure at any step (no OIDC permission, the gateway answering 404 for an
organization it does not know, 503, a timeout, an unusable reply, or the sccache
server not starting) selects the next backend with one warning that names the
HTTP status or the curl exit code, never a body or a token.

## Inputs

Every input is optional. The contract the rollout repositories adopt:

| Input | Default | Use |
| --- | --- | --- |
| `key-prefix` | `rustc` | Cache namespace: one per organization. Leave it. It changes only to roll the whole namespace after a poisoned entry (`rustc-2`), as one change in the starter of every caller. Letters, digits, `.`, `_`, `-`, up to 64 |
| `toolchain` | empty | Toolchain to install (`stable`, `1.85.0`, `nightly-2026-09-01`). Empty uses the repository's `rust-toolchain.toml` (or `rust-toolchain`) found from the workspace upward, else `stable`. Prefer the pin file: the compiler version is part of every cache key, so an unpinned `stable` loses the cache every release |
| `features` | empty | Features for clippy and test: `all` is `--all-features`, a comma list is `--features a,b`. Empty is the crates' defaults. Use the same value on every event: a different feature set is a different compile and a cache miss |
| `fmt` | `true` | Lane: `cargo fmt --all -- --check` |
| `clippy-args` | `--workspace --all-targets -- -D warnings` | Lane: arguments after `cargo clippy --locked`; empty skips the lane |
| `test-args` | `--workspace` | Lane: arguments after `cargo test --locked`; empty skips the lane |
| `locked` | `true` | `--locked` on clippy and test (needs a committed `Cargo.lock`) |
| `workspace` | `.` | Cargo workspace directory, relative to the checkout |
| `runner-class` | `standard` | `standard` or `xlarge` (only after a measured need). A merge group runs on the class's `-merge` lane. No other label is reachable |
| `apt-packages` | empty | Debian packages the build needs |
| `git-deps` | empty | Private git dependencies, one `OWNER/REPO` per line, read through your own reader App; see [rust-check.md](rust-check.md) |
| `timeout-minutes` | `45` | Job timeout |

Secrets, passed by name: `SYLPHX_CI_CACHE_ACCESS_KEY` and
`SYLPHX_CI_CACHE_SECRET_KEY` (the organization's cache user; optional, the
fallback when BuildCache is not available), and
`GIT_DEPS_APP_ID` / `GIT_DEPS_APP_KEY` with `git-deps`.

Fixed by the workflow, because they are part of the cache key: the checkout
path, `CARGO_HOME` under the runner temp directory, `CARGO_INCREMENTAL=0`,
and `debug = 0` for the dev and test profiles. Callers never set `SCCACHE_*`,
`AWS_*` or `RUSTC_WRAPPER`; the `rust-sccache` action owns them.

Arguments and features are checked against a strict character set before
use and never reach a shell as text; anything else fails the first step.

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
