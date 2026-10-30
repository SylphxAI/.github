# Contribution metadata

Both `ci-ok` and `needs-pass` run the shared contribution check before returning
a pre-merge verdict. Repositories with another required verdict can invoke
`.github/actions/contribution-policy` directly from that job. Pin the shared
repository to a released full commit SHA followed by `# main`.

The single configuration is [policy/contribution-policy.json](../policy/contribution-policy.json).
Author and committer names and email addresses must match one configured pair
exactly. Bot exceptions are individual identities, never an email domain or a
name suffix. The approved pairs were read from actual Renovate, application,
and GitHub merge commits. This checks declared identity; it does not verify a
signature or establish who operated an account.

The check rejects attribution trailers, tool coauthors, and the configured
coordination identifiers in commit messages and PR titles and bodies. Ordinary
discussion of a product, model, generator, or command is allowed. Findings name
the rule and location without repeating the rejected text.

Commit metadata comes from immutable Git objects rather than the PR commits
API, whose results stop at 250 commits. Only commits introduced by the change
are checked. A merge group checks its generated commits and the original
commits and current metadata of every associated PR. A manual dispatch on an
open PR head runs the same policy. Post-merge push events do not recheck old
history. Missing permissions, missing PR metadata, unreadable Git objects, and
metadata changing during a read fail the check.

Callers must grant `contents: read` and `pull-requests: read` to the verdict job
and every reusable workflow through which it runs. Their PR event list must
include `edited`, in addition to `opened`, `synchronize`, `reopened`, and
`ready_for_review`, so title and body edits receive a new verdict. Keep
`merge_group` enabled; it rechecks current PR text immediately before merge.
Path filters must not omit the required verdict on a metadata-only event.
Installing an action in an optional job does not enforce the policy.

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
