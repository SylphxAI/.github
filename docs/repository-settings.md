# Repository baseline

Eligible repositories allow auto-merge and delete PR head branches after
merge. The merged PR retains the diff, discussion, and commit IDs; recreate a
head ref from its commit SHA if needed. Existing default-branch protection must
not require approving reviews, including code-owner, last-push, and
unattributed-change approvals. The reconciler never creates protection or
changes required checks, force-push guards, PR requirements, or thread resolution.

## Run the reconciler

	scripts/reconcile_repo_baseline.py --org OWNER
	scripts/reconcile_repo_baseline.py --org OWNER --repo OWNER/NAME --apply

Use repeatable `--org` or a space-separated `REPO_BASELINE_ORGS` environment value.
The default is dry-run. JSON reports each organization and repository, its
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
default branch. It expects two environment secrets: `REPO_BASELINE_ORGS` with
all four approved owners on one line and `REPO_BASELINE_ADMIN_TOKEN` with a
dedicated credential allowed to enumerate those owners and read/write the
relevant repository settings. The existing broker action issues single-repo
tokens; this fleet job does not silently use the workflow token, runner CLI
identity, or another secret. Public job output contains aggregate counts only.

The [merge-queue tool](merge-queue-settings.md) still owns queue settings,
explicit check scope, and its existing off-default review policy. Serialize
its apply and rollback runs with repository baseline apply.
