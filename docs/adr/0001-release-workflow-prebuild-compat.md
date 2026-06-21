# ADR 0001: Keep Release Workflow Input Compatibility

## Status

Accepted

## Context

`SylphxAI/.github/.github/workflows/release.yml` is an organization-level
reusable workflow consumed directly from repository release workflows. A caller
that passes an unknown workflow input fails during GitHub Actions startup,
before any job log exists.

At least one consumer uses `prebuild` for the build command while the reusable
workflow defines `build`. Requiring every consumer to change immediately creates
repo-by-repo operational work and keeps release startup failures active until
all callers are migrated.

## Decision

The release reusable workflow accepts `prebuild` as a legacy alias for `build`.
New callers must use `build`; `prebuild` is supported only to preserve existing
consumer workflows. When both are set and no artifact is supplied, `prebuild`
takes precedence to preserve legacy caller behavior.

The public contract is documented in `docs/specs/release-workflow.md`.

## Consequences

- Existing consumers using `prebuild` stop failing at workflow startup.
- New consumers have one canonical input, `build`.
- Consumers must still grant the reusable workflow sufficient job permissions;
  GitHub does not allow the callee to raise caller token scope.
- The organization can later remove `prebuild` only through an audited migration
  that proves no callers still use it.
