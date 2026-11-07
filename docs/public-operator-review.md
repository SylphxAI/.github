# Public repository operator review

`SylphxAI/skills` and `SylphxAI/.github` require the latest
`ops-security/review` commit status to be `success` on the exact PR head before
merging to `main`. This policy applies to these two public repositories only.
Existing CI requirements, branch protections and queue settings stay intact.

## Publishers and merge groups

The authorized operator (`shtse8`) publishes the PR-head status. No workflow produces this context on
a PR head, and a new source push needs a new exact-head stamp. The requirement
has no integration binding because the operator status and GitHub Actions
merge-group check have different publishers. Do not bind it to Actions or add a
PR-head workflow that turns the external approval into automatic success.

`.github` uses a merge queue. Its `ops-security-merge-group.yml` workflow runs
only on `merge_group` and emits the named check on the combined commit. It uses
the existing contribution-policy membership resolver to inventory every PR in
the immutable candidate, including earlier queue entries that have already
merged. Every member's current head is bound to its synthetic position using
merge parents or squash-tree reconstruction, regardless of PR title. The GitHub commit API parent list must agree with the immutable synthetic
commit object: two-parent positions require the second parent to equal the PR
head; one-parent squash positions require reconstructed tree equality. Every
member's complete status history is read; the highest status ID for
`ops-security/review` must have state `success` and creator `shtse8`. Missing, pending, failure,
error, malformed or unreadable evidence fails closed. Membership and heads are
rechecked after the status reads. The token has read-only contents,
pull-requests and statuses permissions; the workflow never posts a PR status or
changes queue ownership.

There are two separate admission boundaries. First, GitHub's `main` ruleset
requires the PR-head status before the PR can enter the merge queue. Second,
this workflow projects the already-authorized member stamps onto the different
combined SHA. The workflow is not a substitute for the first boundary. A PR
that edits the workflow or imported membership resolver still needs the
operator's success on that exact edited head before enqueue. The verifier is
inline in the workflow and checks out only `repository: SylphxAI/.github` at
`ref: main`. Its imported membership and immutable Git helpers therefore come
from the trusted default branch, never the candidate working tree. Candidate
objects are read as data, not checked out or executed. This also permits the
initial adoption run before a new repository-relative script exists on main.

For initial adoption, the producer PR must receive that exact-head operator
stamp before it is armed, even though its new repository requirement is not
active yet. Observe the producer before activating the requirement, then use a
fresh unstamped no-op PR to prove GitHub's first boundary refuses enqueue. Do
not treat a successful merge-group projection alone as proof of head admission.

Skills has no repository merge queue at adoption. If a queue is introduced
later, install and prove an equivalent merge-group projection before enabling
it; the requirement must not be relaxed to accommodate a missing producer.

## Declaration and adoption

`.github` declares the unbound check in `policy/merge-queue.json` on its existing
repository ruleset `20321046` (`sylphx-merge-queue`). `integration_id: null` is an
explicit unbound publisher, not an omitted or inferred App ID. The policy tool
can update existing rulesets but cannot create a missing ruleset.

Skills therefore retains the exact one-time creation payload in
[`policy/public-skills-operator-review.json`](../policy/public-skills-operator-review.json).
It creates a narrow repository-owned rule on `refs/heads/main`, no bypass
actors, with the existing Actions-bound `ci-ok` plus the unbound operator stamp.
It does not add a merge queue or alter inherited rules.

Adopt in this order:

1. Land the producer and policy through the normal reviewed queue, then observe
   its successful merge-group check before adding its requirement.
2. Save a fresh full before object for `.github` rule `20321046`. Append only
   `{context: "ops-security/review", integration_id: null}` to its existing
   `required_status_checks`, through the repository ruleset update API. Preserve
   every other field. Do not run the full policy convergence during this narrow
   rollout: any pre-existing `review-ok`/`ci-ok` drift is outside its scope.
3. Read skills visibility/default branch and its complete repository-owned
   ruleset list, confirming public/main and no existing rule of this name.
   Create once using the saved payload and `POST /repos/SylphxAI/skills/rulesets`.
   Record the returned ID. On an uncertain response, reconcile by reading the
   list; never blindly repeat a create.
4. Read both complete repository-owned rulesets and effective `main` rules back.
   Use disposable same-tree PRs for refusal without a stamp and successful
   merge with the operator's exact-head success. Keep the unstamped probe
   unarmed after observing rejection, then close it. Verify `.github` also
   produces the projection on its real merge-group SHA.

Use one writer, fresh before/after reads and an exclusive local write marker.
Stop on an intervening edit or API error rather than replacing unreviewed
settings. Recovery uses the saved before object and a narrowly scoped supported
ruleset update; never remove CI, alter merge-queue parameters or add a bypass.

## Provider contract

GitHub's merge-queue documentation, read 2026-11-07 at 09:47 UTC, says PR and
merge-group checks are coupled under rulesets and that merge-group commits
contain the base plus every earlier queued PR. Required checks must run on
`merge_group`; a head-only external status would otherwise deadlock the queue.

- [Managing a merge queue](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue)
- [Repository rules REST API](https://docs.github.com/en/rest/repos/rules?apiVersion=2022-11-28)

## Verification

```sh
python3 -m unittest discover -s tests -p test_ops_security_merge_group.py
python3 -m unittest discover -s tests -p test_merge_group_review.py
python3 -m unittest discover -s tests -p test_apply_merge_queue.py
actionlint .github/workflows/ops-security-merge-group.yml
git diff --check
```

The policy payload is desired state until API readback and disposable PR
receipts confirm adoption; a merged source PR alone is not enforcement proof.
