# ADR 0004: One Rust CI workflow and one compile cache for every organization

## Status

**Proposed** (2026-10-04). Applies to every repository in SylphxAI, Cubeage
and EpiowAI whose CI compiles Rust.

## Context

The industry standard for Rust CI at fleet scale is a remote compile cache
keyed by the content of each compiler invocation (sccache, Bazel's remote
cache), shared by every branch of a repository and written by the trunk, so a
pull request compiles only what it changed. GitHub's own guidance for the
Actions cache follows the same shape: a branch reads its base branch's
entries, and the default branch writes them.

We have most of the parts, but not the fleet:

Inventory of the default branches on 2026-10-04: 48 repositories in SylphxAI,
Cubeage and EpiowAI compile Rust in CI.

| Compile cache today | Repositories |
| --- | --- |
| Object store (in-cluster RGW), warmed by main | 3 (SylphxAI/keel, SylphxAI/cloud on its own bucket, one EpiowAI repository) |
| `rust-sccache` wired, but the credential never reaches the job, so it falls back to the Actions cache | 14 (2 of them never run on main, so nothing warms even that) |
| `Swatinem/rust-cache` on the Actions cache | 5 (2 never run on main) |
| None | 18 (most Keel titles, plus 5 SylphxAI services) |
| Public, on GitHub-hosted runners, on the Actions cache | 8 (out of scope: off our network the Actions cache is the right backend, and fork pull requests stay off our runners) |

No compile-cache key contains a branch, ref or commit; the failures are the
missing cache, the missing credential, and the missing main run.

What exists today:

- [`actions/rust-sccache`](../../.github/actions/rust-sccache/action.yml)
  installs a pinned sccache and picks a backend, first that applies: a
  per-organization credential the runner would carry (`SYLPHX_SCCACHE_*`),
  the organization's own object-store user passed as secrets
  (`SYLPHX_CI_CACHE_*`, bucket `ci-sccache-<org>` on the in-cluster Ceph RGW,
  14-day expiry), then the repository's GitHub Actions cache. It never uses
  the platform's own `ci-sccache` bucket.
- [`rust-check.yml`](../../.github/workflows/rust-check.yml) is a
  dispatch-only `cargo check` service for `sylphx build check`. It uses that
  action with one organization-wide namespace (`key-prefix: rustc`).
- SylphxAI/cloud runs its own setup (`scripts/ci/setup-rust.sh`) on the
  platform `ci-sccache` bucket with the `rustc` prefix, warmed by its
  post-merge verify run and a weekly empty-cache run.
- The remote-build decision in SylphxAI/cloud
  (`docs/adr/ADR-01M41HZMPJB7Y53TH2VXNFW3QG-remote-build-execution.md`,
  section 4) adds **BuildCache**: a gateway in front of the same Ceph RGW
  that speaks the sccache WebDAV protocol, with tenancy per organization and
  cache, a `dev` and a `protected` scope, and short-lived run tokens. It names
  CI's move from static object-store keys to that gateway as a later outcome.
  This ADR is that outcome's plan.

What goes wrong:

1. **No cache, or a cache a pull request cannot read.** Repositories without
   a compile cache build every crate from scratch on every run.
   `Swatinem/rust-cache` keys a `target/` tarball per job, and the Actions
   cache is scoped by ref, so a new pull request restores only what main
   saved under the same key, and nothing at all where main never runs the
   job.
2. **The credential does not reach the job.** The organization secrets that
   select the object-store backend are visible to too few repositories (one,
   in SylphxAI), or do not exist (Cubeage). A caller that passes them by name
   gets empty values and silently falls back to the GitHub Actions cache.
3. **The GitHub Actions cache is the wrong backend for our runners.** Our
   runners are in our own cluster; the Actions cache is across the internet
   (measured near 5 MB/s from our runners in SylphxAI/cloud's CI), is capped
   at 10 GB per repository with eviction, and is scoped by ref.
4. **The runner-carried credential does not exist yet.** The action's first
   backend expects the runner platform to inject `SYLPHX_SCCACHE_*`; nothing
   injects it today, so that branch never runs.

CI wall time before this change (2026-10-04, green runs whose first job
started that day, read from the checks of the open pull request heads and
the main tip; first job start to last job end, so queue time before the
first job is excluded):

| Repository | Gate run wall time, median (p90) | Cache-sensitive job, median | Green runs |
| --- | --- | --- | --- |
| SylphxAI/keel (`ci.yml`) | 23.9 min (26.5) | Tests (affected crates) 18.0 min; Check and clippy 4.3 min | 11 |
| SylphxAI/cloud (`ci.yml`) | 163 min (240), including time between dependent jobs | rust proofs lane 25.1 min; rust partition 1/3 6.8 min | 17 |
| A Keel title (Cubeage; `ci.yml` with its full suite on a ready pull request) | 64.4 min (67.2) | suite / build 40.6 min; unit 9.8 min | 6 |

keel reads the object store; the Keel title passes the cache secrets by name
but its organization has none, so it compiles against the Actions cache.

## Decision

### 1. One reusable workflow, one cache owner

- [`rust-ci.yml`](../../.github/workflows/rust-ci.yml) is the standard Rust
  gate: `cargo fmt --check`, `cargo clippy`, `cargo test`, on one Sylphx
  runner. A repository adopts it with the
  [starter](../../workflow-templates/rust-ci.yml) and the
  [guide](../rust-ci.md).
- A repository whose gate has bespoke jobs (device lanes, release builds,
  WebAssembly size budgets) keeps those jobs and calls the same action in each
  Rust job, with the same namespace. The cache is the shared part; the job
  graph is the repository's.
- `actions/rust-sccache` is the only place that configures sccache. Callers
  never set `SCCACHE_*`, `AWS_*` or `RUSTC_WRAPPER` themselves, so a backend
  change is one change here plus one pin bump per caller.
- `rust-check.yml` stays: it is a different contract (dispatched by the CLI,
  with output markers), and it already uses the same action and namespace.

### 2. Backend: the object store Sylphx Build uses

The cache lives on the in-cluster Ceph RGW, the store BuildCache serves
from. Two phases, so the fleet gets a warm cache now without waiting for the
gateway:

- **Phase 1 (now).** The organization's own object-store user and bucket
  (`ci-sccache-<org>`), passed as the organization secrets
  `SYLPHX_CI_CACHE_ACCESS_KEY` / `SYLPHX_CI_CACHE_SECRET_KEY`. The users and
  their quotas already exist for SylphxAI, Cubeage, EpiowAI and OzyrixLtd.
  Each organization's secrets must exist and be visible to every repository
  that compiles Rust. The values are copied from the object-store user by the
  operator; they are never printed or committed.
- **Phase 2 (when BuildCache's protected-scope minting ships).** The action's
  first backend becomes BuildCache: the job exchanges its GitHub OIDC token
  (`id-token: write`) at the gateway for a run token (claims: organization
  from `repository_owner_id` through the gateway's table of GitHub
  organizations, cache `ci`, scope from the event, expiry at the
  job deadline) and points sccache at `SCCACHE_WEBDAV_ENDPOINT`. This replaces
  the runner-carried credential slot, which was never delivered. The static
  secrets and the per-organization buckets retire once every caller has
  bumped its pin, and SylphxAI/cloud moves off `ci-sccache` to the same path.
- **Fallback, in order:** the next backend, then the GitHub Actions cache,
  then none. The cache only ever speeds a job up: a backend that is
  unreachable compiles cold with a warning, never a failure.

### 3. Key scheme

sccache's key is a hash of the whole rustc invocation: the compiler binary,
the arguments, the `CARGO_*` environment and the digests of every source
file. We choose only the namespace it lives in:

- **One namespace per organization**: prefix `rustc` in the organization's
  bucket (phase 2: `{org}/ci/{scope}/`). No branch, ref, commit, run, job or
  repository in it.
- So a pull request reads every entry that main, the merge queue, or a sibling
  repository already wrote for the same inputs, and nothing else can match.
  Keel titles that build the same Keel revision share its compiled crates.
- A repository in the prefix would add no correctness (the key is the
  content) and would forbid those cross-repository hits. The organization is
  the isolation boundary: one organization's entries are never readable or
  writable by another, because an entry cannot be verified without
  rebuilding it.
- For keys to match across events, the inputs must match: the default
  checkout path, `CARGO_HOME=$RUNNER_TEMP/cargo-home`, `CARGO_INCREMENTAL=0`, a
  pinned toolchain (`rust-toolchain.toml`) and the same features, profile and
  `RUSTFLAGS` as main's run. `rust-ci.yml` fixes the first three.

**Scopes.** In phase 1 every run of an organization, pull requests included,
reads and writes the one namespace. A pull request from a fork receives no
secrets and so never writes; a pull request from a member could write an
entry that a later main build reads. Members are the same people who can push
to main, so phase 1 accepts that. Phase 2 removes it with BuildCache's scope
model, which is the Actions-cache model: pull requests read `protected` then
`dev` and write `dev` only; pushes to main, merge groups and schedules read
and write `protected` only.

### 4. Main keeps the cache warm

- Every caller runs its Rust jobs on `push` to main, and the starter keeps
  that trigger. Main and merge-group runs are never cancelled by concurrency,
  so they always finish writing.
- A pull request opened after the last main run finds every unchanged crate
  in the cache. A repository that merges rarely still benefits from its
  siblings' writes of shared dependencies.
- The bucket's 14-day expiry (phase 2: BuildCache retention, 7 days by last
  read) bounds the store. SylphxAI/cloud keeps its weekly empty-cache run;
  other repositories do not need one.

### 5. Zero GitHub spend

- `rust-ci.yml` can only reach Sylphx runner labels (`sylphx-linux-standard`,
  `sylphx-linux-xlarge` and their `-merge` lanes); a test fails any GitHub-
  hosted label.
- The object store is our own Ceph cluster. The GitHub Actions cache is only a
  fallback, inside the free per-repository allowance; the organization's
  Actions cache storage limit is never raised, since that is what bills.

### 6. Rollout and measurement

- One pull request per repository (or per batch of identical repositories)
  pins `rust-ci.yml`, or adds the action to a bespoke gate, and adds the push
  to main. The first three are SylphxAI/keel, SylphxAI/cloud and
  Cubeage/fun-mahjong-keel; each records its wall time and hit rate before and
  after.
- Each run writes the backend and hit rate to its job summary, so the after
  numbers come from the run itself.
- Success: on a pull request that changes one leaf crate, the cache hit rate
  on the other crates is at least 90%, and the median green gate wall time is
  lower than the before number on the same job set.

## Alternatives rejected

- **`Swatinem/rust-cache` or `actions/cache` on `target/`.** One tarball per
  job key on the Actions cache: ref-scoped, 10 GB per repository with eviction,
  slow from our runners, never shared across repositories, and a pull request
  restores main's only if main saved under the same key. It is the right tool
  on GitHub-hosted runners; ours are not.
- **sccache on the GitHub Actions cache as the primary backend.** Same link
  and cap, and thousands of small entries per build meet the cache service's
  rate limits.
- **The platform's `ci-sccache` bucket for everyone.** One writer could poison
  every organization's builds, and a shared store tells one organization what
  another compiles. Rejected in the remote-build decision for the same reason.
- **A repository, branch or commit in the namespace.** No correctness gain
  over a content key; it only lowers the hit rate. Branch-scoped keys are the
  cause of today's cold pull requests.
- **Persistent `target/` directories on long-lived runners.** Stateful runners
  leak one job's output into the next and need disk management per runner.
  Warm volumes belong to Sylphx Build's remote runs, not to CI.
- **A moving `@v1` tag for callers.** Callers here pin by commit. The backend
  choice is inside the action, so pin bumps are rare and scripted.
- **Waiting for BuildCache before rolling out.** The per-organization users
  already exist; phase 1 delivers the warm cache now, and phase 2 is a change
  inside the action.

## Migration and rollback

- **Adoption** is one pull request per repository. Rollback is reverting it.
- **Cache failure** never fails a build (fail-open), so an outage of the object
  store costs time, not correctness.
- **A bad or poisoned entry**: change the namespace (`rustc` to `rustc-2`) in
  this repository and bump callers, or have the operator delete the
  organization's prefix; the expiry bounds what remains.
- **Phase 2** keeps the static backend as the next fallback until every
  caller has moved; rollback is the previous action commit.
- **Secrets**: widening an organization secret's visibility is reverted by
  narrowing it again; no value changes.

## Validation

- `tests/test_rust_ci.py`: only Sylphx runner labels are reachable; the cache
  goes through the action with the one `rustc` namespace; no ref, commit, run
  or platform key appears in the job; caller inputs never reach a shell body
  and shell metacharacters are refused; the starter keeps the push to main and
  cancels only pull-request runs; every run reports its hit rate.
- `tests/test_rust_sccache_backend.py` (existing) covers the backend order;
  phase 2 adds the BuildCache case and its fallback.
- On adoption, each of the first three repositories shows: a second main run
  with a hit rate of 90% or more on unchanged inputs; a pull request touching
  one leaf crate hitting every other crate; the before and after median wall
  time on the same job set.
- Across repositories: two Keel titles at the same Keel revision, where the
  second title's Keel crates are hits.

## Out of scope

- Public repositories on GitHub-hosted runners (above).
- Rust compiled inside container image builds (`docker build`), which needs
  BuildKit cache mounts or sccache inside the build; a separate decision.
- Caching test results; SylphxAI/cloud's lane result cache covers that for
  cloud.

## Consequences

- Every Rust repository's pull requests compile only what they change, from
  the first push.
- One action owns the cache; moving the fleet to BuildCache is one change
  there and one pin bump per caller.
- Organization secrets become a dependency of every Rust repository until
  phase 2 replaces them with the job's own identity.
