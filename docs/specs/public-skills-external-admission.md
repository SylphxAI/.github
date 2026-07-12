# Public Skills External Admission Specification

Decision records:

- [`ADR-34-public-skills-external-admission.md`](../adr/ADR-34-public-skills-external-admission.md) establishes the external-admission mechanics.
- [`ADR-36-public-skills-cleanroom-control-plane.md`](../adr/ADR-36-public-skills-cleanroom-control-plane.md) binds the replacement identity, snapshot, and independent mutation boundary.

## Contract identity

| Fact | Required value |
| --- | --- |
| Source repository | `SylphxAI/.github` (`1091169653`) |
| Target repository | numeric ID `1297840366`, node ID `R_kgDOTVt47g` |
| Allowed target slugs | `SylphxAI/skills-public-cleanroom`, `SylphxAI/skills` |
| Workflow | `.github/workflows/public-skills-admission.yml` |
| Validator | `scripts/public-skills-admission.mjs` |
| Policy | `policies/public-skills-admission.json` |
| Required job | `public-skills-external-admission/pass` |
| Evidence file | `public-skills-external-admission.json` |

The policy manifest is the current snapshot source of truth. This document
defines mechanics and update order; it does not duplicate file digests.

## Event and runner contract

The workflow must:

- produce a terminal job for both `pull_request` and `merge_group` target
  events, without workflow-level path filters;
- run the admission job only for repository ID `1297840366`, which makes the
  same file harmless on source-repository pull requests;
- use `ubuntu-24.04`, `contents: read`, a timeout, and full-SHA action pins;
- omit `pull_request_target`, secret contexts, write permissions, self-hosted
  runners, and cancellation of an in-progress candidate;
- checkout the candidate at `github.sha`, with `fetch-depth: 0` and
  `persist-credentials: false`;
- checkout `SylphxAI/.github` at `github.workflow_sha`, separately from the
  candidate; and
- pass `github.event_name` as an explicit validator input and run only the
  validator from that source checkout.

The organization ruleset must bind the source repository, workflow path, and
exact merged source SHA. Workflow presence in this repository alone is not
target enforcement.

## Organization-ruleset executor contract

Doctrine is the only desired-state and lifecycle source of truth. The
canonical record is
`control-plane/github-rulesets/public-skills-external-admission.json` in
repository ID `1265184361`, `SylphxAI/doctrine`. The protected executor is
`scripts/public-skills-ruleset-executor.py` in repository ID `1091169653`,
`SylphxAI/.github`.

The executor resolves Doctrine's live default-branch `main` SHA through the
numeric repository endpoint and reads only that canonical record at the
resolved SHA. It does not download or execute the Doctrine schema, reconciler,
dependencies, workflow, candidate branch, or caller-selected path. The record
is parsed as bounded, duplicate-free, integer-only UTF-8 JSON. Unknown fields,
including a record-supplied executor or apply-authority field, fail closed.

Immutable safety invariants live in the executor rather than a second desired-
state document:

- organization `SylphxAI`, ID `206448049`;
- Doctrine repository ID `1265184361`, default branch `main`, and the one
  canonical record path;
- source/executor repository ID `1091169653`, default branch `main`, and exact
  workflow, validator, policy, executor, workflow-name, and check-name paths;
- clean target repository ID `1297840366`, node ID `R_kgDOTVt47g`, default
  branch `main`, and only the cleanroom and final slugs;
- default-branch-only target conditions, one workflow rule,
  `do_not_enforce_on_create: false`, and no bypass actors.

Any superseded or foreign repository identity is never an accepted target. The
executor parses the pinned workflow's numeric repository selector and requires
the exact clean target ID as its sole value; no literal denylist is used.

The request body is constructed from those closed invariants. Doctrine may
contribute only its admitted source SHA, numeric ruleset ID, phase, and desired
enforcement. No arbitrary desired-state field is copied into a GitHub request.
The lifecycle is closed:

1. `expand/evaluate` with a resolved source SHA and null ruleset ID may create
   one evaluate rule. The returned server ID must then be committed to
   Doctrine before any further mutation.
2. `reconcile/evaluate` requires that exact bound ruleset ID.
3. `ratchet/active` independently re-resolves and verifies evaluate readback,
   successful pull-request and merge-group runs, the exact failing negative
   control, rule-suite identities, source-policy baseline, chronology, and
   target effective-rules coverage. Doctrine evidence supplies locators and
   claims; it is never accepted without live reconstruction.
4. `recovery` preserves the same rule identity and permits only an enforcement
   downgrade to `evaluate` or `disabled`. It never deletes, adds a bypass, or
   repairs unrelated structural drift under recovery authority.

Dry-run is the default. `--readback` is read-only and omits a mutation plan;
`--apply` is the only write mode. There are no caller controls for repository,
record, host, endpoint, workflow, or payload. Authentication is read in memory
from the existing `github.com` GitHub CLI keyring. Token/host/config environment
overrides are stripped, redirects are rejected, and all REST calls use fixed
`https://api.github.com` endpoints.

Every canonical mutation is serialized by one source-owned lock. After exact
executor and actor verification, apply creates a unique annotated tag object at
the verified executor commit and atomically creates the fixed ref
`refs/tags/sylph-locks/public-skills-ruleset-executor` in repository ID
`1091169653`. The tag claim binds the repository, ref, executor commit, actor,
64-hex cryptographic nonce, and acquisition time as canonical JSON. The unique
tag-object SHA is the fencing identity even when consecutive runs share the
same executor commit. A foreign or malformed ref, ambiguous acquisition,
ownership loss, or release uncertainty fails closed.

The lock remains held across Doctrine/live reads, the ruleset request, and all
post/effective readbacks. Release first proves that the ref and annotated tag
still belong to this acquisition, deletes only the hard-coded ref, then requires
provider `404` readback. There is no TTL, force update, expiry, steal, or
automatic stale-lock recovery. A crash may halt future mutation until a
separately authorized incident recovery; safety wins over availability.
Dry-run and readback make no lock mutation.

Before a write, the executor re-reads both protected executor `main` and
Doctrine `main`; either moving since the initial read blocks the mutation.
Every write is followed by exact ruleset readback, and active enforcement also
requires effective-rules readback. A sent request followed by mismatching or
unavailable readback is an error, never reported as success.

GitHub does not expose a conditional organization-ruleset PUT. The fixed lock
is therefore mandatory for every supported writer. Direct administrator or
foreign-client mutation is outside the authorized writer set, must be treated
as an incident, and remains a provider-level override that readback can detect
but cannot cryptographically fence.

Output is canonical JSON evidence binding the executor commit and byte digest,
lock repository/ref/tag-object/message digest/executor commit/nonce/actor and
acquire/release outcomes,
Doctrine commit/blob/exact/semantic digests, source SHA and file blobs, target
identity, normalized pre-readback, exact request digest, mutation outcome, and
normalized post-readback. The top-level `evidenceDigest` covers the entire
report except itself. Credentials, headers, and desired-state bytes are never
emitted.

## Validator contract

The validator treats the candidate checkout as inert Git data and uses
`execFileSync` argument arrays rather than shell interpolation. It disables Git
replace objects and reads objects with `rev-list`, `ls-tree`, `cat-file`, and
`show`. It does not invoke candidate package managers, hooks, actions, scripts,
or language runtimes.

For all reachable refs plus detached HEAD in the checkout it must verify:

1. every ordinary reachable commit exactly matches one policy record: commit
   ID, tree ID, and ordered parent IDs;
2. all approved commits are reachable and exactly one approved fresh root
   exists;
3. the only permitted unknown commit is one GitHub-generated event HEAD with
   the pinned baseline tree and the exact ordered parent set selected by the
   explicit `pull_request` or `merge_group` event name;
4. pull-request dynamic HEAD uses `[base main, approved PR head]`; merge-group
   additionally permits the `[base main]` squash-queue shape;
5. every ref name and target is explicitly approved, except narrow dynamic
   pull/queue refs pointing to the permitted event HEAD; annotated tags,
   unknown branches, detached tag blobs, and other ref types are rejected;
6. every reachable commit belongs to the exact approved graph, and commit/blob
   counts remain bounded;
7. every historical path belongs to the approved physical allowlist;
8. paths are normalized printable ASCII without traversal, case collisions, or
   forbidden boundary segments;
9. every tree entry is a regular blob with an approved mode, with only the two
   explicitly pinned Python tools executable;
10. every commit object, tag object, and unique blob is UTF-8 text without NUL
    bytes, bidi controls, Git LFS indirection, secret signatures, or private
    markers;
11. HEAD has the exact approved Git tree, path set, and SHA-256 digest for every
    file; and
12. admissions, catalog, physical skill directories, eval files, and SKILL.md
    names agree on the exact eight IDs, MIT ownership, candidate channel,
    unverified state, and approved provenance; and
13. each skill's Git-tree manifest is recomputed as
    `git-tree-manifest-sha256-v1` and must match the source-owned file count and
    transfer-bundle digest. The manifest is a compact, whitespace-free UTF-8
    JSON array sorted by relative-path UTF-8 bytes. Every element has the exact
    property order `{path,mode,type,sha256}`: `path` is relative to the skill
    root, `mode` is `100644` or `100755`, `type` is `blob`, and `sha256` is the
    lowercase 64-hex SHA-256 of the raw blob bytes with no prefix. The bundle
    digest is the lowercase 64-hex SHA-256 of those serialized manifest bytes.
    This recursive projection lets downstream private authorization cross-bind
    exact public package bytes to the independently protected source policy
    without granting cross-private repository access; and
14. the launch graph may canonicalize through exactly one squash commit whose
    parent is the fresh root and whose tree is the exact approved launch tree.
    After that canonicalization, only one same-tree no-op canary commit plus
    its exact pull-request or merge-group event HEAD is admissible. This finite
    graph contract survives the merge queue without a source-policy rotation;
    any changed tree, partial launch graph, extra commit, or unapproved ref is
    rejected.

A benign text commit followed by a restore to the approved HEAD tree is still
rejected because its commit/tree graph is not source-approved. Empty commits
with the same tree are also rejected. Denylist cleanliness is never treated as
provenance.

Failures are fail-closed and produce a redacted error code and message. Secret
matches are never included in the report. The report binds source commit,
candidate commit/tree/root, target identities, scan counts, and skill IDs.

## Exact public allowlist

The IDs are:

1. `customer-support-operations`
2. `decision-memo-writer`
3. `fleet-migration-factory`
4. `interface-craft`
5. `market-research-synthesis`
6. `public-skill-repository-governance`
7. `skill-eval-designer`
8. `source-to-skill-distiller`

`decision-memo-writer`, `fleet-migration-factory`, `interface-craft`, and
`market-research-synthesis` are exact historical-public imports.
`customer-support-operations` and `source-to-skill-distiller` are bounded
historical-public derivatives. `skill-eval-designer` is an explicitly
declassified public derivative. `public-skill-repository-governance` is a
public-original skill and must not claim an import commit.

## Source-first change protocol

Exact graph and full-tree pinning means every target history or byte change,
including final repository-identity edits, requires a prior source-policy
commit/tree/ref and digest update. After that source PR merges, GitOps ratchets
the organization ruleset to the new source SHA. Only a target candidate
evaluated by that exact source SHA is eligible for the merge queue.

Local proof for a checked-out candidate is:

```sh
PUBLIC_SKILLS_CANDIDATE=/path/to/skills-public-cleanroom \
  node --test tests/public-skills-admission.test.mjs
```

Offline executor proof is:

```sh
python3 -B tests/test_public_skills_ruleset_executor.py
```

The CLI used by GitHub additionally binds the checked-out source SHA to
`github.workflow_sha`; see the workflow for the complete argument contract.
