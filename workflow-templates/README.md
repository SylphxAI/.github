# Workflow templates

Starter callers for the reusable workflows in `.github/workflows/`. Copy one
into a repository's `.github/workflows/`, replace the commit placeholder or pin
with the commit you have reviewed, and pass secrets by name.

| Template | Reusable workflow | Guide |
| --- | --- | --- |
| `optimistic-gate.yml`, `optimistic-verify.yml`, `red-main.yml` | merge-queue gate, post-merge verify, red-main handler | [optimistic-merge.md](../docs/optimistic-merge.md) |
| `rust-check.yml` | `rust-check.yml` | [rust-check.md](../docs/rust-check.md) |
| `ios-release.yml` | `ios-release.yml` (signing, archive, TestFlight upload; internal macOS runners) | [ios-release.md](../docs/ios-release.md) |
| `keel-repin.yml` | the `keel-repin` action (repin PR for a Keel title; no reusable workflow) | [keel-repin.md](../docs/keel-repin.md) |
| `keel-pin-check.yml` | the `keel-pin-check` action (fails a pull request that pins Keel off Keel main or to two commits; warns when the pin is not a verified tag; no reusable workflow) | [keel-pin-check.md](../docs/keel-pin-check.md) |
| `web-smoke.yml` | the `web-smoke` action (Keel web pack: Slow 3G splash and a drawn frame; no reusable workflow) | [web-smoke.md](../docs/web-smoke.md) |
