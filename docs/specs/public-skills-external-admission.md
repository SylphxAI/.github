# Public Skills External Admission Specification

Decision record:
[`ADR-34-public-skills-external-admission.md`](../adr/ADR-34-public-skills-external-admission.md).

## Contract identity

| Fact | Required value |
| --- | --- |
| Source repository | `SylphxAI/.github` (`1091169653`) |
| Target repository | numeric ID `1297721845`, node ID `R_kgDOTVmp9Q` |
| Allowed target slugs | `SylphxAI/skills-public-staging`, `SylphxAI/skills` |
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
- run the admission job only for repository ID `1297721845`, which makes the
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
6. forbidden private commits are unreachable, and commit/blob counts remain
   bounded;
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
    unverified state, and approved provenance.

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

The first five and final two are historical public imports bound to their
reviewed source commit and path. `public-skill-repository-governance` is a
public-original skill and must not claim an import commit.

## Source-first change protocol

Exact graph and full-tree pinning means every target history or byte change,
including final repository-identity edits, requires a prior source-policy
commit/tree/ref and digest update. After that source PR merges, GitOps ratchets
the organization ruleset to the new source SHA. Only a target candidate
evaluated by that exact source SHA is eligible for the merge queue.

Local proof for a checked-out candidate is:

```sh
PUBLIC_SKILLS_CANDIDATE=/path/to/skills-public-staging \
  node --test tests/public-skills-admission.test.mjs
```

The CLI used by GitHub additionally binds the checked-out source SHA to
`github.workflow_sha`; see the workflow for the complete argument contract.
