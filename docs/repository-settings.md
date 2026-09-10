# Repository settings policy

Some repository behavior lives in GitHub settings rather than in source. This
page owns the policy for those settings and the tool that converges them.

## Auto-delete merged PR head branches

**Policy: every eligible SylphxAI repository keeps `delete_branch_on_merge=true`
("Automatically delete head branches").**

A merged pull request is terminal. The PR record — diff, commits, review
discussion, and the merge commit — is the recovery home for its head branch, so
the head ref holds no state that deletion loses. Keeping merged heads instead
accumulates stale refs: branch lists, `git branch -r`, and agent branch
selection all drift toward branches nobody owns.

Restoring a deleted head branch is an explicit act: recreate the ref from the
PR's head SHA or from the merge commit.

## Running the sweep

`scripts/set-delete-branch-on-merge.sh` is dry-run by default; it writes only
with `--apply`.

```bash
scripts/set-delete-branch-on-merge.sh                     # dry-run, owner SylphxAI
scripts/set-delete-branch-on-merge.sh --apply             # converge SylphxAI
scripts/set-delete-branch-on-merge.sh --apply SylphxAI shtse8
```

The script enumerates each owner's repositories (skipping archived, forked, and
disabled ones), reads `delete_branch_on_merge` and `permissions.admin` per
repository, and prints one line per repository:

| Report | Meaning |
| --- | --- |
| `OK` | already `true` |
| `WOULD-PATCH` | dry-run: currently not `true` |
| `PATCHED` | written and re-read as `true` |
| `SKIP` | the credential has no admin permission |
| `FAILED` | API error, or the write did not verify |

It continues past failures and exits non-zero when any repository `FAILED`.
Run the dry-run first, then `--apply`, then confirm the summary reports
`would_patch: 0`/no `FAILED` lines.

## New repositories

Create new repositories with the setting already on:

- `POST /orgs/{org}/repos` accepts `delete_branch_on_merge: true`
  ([Create an organization repository](https://docs.github.com/en/rest/repos/repos#create-an-organization-repository));
  an org owner credential is required.
- Any repository created by another path is swept afterwards with
  `scripts/set-delete-branch-on-merge.sh --apply <owner>`.

## Scheduled automation (not enabled)

A schedule would need an admin credential stored as an Actions secret in this
repository. No such secret and no sweep workflow are committed here; the sweep
stays a deliberate manual run until that credential exists.

## Exceptions

- Archived repositories, forks, and disabled repositories are out of scope.
- A repository whose rulesets intentionally protect long-lived branches keeps
  its own exceptions; record them in that repository, not here.
- Repositories the running credential cannot administer are reported `SKIP` and
  stay out of scope until an admin-able credential sweeps them.
