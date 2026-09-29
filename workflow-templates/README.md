# Workflow templates

Starter callers for the reusable workflows in `.github/workflows/`. Copy one
into a repository's `.github/workflows/`, replace the commit placeholder or pin
with the commit you have reviewed, and pass secrets by name.

| Template | Reusable workflow | Guide |
| --- | --- | --- |
| `optimistic-gate.yml`, `optimistic-verify.yml`, `red-main.yml` | merge-queue gate, post-merge verify, red-main handler | [optimistic-merge.md](../docs/optimistic-merge.md) |
| `rust-check.yml` | `rust-check.yml` | [rust-check.md](../docs/rust-check.md) |
| `ios-release.yml` | `ios-release.yml` (signing, archive, TestFlight upload; internal macOS runners) | [ios-release.md](../docs/ios-release.md) |
