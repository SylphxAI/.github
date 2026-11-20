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
`ci-ok`, the only merge gate besides explicitly scoped deterministic
`required_checks` entries. The full suite still runs
after the merge ([optimistic-merge.md](optimistic-merge.md)). Values:
`policy/merge-queue.json` `settings`.

The fleet default is Tier-2 (`SQUASH`, `HEADGREEN`, `max_entries_to_build: 5`,
`min_entries_to_merge: 2`, `max_entries_to_merge: 5`,
`min_entries_to_merge_wait_minutes: 1`, `check_response_timeout_minutes: 20`).
Change a number in the policy file in a pull request, then apply.

A repository that needs another value lists an `overrides` entry with its ruleset
name and a reason; `fast_gate_overrides` does the same for a ruleset gated by
another check (the platform's `deploy/pins` branch by `pin-ok`). `exclude` lists
repositories never read or written; the hands-off repositories of
`policy/optimistic-merge.json` are always excluded.

Tier-1 high-concurrency monorepos (`SylphxAI/sylphx`, `SylphxAI/infra`,
`SylphxAI/spiron`, `SylphxAI/keel`) override the default to batch 4 to 8 entries
with a 2-minute collection window (`max_entries_to_build: 8`,
`min_entries_to_merge: 4`, `max_entries_to_merge: 8`,
`min_entries_to_merge_wait_minutes: 2`) and keep a 60-minute transition deadline
until p90 gate latency stays at or below 5 minutes.

Slow native/game repositories carry explicit overrides:

- `Cubeage/hk-mahjong-tycoon-keel` keeps a 120-minute response deadline (measured
  82-minute native/browser merge-group gate on 2026-11-02) and its existing
  `ALLGREEN` build/merge limits of 10.
- `Cubeage/hk-mahjong-tycoon` preserves its live `MERGE` method,
  `max_entries_to_build: 2`, `max_entries_to_merge: 10`, and 240-minute deadline
  for Unity/Android/iOS and backend merge-group gates.
- `Cubeage/cubeage-native-sdk` keeps a 60-minute response deadline (measured
  Android/iOS merge-group runs of 20 to 34 minutes on 2026-11-18).
- `Cubeage/solar2d-singular-sdk` preserves its existing `ALLGREEN` grouping,
  build/merge limits of 10, `min_entries_to_merge: 1`, `wait: 0`, and 60-minute
  timeout.

## Required approving reviews: none

Repository `pull_request` rules require zero human approving reviews (policy
`review`), and no human or model review status is required: deterministic CI
is the merge gate. The enterprise ruleset keeps zero approving reviews.

The same `review` block turns `require_extra_approval_for_unattributed_changes`
off. That flag adds one approving review when a pull request carries commits
GitHub cannot attribute to a user, which is every desk App's commit, so it held
every bot pull request for an approval `ci-ok` already made (both production
gates found live on 2026-10-07). The tool reads it from each selected,
non-excluded ruleset's REST detail, because GraphQL's `PullRequestParameters`
does not carry it, and converges it to false.

The count reaches every repository ruleset of every organization in `orgs`,
which is the same four-organization fleet as `policy/optimistic-merge.json`:
`SylphxAI`, `Cubeage`, `EpiowAI` and `OzyrixLtd`. An approval held by any of
them blocked a merge the fast gate had already passed. A repository ruleset
that has a `pull_request` rule but no queue (a tags or non-default-branch
ruleset) is converged too, because the rule holds a review before every merge.
The hands-off repositories of `policy/optimistic-merge.json` are excluded and
keep their rule as it is: `SylphxAI/bgca` and `SylphxAI/openclaw-sylphx`.
HK Mahjong Tycoon is under active development and is not in that class. Before
planning, the tool also reads each selected,
non-excluded repository's custom properties. It excludes `sylphx_delivery=delivered`
and unreadable properties, so neither can produce a ruleset write. A property
read answering HTTP 403 stops the run before any write.

One run reaches all four: `orgs` is read as `[org for org in policy["orgs"] for
item in read_org(gh, org)]`, one GraphQL page per organization.

## What the tool manages

The tool manages merge-queue parameters, approving review counts, the
extra-approval flag and explicitly scoped required-check bindings. `required_checks` names the exact repository,
ruleset ID and name, a reason, and `{context, integration_id}` checks. Publisher
IDs must be positive integers; null and unbound publishers are refused. It adds
missing checks or corrects their publisher bindings without deleting unrelated
checks or altering conditions or bypass actors. Independently,
`queue_required_status_checks.strict_required_status_checks_policy: false`
converges the strict flag on repository rulesets carrying a `merge_queue` rule.
Non-queue rulesets preserve their existing strict flag (new non-queue check
rules default to strict=true). Excluded and inherited rulesets are never changed.
The managed readback includes contexts, App bindings and the strict flag.
GitHub documents that a queue provides the same up-to-date protection without
requiring authors to update PR branches ([merge queue documentation](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue),
read 2026-11-05).
Unscoped rulesets keep their required checks unchanged. A missing declared
target or a changed ruleset name refuses apply rather than silently missing a
repository. Dry-run reports required-check drift and App IDs; apply reads back
the resulting bindings. The saved before object is the recovery source; see the
queue-only rollback limitation below.

For adoption of one observed producer, use `--repo ORG/NAME --only-check CONTEXT`
with dry-run first, then `--apply --backup-dir DIR`. This mode requires a
policy-declared context and an existing required-check rule; it changes only
that binding and verifies the full resulting object. It preserves all queue,
review, strictness, bypass and condition fields, including unrelated check drift.
The unfiltered apply remains full policy convergence, not a narrow adoption
command. For example, `--repo SylphxAI/.github --only-check pr-metadata`
selects only the Actions metadata binding. That repository inherits `ci-ok` from
the organization.

A ruleset inherited from an organization or enterprise ruleset (GraphQL `source`
not `Repository`) is reported `INHERITED` and never written: REST has no path to
write it, and the enterprise ruleset already requires 0 approvals. A new
repository therefore needs no manual step for the review count; the next run
reads whatever repository ruleset it carries, queue or not.

## First required check on an existing queue

A queue with no required checks may merge without creating an observable
merge-group run. Do not manufacture a test change or wait for a merge-group
proof that its current rules cannot produce. For that bootstrap only:

1. Merge the real producer first, with unfiltered `pull_request`, `merge_group`
   and default-branch push events. Read its original PR and merged-main checks,
   exact commits and publisher identity.
2. Declare only that repository, existing ruleset ID/name and observed context
   here. Preserve its current queue settings with a reasoned override; do not
   activate another repository or an unobserved review producer.
3. Apply with `--repo ORG/NAME`, retain the before object, and read back the
   complete ruleset and effective default-branch rules.
4. Correct the repository's now-stale delivery manifest in a genuine PR. Read
   that original PR, merge-group and merged-main result. A missing or failed
   merge-group producer remains non-success; repair its cause, never bypass
   the gate or rerun a failed queue entry.

The first scoped target is `Cubeage/solar2d-singular-sdk`, ruleset `20321316`.
Its Android producer passed original PR run `41449602348` and main run
`41453570358` at merged commit `9bab4031edecc1266e0a55ed949d1bbd2ccdd4de`.
The 2026-11-02 13:43 UTC read found an active queue and no required checks.
Its existing ALLGREEN grouping, build/merge limits of 10, and 60-minute timeout
are preserved. This entry neither enables Release nor claims a merge-group
receipt before the post-adoption change has actually run.

GitHub's merge-queue documentation and repository-rules REST schema were read
on 2026-11-02 at 13:43 UTC: the Actions producer must include `merge_group`,
and required checks can bind an `integration_id`. Sources:
<https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue>
and <https://docs.github.com/en/rest/repos/rules?apiVersion=2022-11-28>.

`--rollback` currently restores queue parameters only; it does **not** undo a
required-check addition or publisher binding. For those fields, retain the
saved before object and restore the affected rule through GitHub's supported
ruleset update, preserving unrelated intervening changes and reading the
result back. Do not call a queue-only rollback a required-check recovery.

## Required-check recovery

Save a fresh backup directory per apply. `--rollback` alone restores queue
parameters, not required checks; restore an affected required-check rule from the
saved before object through the supported ruleset update and read back the
bindings. A successful PUT alone is not an apply or rollback receipt. The
retired work repository is archived and is an `exclude` entry.


## Commands

```sh
scripts/apply_merge_queue.py --dry-run                    # diff of every ruleset (the default)
scripts/apply_merge_queue.py --repo SylphxAI/.github      # one repository
scripts/apply_merge_queue.py --check                      # exit 1 on any drift (for a schedule)
scripts/apply_merge_queue.py --audit-bypasses --enterprise sylphxai --org SylphxAI  # fail closed on redacted or non-empty bypass_actors
scripts/apply_merge_queue.py --apply --backup-dir DIR     # write; saves each ruleset in DIR first
scripts/apply_merge_queue.py --rollback DIR               # put the saved merge_queue parameters back
```

Empty `required_status_checks: []` rules inside a ruleset carrying another rule
(such as `merge_queue`) enforce no checks and are pruned on apply.
`--audit-bypasses` inspects REST ruleset details and exits non-zero whenever
`bypass_actors` is non-empty or omitted/redacted by a token lacking editor
visibility.

Reads are one GraphQL query per 100 repositories plus one custom-property read
per selected, non-excluded repository, and one ruleset-detail read per selected,
non-excluded repository ruleset that carries a `pull_request` rule (its
extra-approval flag is not in the GraphQL schema). Writes are one `PUT` per
drifted ruleset, a second apart, read back, and stop at the first 403. The
credential needs repository administration on each repository. Running
`--apply` twice writes nothing the second time.
`--apply` refuses a backup directory that already holds a backup of a ruleset it
would change, so the first backup stays the true "before"; use a new directory
for each run. `--rollback` reads each ruleset as it is now and puts back only the
saved `merge_queue` parameters, so any other edit made since the apply stays.
The organizations are SylphxAI, Cubeage, EpiowAI and OzyrixLtd (read with the
same login as the optimistic-merge audit); `Cubeage/cubeage-platform` and the
hands-off repositories are excluded. `SylphxAI/sylphx`'s
`.github/branch-protection-contract.json` delegates queue capacity and grouping
parameters here and checks only required `ci-ok` and merge-queue enforcement.
