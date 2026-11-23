# Repository baseline

Eligible repositories allow auto-merge and delete PR head branches after
merge. The merged PR retains the diff, discussion, and commit IDs; recreate a
head ref from its commit SHA if needed. Existing default-branch protection must
not require approving reviews, including code-owner, last-push, and
unattributed-change approvals. The reconciler never creates protection or
changes required checks, force-push guards, PR requirements, or thread resolution.

## Run the reconciler

```bash
python3 scripts/reconcile_repo_baseline.py --dry-run
python3 scripts/reconcile_repo_baseline.py --org OWNER --repo OWNER/NAME --apply
```

Use repeatable `--org` or a space-separated `REPO_BASELINE_ORGS` environment value.
The default is a dry-run against the approved owners in the merge-queue
policy. Apply always requires explicit owner scope. JSON reports each organization and repository, its
status, findings, and changed fields. Exit 1 indicates blocked drift or an
unreadable state; actionable dry-run drift is not an error. Keep detailed JSON
private. The credential must be authorized for the full approved repository
inventory; a GitHub token limited to a subset only enumerates visible repos.

Archives, forks, disabled repositories, delivered projects, and existing
hands-off and advisory-fork policy entries are skipped. An unreadable delivery
value or unknown repository admin authorization is an error and never a write.
Classic branch protection receives only a review-fields PATCH. An active
repository ruleset receives a PUT only if its branch selector is proven to
match just the current default branch. The PUT preserves all other writable
state, including bypass actors, conditions and rule parameters. The tool
re-reads before writing, uses the ETag where returned, and verifies readback.
Do not run it concurrently with another ruleset writer; GitHub does not
document an atomic compare-and-swap for ruleset updates. Inherited rules are
inspected at their applied branch and reported, never edited. Drift in an
inherited or mixed-scope rule remains for its owning authority to resolve.

## Scheduled workflow

Repository baseline runs a weekly dry-run on the self-hosted control runner.
An explicit `apply=true` dispatch from the default branch can write. Configure
its protected `repository-baseline` Actions environment to admit only the
default branch. Set these three environment secrets:

- `REPO_BASELINE_ORGS`: the space-separated owner list from the merge-queue policy.
- `REPO_BASELINE_APP_ID`: the dedicated GitHub App's numeric ID.
- `REPO_BASELINE_APP_PRIVATE_KEY`: the App's PEM private key.

Install the App on **all repositories** in each policy organization, with
repository permissions **Administration: write**, **Metadata: read**, and
**Contents: read** (for private-branch lookups). Do not grant Contents write.
Both the workflow and local CLI use these App credentials; no PAT, workflow
token or runner CLI login is used. Python 3 and OpenSSL are required. The
reconciler checks every installation and exchanges its App JWT for one
installation token per owner before starting reconciliation. A missing or
unreadable installation, a selected-repository grant, or a failed token
exchange aborts the run before any repository settings are changed. Each
token is used only for its own installation inventory and repositories.
Public job output contains aggregate counts and sanitized errors only.
The reconciler refuses writes if the installation cannot read the current
merge settings or default-branch state. Verify a dry-run before dispatching
with `apply=true`.

The [merge-queue tool](merge-queue-settings.md) still owns queue settings,
explicit check scope, and its existing off-default review policy. Serialize
its apply and rollback runs with repository baseline apply.
