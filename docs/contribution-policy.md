# Contribution metadata

Both `ci-ok` and `needs-pass` run the shared contribution check before returning
a pre-merge verdict. Repositories with another required verdict can invoke
`.github/actions/contribution-policy` directly from that job. Pin the shared
repository to a released full commit SHA followed by `# main`.

The single configuration is [policy/contribution-policy.json](../policy/contribution-policy.json).
Author and committer names and email addresses must match one configured pair
and its permitted role exactly. GitHub's merge identity is committer-only.
Bot exceptions are individual identities, never an email domain or a
name suffix. The approved pairs were read from actual Renovate, application,
and GitHub merge commits. This checks declared identity; it does not verify a
signature or establish who operated an account.

The check rejects attribution trailers, tool coauthors, and the configured
coordination identifiers in commit messages and PR titles and bodies. Ordinary
discussion of a product, model, generator, or command is allowed. Findings name
the rule and location without repeating the rejected text.

Commit metadata comes from immutable Git objects rather than the PR commits
API, whose results stop at 250 commits. Only commits introduced by the change
are checked. A merge group resolves synthetic commits through the paginated
merge queue, binding each to its original PR head. Earlier merged entries are
resolved through their introducing PR. Every range commit must be covered.
The check reads the original commits and current metadata of those PRs. A manual dispatch on an
open PR head runs the same policy. Post-merge push events do not recheck old
history. Missing permissions, missing PR metadata, unreadable Git objects, and
metadata changing during a read fail the check.

Callers grant `contents: read` and `pull-requests: read` to the verdict job
and every reusable workflow through which it runs. Install
[`pr-metadata.yml`](../workflow-templates/pr-metadata.yml) beside the code CI
workflow, pin its action to the adopted release, and require its `pr-metadata`
check separately after observing it on PR heads and merge groups. It handles
`opened`, `synchronize`, `reopened`, `edited`, and `ready_for_review`; its
separate concurrency group supersedes metadata checks only. Keep
`merge_group` enabled to recheck current PR text immediately before merge.
Path filters must not omit the metadata check. An optional job alone does not
enforce the policy. After requiring that producer, set
`separate-pr-metadata: 'true'` on every code verdict action, including nested
Verify jobs. The default remains `false` for existing callers. The opt-in
keeps immutable commit identities/messages and secret checks in code CI while
moving mutable PR text to its separate required producer. This lets a
title/body correction recover metadata without leaving successful code CI red.
The legacy cross-workflow `ci-ok` action also excludes `pr-metadata` from its
code verdict when opted in.

Code workflows accept only `opened`, `synchronize`, and `reopened` PR events.
Title/body edits and marking ready never start or cancel code CI, and metadata
workflows never publish `ci-ok`. A push runs the same affected tests for drafts
and ready PRs, so marking ready cannot reveal a suite that never ran. Retain
the code job dependencies and required-job settings: metadata success cannot
replace evidence for the exact head.

Keep code `workflow_dispatch` enabled. The metadata job dispatches it only for
`changes.base` retargeting, after checking the current PR head and branch ref;
title/body edits and readiness make no dispatch request. Retargeted fork PRs
fail metadata until a push or reopen supplies new code validation. The
metadata job needs `actions: write` for retargeting, plus its read permissions.
Retarget dispatches bind the PR number and base branch as well as the immutable
base/head/merge SHAs. The code verdict rechecks that target before accepting
evidence, and the added-commit secret gate scans the same base..head range.
A separate concurrency key for each base edit preserves its dispatch even when
a title/body edit arrives while it is pending or running.
Change its `pr-base-ci` workflow filename if the code file is not `ci.yml`.

Bot-created PRs dispatch both producers: list `pr-metadata.yml` before the code
workflow in the bot's `ci-workflows`, and add `pr-metadata` to its required
check list. Both metadata and code starters accept `workflow_dispatch`.

Before requiring a new release, run the checker over all merged PRs from the
last seven days across the complete active repository inventory. Keep the
fixed time window, pagination receipts, exact findings, and classification of
each refusal. Correct false positives before enforcement. Historical commit
findings are recorded separately and never exempt new commits. Delivered
customer repositories and other excluded repositories stay outside rollout.

Run the acceptance fixtures and acquisition tests with:

```sh
python3 -m unittest discover -s tests -p test_contribution_policy.py
```

An offline snapshot can use the same entry point as CI:

```sh
python3 .github/actions/contribution-policy/contribution_policy.py --input snapshot.json --report report.json
```

The snapshot has `pull_request` (`title`, `body`) and `commits` (REST commit
objects) fields. Exit status is 0 for clean metadata, 1 for findings, and 2 for
incomplete acquisition or invalid input. There is no caller-controlled waiver.
