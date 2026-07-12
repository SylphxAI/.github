# ADR-36: Public Skills cleanroom control plane

## Status

Accepted in PR #36.

## Context

The public-skills required workflow must be owned outside its target, while the
organization-ruleset desired state remains in `SylphxAI/doctrine`. Executing a
Doctrine-supplied reconciler or schema before mutation would let one Doctrine
change redefine both requested state and mutation authorization.

Two private staging identities were quarantined before publication. One
contained post-privacy source history. The next contained no commercial skill
leakage, but its bootstrap root retained superseded identity and internal
control-plane metadata. A cleanup commit could not remove those bytes from
reachable public history.

The replacement target is repository ID `1297840366`, node ID
`R_kgDOTVt47g`, initially `SylphxAI/skills-public-cleanroom` and finally
`SylphxAI/skills`. Its sanitized graph has bootstrap root
`e477aee5c1d93b2bac8619fdc6f15f27483855a3` and candidate
`f74c83d966331193d6a2f325173094c5d5c51762` with tree
`04c100fed8c99a72290896a6a825ee68f6617331`. Superseded or foreign identities
must never be selected by new organization policy.

## Decision

The source-owned admission policy admits only the replacement numeric/node
identity, exact two-commit graph, 73-file snapshot, eight-package provenance
map, generic boundary markers, and secret rules. Quarantined repositories
remain private and cannot receive the canonical slug.

`SylphxAI/doctrine` remains the single desired-state, lifecycle, schema, and
evidence-claim authority. Protected public repository `SylphxAI/.github`, ID
`1091169653`, owns `scripts/public-skills-ruleset-executor.py`.

The executor fetches Doctrine by fixed numeric repository ID, resolves protected
`main`, and reads only the canonical ruleset record at that immutable commit.
It never downloads or executes Doctrine code, schema, dependencies, or target
candidate code. A closed parser rejects unknown or unsafe record shapes.

Non-configurable executor invariants bind organization/source/target identities,
Doctrine path, source files/check, target default branch and rename states, one
default-branch workflow rule, no bypass, approved credential actor, and the
lifecycle matrix. GitHub request bodies are independently rebuilt from those
invariants; a planner envelope is never mutation authority.

`expand/evaluate` may create one evaluate rule. `reconcile/evaluate` may
repair an exact bound evaluate rule but cannot downgrade active enforcement.
`ratchet/active` requires fresh provider-derived canaries for the current live
evaluate-rule revision, full evidence reconstruction immediately before write,
and effective-rules proof. `recovery` alone may downgrade enforcement on the
same structurally exact rule. Delete and bypass operations do not exist.

Dry-run is default, `--readback` is read-only, and `--apply` is the only
write mode. Host, repository, record, endpoint, actor, and payload are not
caller-selectable. The executor pins and verifies the approved `github.com`
credential actor, strips relevant environment overrides, uses fixed-host HTTPS,
and rejects redirects.

Every authorized `--apply` first creates a unique annotated Git tag object in
repository ID `1091169653`, then atomically acquires the single fixed ref
`refs/tags/sylph-locks/public-skills-ruleset-executor`. The cryptographic nonce
and unique tag-object SHA form the fencing identity. The executor verifies
exact ownership before a ruleset request, holds the ref through post-readback,
and deletes it only after proving it still points to that acquisition. Lock
contention blocks. Dry-run and readback never touch the lock. A crashed owner
may leave a fail-closed stale lock; expiry, stealing, force-update, and automatic
recovery do not exist.

Apply re-reads executor main, Doctrine main, target identity, live ruleset, and
activation evidence immediately before writing. Every write requires exact
post-readback; active state additionally requires effective-rules readback.
Canonical reports bind actor, executor, desired-state, source, target,
precondition, request, and postcondition digests without emitting credentials
or desired-state bytes.

## Consequences

- No contaminated or superseded staging commit belongs to the replacement
  graph.
- The target cannot change its judge or organization selector.
- Doctrine can request state but cannot redefine the executable that authorizes
  it; `.github` can change executor code but cannot author desired state.
- Creation stops after evaluate until the server-assigned ruleset ID is
  committed to Doctrine.
- Every canonical writer shares one durable lock; an out-of-band administrator
  mutation is a policy violation and an incident, not a second supported writer.
- GitHub exposes no conditional organization-ruleset PUT. The lock serializes
  cooperating authorized writers, while exact live readbacks detect but cannot
  cryptographically fence a provider administrator acting outside the contract.
- Source-policy changes remain source-first and require new canaries.
- The source policy, Doctrine record, private authorization, release identity,
  and live rule must all ratchet to repository ID `1297840366`.
- Uncertain post-write readback is an error requiring live reconciliation, not
  success.

## Alternatives considered

### Add cleanup commits to contaminated staging histories

Rejected. Reachable historical objects would remain public.

### Execute the Doctrine reconciler from Doctrine main

Rejected. Desired state and mutation authorization would share one change
boundary.

### Copy desired state into `.github`

Rejected. It creates a second policy source and reconciliation drift.

### Activate immediately

Rejected. It skips evaluate-mode PR, merge-group, negative-control, and
effective-rules proof.

## Verification

```sh
PUBLIC_SKILLS_CANDIDATE=/path/to/fresh-clone \
  node --test tests/public-skills-admission.test.mjs
python3 -B tests/test_public_skills_ruleset_executor.py
actionlint .github/workflows/*.yml
git diff --check
```
