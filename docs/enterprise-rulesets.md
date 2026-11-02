# Enterprise repository rules

Shared branch integrity belongs to the `sylphxai` Enterprise account. The shared
CI gate currently remains at organization scope: the Enterprise API refused its
replacement. Repository merge queues remain separate because GitHub's Enterprise
ruleset API does not accept a `merge_queue` rule. See
[merge queue settings](merge-queue-settings.md).

## Current CI gate and migration result

On 2026-11-02, the existing `org-required-ci-gate` (`25143064`) remained active,
requiring the GitHub Actions `ci-ok` check (integration `15368`) on default branches
of `SylphxAI` repositories with `sylphx_ci_gate=required`. Its non-strict and
branch-creation settings, bypass actors and repository queues were not changed.
The Enterprise replacement was **not created or activated**, and the organization
gate was **not deleted**.

GitHub's current OpenAPI and prose document Enterprise rulesets, required status
checks and evaluate mode. Nevertheless, these requests to
`POST /enterprises/sylphxai/rulesets` returned HTTP 422 with
`Invalid rule 'required_status_checks': ` and no further detail:

| UTC on 2026-11-02 | Candidate variation |
| --- | --- |
| 17:14:32 | Existing check parameters and publisher binding, evaluate mode |
| 17:16:34 | Same candidate without the optional publisher binding |
| 17:17:01 | Original check parameters in disabled mode |
| 17:17:22 | Evaluate mode without publisher binding or the branch-creation option |

Each subsequent live list contained only the three existing Enterprise integrity
rulesets, with no failed-create residue. These results establish a refusal of
this configuration, **not** that Enterprise status checks are universally
unsupported. The backend cause remains unresolved. Do not weaken the working CI
gate or repeat speculative creates to force a migration.

[`policy/enterprise-ci-candidate.json`](../policy/enterprise-ci-candidate.json)
is the **unapplied candidate**, saved in evaluate mode. It preserves the existing
check and publisher binding, adds an explicit exclusion for
`sylphx_delivery=delivered`, and limits coverage to `SylphxAI`. It is not the live
CI authority and must not be treated as an applied policy. It does not expand CI
to other organizations, feature branches, exempt repositories or delivered
customer projects. BGCA remains CI-exempt and delivered.

## Repository properties

[`policy/enterprise-properties.json`](../policy/enterprise-properties.json)
records the two existing property definitions, promoted to Enterprise scope at
17:14 UTC on 2026-11-02. A complete read-back of all 248 SylphxAI repositories
confirmed that every property value was preserved, including all 16 CI
exemptions and the delivered-customer value.

Property definitions must be at Enterprise scope to use them in an Enterprise
ruleset: the initial organization-only references also returned HTTP 422.
Promotion resolved that validation error, but not the later check-rule refusal.
Promotion used the supported endpoint, not deletion and recreation. The
`required` default on `sylphx_ci_gate` is metadata; no new CI rule was applied in
any organization. A complete read of the other four organizations found that all
409 of their repositories now expose `sylphx_ci_gate=required` and
`sylphx_delivery=null`. They do not inherit the SylphxAI organization CI gate.

The current REST API and promotion documentation provide no in-place demotion
operation. Treat promotion as non-reversible through the documented API, not as a
reversible ruleset toggle. Deleting and recreating definitions is not a supported
in-place reversal and can discard repository values or break the still-active
property-scoped gate. No such deletion was attempted. Retain the definitions and
saved values; any future scope reversal needs a separately verified recovery
procedure or a provider-supported demotion.

## Reconsidering the migration

Only reconsider activation after new provider evidence resolves the observed
refusal. The saved JSON is not evidence that GitHub accepts it.

1. Read the Enterprise and organization rulesets immediately before writing.
   Save complete rule objects, property definitions and values, and the complete
   repository census outside this public repository. Save repository rulesets
   and effective default-branch rules for `infra` and `.github` as consumer
   controls. Refuse a second writer or an unexplained change to these objects.
2. Reuse the promoted property definitions. For a different Enterprise's initial
   migration, first check name collisions in every member organization and use
   `PUT /enterprises/{enterprise}/properties/schema/organizations/{org}/{name}/promote`
   to preserve values. Independently read back definitions and the complete
   property-value population.
3. With evidence that the previous refusal is resolved, create the candidate in
   evaluate mode while keeping the old organization rule active. List rulesets
   first and confirm its name is absent. Read a successful result independently;
   reconcile an unacknowledged write by reading the live list, never by blindly
   resending it.
4. Verify targeting across the complete repository population using inherited
   ruleset lists (`GET /repos/{owner}/{repo}/rulesets?includes_parents=true`, or
   GraphQL `rulesets(includeParents:true)`). The expected set is SylphxAI
   repositories with CI property `required`, excluding `delivered`. The candidate
   must be absent from every other organization, CI-exempt repository and BGCA.
   Evaluate rules do not appear in the effective branch-rule endpoint.
5. Only after successful evaluation and exact coverage proof, merge an explicit
   source change to active enforcement normally. Read the old rule and candidate
   again, apply that committed payload and read back the effective rules. Both
   `infra` and `.github` must inherit the Enterprise check while their complete
   repository queue objects remain unchanged.
6. Only after that proof, delete the unchanged organization rule `25143064`.
   Recheck the complete population and both consumers: exactly one active shared
   CI gate, sourced from Enterprise, with no organization or repository copy.

Serialize writes with a local exclusive lock and record each request before
sending it. On a failed coverage or consumer check, retain the working old gate.
Never remove both gates or overwrite intervening changes. After deletion, restore
the saved organization payload before disabling a defective Enterprise gate.

## Existing branch integrity

The Enterprise already supplies `agent-native-branch-integrity` on non-default
branches (`non_fast_forward`), `agent-native-trunk-integrity` on default branches
(`deletion`, `non_fast_forward`), and `agent-native-queued-trunk-base` for pull
requests. Their conditions, bypass actors and rule parameters were not rewritten.
A repository should inherit these rules, not duplicate them.

## Primary sources

Read on 2026-11-02 at 16:37–16:43 UTC, with the prose compatibility checks reread
at 17:18–17:20 UTC:

- [Enterprise rules REST API](https://docs.github.com/en/enterprise-cloud@latest/rest/enterprise-admin/rules?apiVersion=2022-11-28)
- [Enterprise custom properties REST API](https://docs.github.com/en/enterprise-cloud@latest/rest/enterprise-admin/custom-properties?apiVersion=2022-11-28)
- [Promoting repository properties](https://docs.github.com/en/enterprise-cloud@latest/admin/managing-accounts-and-repositories/managing-repositories-in-your-enterprise/managing-custom-properties-for-repositories-in-your-enterprise#promoting-organization-properties-to-enterprise-properties)
- [Enterprise code-governance policies](https://docs.github.com/en/enterprise-cloud@latest/admin/enforcing-policies/enforcing-policies-for-your-enterprise/enforcing-policies-for-code-governance#using-ruleset-enforcement-statuses)
- [Available rules](https://docs.github.com/en/enterprise-cloud@latest/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets#require-status-checks-to-pass-before-merging)
- [Troubleshooting required status checks](https://docs.github.com/en/enterprise-cloud@latest/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/troubleshooting-rules#troubleshooting-required-status-checks)
- [REST OpenAPI, `ghec.2022-11-28`, version `1.1.4`](https://github.com/github/rest-api-description/blob/main/descriptions/ghec/ghec.2022-11-28.json):
  `enterprise-ruleset-conditions` accepts organization-name plus repository-property
  conditions; `enterprise-rules` includes required checks but not merge queues.
