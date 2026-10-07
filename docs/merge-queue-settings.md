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

HEADGREEN: only the commit at the head of the merge group must pass the required
check, not every entry in it. The required check is the fast gate, `ci-ok`; the
full suite runs after the merge and a real failure there is reverted
([optimistic-merge.md](optimistic-merge.md)). Values: `policy/merge-queue.json`
`settings`. `max_entries_to_build` is 5 because the shared merge-queue runner
pool bounds build concurrency (the cloud merge-queue runbook records the
measurements); change a number in the policy file in a pull request, then apply.

A repository that needs another value lists an `overrides` entry with its ruleset
name and a reason; `fast_gate_overrides` does the same for a ruleset gated by
another check (cloud's `deploy/pins` branch by `pin-ok`). `exclude` lists
repositories never read or written; the hands-off repositories of
`policy/optimistic-merge.json` are always excluded.

## Required approving reviews: none

Every repository ruleset with a `pull_request` rule requires 0 approving
reviews (policy `review`). The fast gate `ci-ok` is the check; an approval
added after it checks nothing `ci-ok` did not, and a required approval only
made desk bots approve green pull requests to satisfy it. GitHub cannot scope
a review rule to file paths, so the count is 0 for every path. The enterprise
ruleset `agent-native-queued-trunk-base` already requires 0; repository
rulesets are converged here. A pull request can still be reviewed; it is no
longer held for an approval.

## What the tool manages

It manages the parameters of the `merge_queue` rule of every ruleset that has
one and the required approving review count of every repository ruleset with a
`pull_request` rule, and nothing else: on a write the rest of the ruleset (conditions, bypass
actors, the other rules) is sent back unchanged. It only reports whether the
fast gate is among the ruleset's required checks, and the strict flag. It never
adds a required check, because requiring a check that no workflow reports would
stop the queue; moving a repository's required check to `ci-ok` belongs with the
change that makes the repository report it.

## Commands

```sh
scripts/apply_merge_queue.py --dry-run                    # diff of every ruleset (the default)
scripts/apply_merge_queue.py --repo SylphxAI/desk-tools   # one repository
scripts/apply_merge_queue.py --check                      # exit 1 on any drift (for a schedule)
scripts/apply_merge_queue.py --apply --backup-dir DIR     # write; saves each ruleset in DIR first
scripts/apply_merge_queue.py --rollback DIR               # put the saved rulesets back
```

Reads are one GraphQL query per 100 repositories. Writes are one `PUT` per
drifted ruleset, a second apart, read back, and stop at the first 403. The
credential needs repository administration on each repository. Running
`--apply` twice writes nothing the second time. `cloud` also pins its queue in
`.github/branch-protection-contract.json`, which its guard compares with the live
ruleset; when this policy changes a value for cloud, change that file in the
same step.
