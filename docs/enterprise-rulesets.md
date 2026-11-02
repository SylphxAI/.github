# Enterprise repository rules

Shared branch policy belongs to the `sylphxai` Enterprise account, not a copy
on each repository. Repository merge queues remain separate: GitHub's Enterprise
ruleset API does not accept a `merge_queue` rule. See
[merge queue settings](merge-queue-settings.md).

## Shared CI gate

[`policy/enterprise-ci.json`](../policy/enterprise-ci.json) is the desired
Enterprise ruleset. It requires the existing GitHub Actions `ci-ok` check
(integration `15368`) on default branches of `SylphxAI` repositories with
`sylphx_ci_gate=required`. It excludes `sylphx_delivery=delivered`, has no bypass
actors, and preserves the previous gate's non-strict and branch-creation
settings. It does not add a check to other organizations, feature branches,
exempt repositories, or delivered customer projects. BGCA remains exempt and
delivered.

[`policy/enterprise-properties.json`](../policy/enterprise-properties.json)
records the two property definitions. Property definitions must be at Enterprise
scope to use them in an Enterprise ruleset: GitHub rejected organization-only
properties with HTTP 422 on 2026-11-02. Promote the existing definitions rather
than deleting and recreating them; promotion keeps existing repository values.
The `required` default on `sylphx_ci_gate` is metadata, not an expansion of the
rule to other organizations. Changing that rule's organization condition is a
separate policy change.

## Apply or migrate

The JSON is desired state, not evidence that a live rule has been applied.
Merge source changes normally before activating them. Use the supported REST
API with the connected account; no repository ruleset, queue, seat or plan
change is required.

1. Read the Enterprise and organization rulesets immediately before writing.
   Save complete rule objects, property definitions and values, and the complete
   repository census outside this public repository. Save repository rulesets
   and effective default-branch rules for `infra` and `.github` as consumer
   controls. Refuse a second writer or an unexplained change to these objects.
2. For the initial migration only, check property-name collisions in **every**
   Enterprise organization. Promote `sylphx_ci_gate` and `sylphx_delivery` with
   `PUT /enterprises/sylphxai/properties/schema/organizations/SylphxAI/{name}/promote`.
   Read back Enterprise definitions and the complete organization property-value
   population; existing values and exemptions must remain identical. Later runs
   use the existing Enterprise definitions and must not repeat promotion.
3. Create the CI rule with `enforcement=evaluate`, keeping the old organization
   rule active. Use `POST /enterprises/sylphxai/rulesets` only after listing
   rulesets and confirming the name is absent. For an existing rule, read its
   complete object and use `PUT /enterprises/sylphxai/rulesets/{id}` instead.
   Read the resulting object back independently. An unacknowledged write is
   reconciled by reading the live list/object, never blindly resent.
4. Verify targeting across the complete repository population using
   `GET /repos/{owner}/{repo}/rulesets?includes_parents=true`. The expected set
   is `SylphxAI` repositories with CI property `required`, excluding `delivered`.
   Check that the candidate is absent from every other Enterprise organization,
   CI-exempt repository and BGCA. Evaluate rules do not appear in the effective
   branch-rule endpoint, so ruleset inheritance is the pre-activation check.
5. Read the still-active old rule and candidate again. After coverage matches,
   update the candidate with the committed payload's `enforcement=active` and
   read back its object and the effective default-branch rules. In particular,
   `infra` and `.github` must inherit the Enterprise `ci-ok` rule while their
   complete repository-level queue objects remain unchanged.
6. Only after that proof, delete the unchanged organization rule
   `org-required-ci-gate` (`25143064`). Read the organization and inherited rule
   lists again: exactly one active shared CI gate, sourced from Enterprise,
   with no organization or repository copy. Recheck the full target population,
   both consumer queues, and the negative cases. Retain timestamped receipts
   outside this public repository.

Serialize writes with a local exclusive lock and record each request before
sending it. On a failed coverage or consumer check, keep the old gate active
and leave the candidate in evaluate mode. After activation, recover by restoring
its saved payload (or disabling the new rule while the old one still exists).
After deletion, restore the saved organization payload before disabling the
Enterprise gate. Do not remove both gates or overwrite intervening changes.

## Existing branch integrity

The Enterprise already supplies `agent-native-branch-integrity` on non-default
branches (`non_fast_forward`), `agent-native-trunk-integrity` on default branches
(`deletion`, `non_fast_forward`), and `agent-native-queued-trunk-base` for pull
requests. The CI migration does not rewrite their conditions, bypass actors,
or rule parameters. A repository should inherit these rules, not duplicate them.

## Primary sources

Read on 2026-11-02 at 16:37–16:43 UTC:

- [Enterprise rules REST API](https://docs.github.com/en/enterprise-cloud@latest/rest/enterprise-admin/rules?apiVersion=2022-11-28)
- [Enterprise custom properties REST API](https://docs.github.com/en/enterprise-cloud@latest/rest/enterprise-admin/custom-properties?apiVersion=2022-11-28)
- [Promoting repository properties](https://docs.github.com/en/enterprise-cloud@latest/admin/managing-accounts-and-repositories/managing-repositories-in-your-enterprise/managing-custom-properties-for-repositories-in-your-enterprise#promoting-organization-properties-to-enterprise-properties)
- [REST OpenAPI, `ghec.2022-11-28`](https://github.com/github/rest-api-description/blob/main/descriptions/ghec/ghec.2022-11-28.json):
  `enterprise-ruleset-conditions` supports organization-name plus repository-property
  conditions; `enterprise-rules` supports required checks but not merge queues.
