# Workflow templates

Starter callers for the reusable workflows in `.github/workflows/`. Copy one
into a repository's `.github/workflows/`, replace the commit placeholder or pin
with the commit you have reviewed, and pass secrets by name.

Rolling a new check out to many repositories: run it on each target's default branch first, with every
rule enforced (a manual run, or the check's own audit command, such as `keel_pin_check.py audit`). A target
that already fails gets the fix in the same pull request that adds the check, or is skipped and listed;
adding a check alone to a branch that fails it turns that branch red.

| Template | Reusable workflow | Guide |
| --- | --- | --- |
| `optimistic-gate.yml`, `optimistic-verify.yml`, `red-main.yml` | merge-queue gate, post-merge verify, red-main handler | [optimistic-merge.md](../docs/optimistic-merge.md) |
| `rust-ci.yml` | `rust-ci.yml` (format, clippy and tests on the organization's shared compile cache) | [rust-ci.md](../docs/rust-ci.md) |
| `rust-check.yml` | `rust-check.yml` | [rust-check.md](../docs/rust-check.md) |
| `ios-release.yml` | `ios-release.yml` (signing, archive, TestFlight upload; internal macOS runners) | [ios-release.md](../docs/ios-release.md) |
| `keel-repin.yml` | the `keel-repin` action (repin PR for a Keel title; no reusable workflow) | [keel-repin.md](../docs/keel-repin.md) |
| `keel-pin-check.yml` | the `keel-pin-check` action (fails a pull request that pins Keel off Keel main or to two commits; warns when the pin is not a verified tag; no reusable workflow) | [keel-pin-check.md](../docs/keel-pin-check.md) |
| `workflow-pin-check.yml` | the `workflow-pin-check` action (fails a pull request that pins another of our repositories to a commit that is not on its default branch, which becomes unreachable after a squash merge; no reusable workflow) | [workflow-pin-check.md](../docs/workflow-pin-check.md) |
| `web-smoke.yml` | the `web-smoke` action (Keel web pack: Slow 3G splash and a drawn frame; no reusable workflow) | [web-smoke.md](../docs/web-smoke.md) |
| `pages-publish.yml` | the `pages-publish-gate` action (a web preview deploy from every passing main run that is ahead of the commit it serves; never goes back; no reusable workflow) | [pages-publish-gate.md](../docs/pages-publish-gate.md) |
