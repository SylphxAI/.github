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
and unique tag-object SHA form the fencing identity. A read-only preflight binds
the exact Doctrine revision, desired payload digest, planned action, and
pre-readback revision into that immutable claim. The executor verifies exact
ownership and rebuilds the same authorization before a ruleset request, holds
the ref through post-readback, and deletes it only after proving it still
points to that acquisition. Lock contention blocks. Dry-run and readback never
touch the lock. A crashed owner may leave a fail-closed stale lock; expiry,
stealing, force-update, and automatic recovery do not exist.

Successful activation also creates a durable provider witness after lock
release and confirmed absence. Source-owned policy
`policies/public-skills-activation-attestation-ruleset.json` declares one
active, zero-bypass organization tag ruleset over repository ID `1091169653`
and the exact nonce-scoped prefix. It forbids update, deletion, and
non-fast-forward changes but deliberately permits first creation. The
provider-assigned ruleset ID is discovered and bound in evidence rather than
hard-coded. Organization policy bytes are cross-checked with the pinned
actor's repository-effective `current_user_can_bypass: never` observation.

The executor rechecks executor, Doctrine, target, live/effective state, and
that immutable tag ruleset, then creates the deterministic annotated tag/ref
`refs/tags/sylph-attestations/public-skills-ruleset/<lock-nonce>`. Its claim
binds the full lock claim/tag SHA and released/absent lifecycle, executor bytes,
actor, pre-ratchet Doctrine record, desired payload, ruleset ID, exact pre/post
state and effective digests, real provider request ID, source policy identity,
and live attestation-ruleset ID/digest. An exact existing ref is idempotent;
foreign, mutable, overwritten, reused, or missing evidence fails closed.
Attestation failure produces a sealed pending-attestation report that a narrow
finalizer may reconcile without another organization-ruleset write.
The lock authorization itself binds this exact attestation-ruleset evidence;
the executor rechecks it immediately before activation and again after
permanent-ref creation. Post-release recovery reconstructs the lock claim and
digest but never assumes the deleted ephemeral tag object survives Git GC.

Apply re-reads executor main, Doctrine main, target identity, live ruleset, and
activation evidence immediately before writing. Every write requires exact
post-readback; active state additionally requires effective-rules readback.
Canonical reports bind actor, executor, desired-state, source, target,
precondition, request, and postcondition digests without emitting credentials
or desired-state bytes. A bounded collector persists only a privacy-safe audit
allowlist, combines it with current live/effective readback and the permanent
attestation, then recaptures all current provider state after audit lookup and
immediately before sealing the fixed Doctrine activation artifact. `active` is a
readback-only steady state and re-verifies the exact historical record,
executor, sealed canary-summary cross-bindings, artifact, provider attestation,
immutable tag ruleset, and current state without mutation. It deliberately
does not depend on retention-limited historical Actions or pull-request APIs,
or on the deleted ephemeral lock tag object after its claim has been
live-verified and durably sealed into the immutable provider attestation.

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
- A protected Doctrine commit plus an internally hashed JSON report is not
  sufficient activation proof; the provider-hosted immutable attestation ref
  is the durable transition witness.
- GitHub exposes no conditional organization-ruleset PUT. The lock serializes
  cooperating authorized writers, while exact live readbacks detect but cannot
  cryptographically fence a provider administrator acting outside the contract.
- Source-policy changes remain source-first and require new canaries.
- The source policy, Doctrine record, private authorization, release identity,
  and live rule must all ratchet to repository ID `1297840366`.
- Uncertain post-write readback is an error requiring live reconciliation, not
  success.
- Attestation unavailability never permits a second active mutation. The exact
  pending report is finalized idempotently, while ratchet/live-active remains
  blocked pending transition evidence.

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
