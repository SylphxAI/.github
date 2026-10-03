# Actions approval guard

**Policy: no Cubeage or SylphxAI ruleset or branch protection may count a
GitHub Actions approval, or let the Actions identity bypass it.**

The organization setting "Allow GitHub Actions to create and approve pull
requests" lets a workflow's `GITHUB_TOKEN` approve a pull request, and GitHub
counts that approval toward a required-review rule. We merge through the merge
queue with zero required reviews, so enabling the setting is safe only while no
rule relies on reviews. The weekly check keeps it so.

`.github/workflows/actions-approval-guard.yml` runs Mondays (and on dispatch)
on `sylphx-linux-standard`, reading both organizations with
[scripts/audit_actions_approval.py](../scripts/audit_actions_approval.py)
(read-only: only GETs). Tests: `python -m unittest tests.test_audit_actions_approval`.

| Result | Condition (active rules only) |
| --- | --- |
| FAIL | a `pull_request` rule needs more than 0 approving reviews while Actions can approve (organization and repository setting both on) |
| FAIL | a bypass actor is the GitHub Actions integration (id 15368) or the `write` repository role, which the Actions token holds |
| FAIL | classic branch protection on the default branch lets `github-actions` bypass pull request reviews |
| FAIL | anything the audit could not read, including a missing token (a guard that cannot see is not a pass) |
| WARN | a rule needs approving reviews while Actions cannot approve today; it turns into a FAIL the day the setting is on |

The run prints a summary table (also in the job summary). A failing run opens
one issue labelled `owner:ops` in this repository, comments on it while the
failure persists, and closes it on the first clean run.

Enterprise rulesets are visible only as effective rules on a default branch, so
their review counts are checked but their bypass actors are not: the report
carries one standing WARN per organization that has one. Read those in
enterprise settings. `SylphxAI/bgca` (a handed-over client project) is skipped.

## One-time setup (an organization owner)

The existing builder App has write permissions and is not installed on Cubeage,
so the audit uses its own read-only App:

1. Register an App under SylphxAI with repository permissions Administration:
   read and Metadata: read, and organization permission Administration: read.
   No webhook, no write permission.
2. Install it on SylphxAI and on Cubeage (an owner of each approves), all
   repositories.
3. In this repository set the variable `ORG_AUDIT_APP_ID` and the secret
   `ORG_AUDIT_APP_PRIVATE_KEY`.

Until then the run fails with "no audit token" or an unreadable-organization
finding, which is the intended state of an audit that cannot see.
