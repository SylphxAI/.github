# Merge queue settings

One file, [`policy/merge-queue.json`](../policy/merge-queue.json), holds the
parameters of the merge queue for every repository. [`scripts/apply_merge_queue.py`](../scripts/apply_merge_queue.py)
puts them on each repository's ruleset.

## Why a tool and not an organization ruleset

GitHub does not accept a `merge_queue` rule in an organization or enterprise
ruleset (read 2026-10-05):

- Docs, *Available rules for rulesets*, "Require merge queue": "This rule is
  not available for rulesets created at the organization level." It can be
  required only "at the repository level".
  <https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets>
- REST schema (rest-api-description, `ghec.2022-11-28`): `POST
  /orgs/{org}/rulesets` and `POST /enterprises/{enterprise}/rulesets` take
  `org-rules` and `enterprise-rules`, neither of which lists `merge_queue`;
  only `repository-rule` does (`repository-rule-merge-queue`).
  <https://docs.github.com/en/rest/orgs/rules>

So the enterprise ruleset `agent-native-queued-trunk-base` carries the shared
branch rules (`deletion`, `non_fast_forward`, `pull_request`), and the queue is
set once per repository, here.

## The settings

HEADGREEN: the merge-group head must pass the required checks. The CI check is
`ci-ok`; explicitly scoped `required_checks` entries also require the independent
`review-ok` status from the sole review publisher App. The full suite still runs
after the merge ([optimistic-merge.md](optimistic-merge.md)). Values:
`policy/merge-queue.json`
`settings`. `max_entries_to_build` is 5 because the shared merge-queue runner
pool bounds build concurrency (the cloud merge-queue runbook records the
measurements); change a number in the policy file in a pull request, then apply.

A repository that needs another value lists an `overrides` entry with its ruleset
name and a reason; `fast_gate_overrides` does the same for a ruleset gated by
another check (cloud's `deploy/pins` branch by `pin-ok`). `exclude` lists
repositories never read or written; the hands-off repositories of
`policy/optimistic-merge.json` are always excluded.

## Required approving reviews: none

Repository `pull_request` rules require zero human approving reviews (policy
`review`). Independent review is instead the exact-head `review-ok` status,
bound to the `sylphx-desk-studio` App (integration ID `5155317`). It is posted
on PR heads and merge-group commits, not implemented as a CI job. An undecided,
withdrawn or blocking verdict cannot satisfy it. The enterprise ruleset keeps
zero approving reviews; the status check is required by the repository ruleset.

The count reaches every repository ruleset of every organization in `orgs`,
which is the same four-organization fleet as `policy/optimistic-merge.json`:
`SylphxAI`, `Cubeage`, `EpiowAI` and `OzyrixLtd`. An approval held by any of
them blocked a merge the fast gate had already passed. A repository ruleset
that has a `pull_request` rule but no queue (a tags or non-default-branch
ruleset) is converged too, because the rule holds a review before every merge.
The hands-off repositories of `policy/optimistic-merge.json` are excluded and
keep their rule as it is: `SylphxAI/bgca`, `Cubeage/hk-mahjong-tycoon` and
`SylphxAI/openclaw-sylphx`. Before planning, the tool also reads each selected,
non-excluded repository's custom properties. It excludes `sylphx_delivery=delivered`
and unreadable properties, so neither can produce a ruleset write. A property
read answering HTTP 403 stops the run before any write.

One run reaches all four: `orgs` is read as `[org for org in policy["orgs"] for
item in read_org(gh, org)]`, one GraphQL page per organization.

## What the tool manages

The tool manages merge-queue parameters, approving review counts and explicitly
scoped required-check bindings. `required_checks` names the exact repository,
ruleset ID and name, a reason, and `{context, integration_id}` checks. It adds
missing checks or corrects their publisher bindings without deleting unrelated
checks, changing the strict flag, or altering conditions or bypass actors.
Unscoped rulesets keep their required checks unchanged. A missing declared
target or a changed ruleset name refuses apply rather than silently missing a
repository. Dry-run reports required-check drift and App IDs; apply reads back
the resulting bindings, and the existing saved-ruleset rollback restores them.

A ruleset inherited from an organization or enterprise ruleset (GraphQL `source`
not `Repository`) is reported `INHERITED` and never written: REST has no path to
write it, and the enterprise ruleset already requires 0 approvals. A new
repository therefore needs no manual step for the review count; the next run
reads whatever repository ruleset it carries, queue or not.

## Review gate rollout barrier

The first declared targets are cloud (`20321070`) and infra (`23689347`). The
policy is desired state, not a claim that a live gate is already enabled. Do
not apply an entry until all of the following have actual readbacks:

- The reviewed producer is merged and installed by desk-sync.
- Backfill posted `review-ok=success` on every open canonical-PASS head.
- Normal posting on PR heads and merge-group commits has run for at least an
  hour, including undecided and blocking verdicts remaining non-success.
- The ReviewGate VMRule is live.
- Cloud's App-binding/runner guard support is landed, and its
  `.github/branch-protection-contract.json` activation is updated in the same
  rollout step as the live ruleset.

Guard support must precede enforcement: CI compares the proposed cloud contract
against live rules, so activating its JSON before the producer/live-rule barrier
would make CI fail. First land binding-reader and external-status support with
the existing `ci-ok` floor unchanged. At the barrier, activate `review-ok` and
its App binding in the cloud contract and apply only cloud and infra. Send heads
for independent review only after their native CI is green.

After those two readbacks, extend the same policy to work (`24403812`),
desk-tools (`24403587`) and .github (`20321046`) with their provider-confirmed
ruleset names. Every rollout keeps the queue and `ci-ok`. No TSV armer, private
queue, polling loop or synthetic CI job is part of this path.

Rollout progress: cloud and infra were applied on 2026-10-15 and read back
(`review-ok` present, bound to integration `5155317`), with the pre-change
rulesets saved in a ruleset-backup directory on the operator host, which is the
rollback source for every entry of this rollout. work, desk-tools and .github
are declared here and applied in the same rollout.

Save a fresh backup directory per rollout. If posting stops or the gate blocks
approved heads, use `--rollback` with that directory and read back the restored
App bindings and CI gate. A successful PUT alone is not an apply or rollback
receipt.


## Commands

```sh
scripts/apply_merge_queue.py --dry-run                    # diff of every ruleset (the default)
scripts/apply_merge_queue.py --repo SylphxAI/desk-tools   # one repository
scripts/apply_merge_queue.py --check                      # exit 1 on any drift (for a schedule)
scripts/apply_merge_queue.py --apply --backup-dir DIR     # write; saves each ruleset in DIR first
scripts/apply_merge_queue.py --rollback DIR               # put the saved rulesets back
```

Reads are one GraphQL query per 100 repositories plus one custom-property read
per selected, non-excluded repository. Writes are one `PUT` per
drifted ruleset, a second apart, read back, and stop at the first 403. The
credential needs repository administration on each repository. Running
`--apply` twice writes nothing the second time. `cloud` also pins its queue in
`.github/branch-protection-contract.json`, which its guard compares with the live
ruleset; when this policy changes a value for cloud, change that file in the
same step.
