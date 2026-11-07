# Public repository operator review

`SylphxAI/skills` and `SylphxAI/.github` require an `ops-security/review`
Check Run from the canonical `sylphx-ai` App (`1299545`) with conclusion
`success` on the exact PR head before merging to `main`. This policy applies
only to these two public repositories. Existing CI, protections and queue
settings stay intact. Commit statuses from other publishers and GitHub Actions
checks (`15368`) cannot satisfy this App-bound requirement; the native rule binds
the App, not the result type. The operator interface and queue projection use
Check Runs only.

## Operator approvals

The authorized operator publishes the decision using a short-lived,
repository-scoped App token obtained through the existing Broker grant. No
workflow automatically approves a PR head. A source push requires a new
exact-head approval. The manual interface refuses execution in Actions:

```sh
# Run from trusted source as the operator, never from a candidate checkout.
# OPS_SECURITY_APP_TOKEN: Broker checks:write-only grant; GH_TOKEN: operator's
# separate read credential. Neither token is a CLI argument or printed.
python3 scripts/operator_review_check.py \
  --repo SylphxAI/skills --pr <number> --head <reviewed-40-character-sha> \
  --conclusion success --receipt-dir <new-private-receipt-directory>
```

The publisher reads public repository identity and the current open PR's
main/head binding before writing. It creates a completed success or failure
Check Run, then reads its ID, App, head, name and conclusion back. An exclusive
pre-submit marker prevents repeating a POST after a lost response; retain the
receipt directory and reconcile using GETs instead of resubmitting. App tokens
stay in process memory; receipt files contain no credentials. The checks-only
App credential is not used for PR metadata reads.

## Trusted merge-group projection

`.github` has a merge queue. Its existing **PR review** wrapper
(`pr-review.yml`) starts on `workflow_run: requested` from **Project control**.
GitHub loads this wrapper from the default branch, not the triggering
`merge_group` candidate. The operator-projection job requires public
`SylphxAI/.github` and `refs/heads/main`, checks out `github.workflow_sha`, and
executes only that trusted revision. It never executes candidate code or
artifacts. A candidate may rename/remove its own job or emit a green Actions
lookalike, but cannot satisfy the required App `1299545` publisher binding.

The producer authenticates through the existing main-wrapper Access declaration
(`pr-review-shared`) and repository Broker policy. It requests only
`checks:write` for this repository; no App key, repository secret, candidate
OIDC exchange or candidate write token is introduced. The auth command performs
no inference. Ephemeral credentials are removed even on failure. Source reads
use the separate read-only Actions token with actions, contents, pull requests
and checks permissions.

The verifier obtains the native source run ID from the event, rereads that run,
and requires its repository ID, `merge_group` event, Project control workflow ID
and main queue branch. GitHub GraphQL queue-entry `headCommit` and `baseCommit`
define the live group. Candidate outputs, artifacts and user-supplied head/base
SHAs have no authority. Manual recovery accepts only a native run ID on the
same protected wrapper; a retired or replaced entry fails closed.

The existing contribution-policy resolver inventories every PR in the immutable
group range, including earlier entries already merged. Every member head is
bound to its immutable position through merge parents or squash-tree
reconstruction, regardless of title; the REST parent list must agree with the
raw commit object. Every member's complete Check Run history is read. Only the
latest `ops-security/review` run from App `1299545` counts, and it must be
completed, exact-head `success`. Missing, pending, neutral, skipped, failed,
malformed or unreadable evidence refuses. Other Apps and commit statuses cannot
approve or override a canonical failure. Membership, heads and live native
group identity are rechecked before publishing the App result on the combined
SHA, never on a PR head. Each projection has an independent concurrency group.

Skills has no repository merge queue at adoption; GitHub enforces its App-bound
PR-head requirement directly. If a queue is introduced later, install and prove
a trusted group projection first; do not relax the requirement for a missing
producer.

## Declaration and adoption

`.github` declares App `1299545` in `policy/merge-queue.json` on its existing
repository ruleset `20321046` (`sylphx-merge-queue`). The policy loader rejects
null, boolean, zero and negative publisher IDs. Skills retains its one-time
creation payload in
[`policy/public-skills-operator-review.json`](../policy/public-skills-operator-review.json):
a repository-owned main rule, no bypass actors, existing Actions-bound `ci-ok`
and the canonical App-bound operator requirement, without adding a queue.

Adopt in this order:

1. Independently review the producer's exact head, issue its operator App check,
   and land it through the normal queue. The requirement is not active during
   this bootstrap. On a subsequent disposable group, observe the trusted main
   producer's successful App Check Run before activating the requirement.
2. Save a fresh full before object for `.github` ruleset `20321046`. Append only
   `{context: "ops-security/review", integration_id: 1299545}` to its existing
   `required_status_checks` through the repository ruleset API, preserving every
   other field. Use the supported scoped command after a dry-run:
   `python3 scripts/apply_merge_queue.py --repo SylphxAI/.github --only-check ops-security/review --apply --backup-dir <new-directory>`.
   Omit `--apply --backup-dir ...` for its dry-run. Do not run full policy
   convergence: unrelated `review-ok`/`ci-ok` drift is outside this rollout.
3. Read skills visibility/default branch and its complete repository-owned
   ruleset population. Confirm public/main and no existing rule of this name;
   create once with `POST /repos/SylphxAI/skills/rulesets`, recording its ID.
   On an uncertain response, reconcile the list instead of repeating creation.
4. Read both complete repository-owned rulesets and effective main rules back.
   Use disposable documentation-only PRs with no production behavior change to
   prove refusal without approval and with a same-name status/Actions lookalike,
   then successful normal merge after the
   canonical exact-head App success. Verify `.github` also receives the trusted
   projection on its actual combined SHA. Close negative probes unarmed.
   A zero-file, same-tree PR is not a valid fixture for the current required
   review producer: it refuses an empty changed-file population before review.

Use one writer, fresh before/after reads and an exclusive write marker. Stop on
intervening settings changes or API errors. Recovery uses the saved before
object and a narrow supported update, never removal of CI or addition of a
bypass. Source declarations remain desired state until these live receipts.

## Provider contracts and verification

GitHub's event documentation, read 2026-11-07 at 10:50 UTC, identifies
`workflow_run`'s ref/SHA as the default branch and permits a privileged follow-up
after an unprivileged source workflow. Queue-entry `baseCommit`/`headCommit`
fields were verified against GitHub's live GraphQL schema the same session.
The source merge group's conclusion is not itself an operator approval.

- [Workflow events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_run)
- [MergeQueueEntry](https://docs.github.com/en/graphql/reference/objects#mergequeueentry)
- [Expected status-check source](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches#require-status-checks-before-merging)
- [Repository rules REST API](https://docs.github.com/en/rest/repos/rules?apiVersion=2022-11-28)

```sh
python3 -m unittest discover -s tests -p test_ops_security_merge_group.py
python3 -m unittest discover -s tests -p test_operator_review_check.py
python3 -m unittest discover -s tests -p test_merge_group_review.py
python3 -m unittest discover -s tests -p test_apply_merge_queue.py
python3 -m unittest discover -s tests -p test_contribution_policy.py
actionlint .github/workflows/pr-review.yml
git diff --check
```
