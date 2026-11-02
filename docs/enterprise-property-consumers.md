# Enterprise property consumers

Read on 2026-11-02 at 17:27–17:33 UTC after promotion of `sylphx_ci_gate` and
`sylphx_delivery` to Enterprise scope. See [the migration result](enterprise-rulesets.md).
The property metadata now spans all 657 repositories in five organizations,
including 409 outside SylphxAI. `sylphx_ci_gate` defaults to `required`;
`sylphx_delivery` defaults to null. Property presence does not authorize a rollout.

## Readers and scope

| Reader | What it reads and enumerates | Effect of promotion |
| --- | --- | --- |
| GitHub organization rule `org-required-ci-gate` (`25143064`) | Default branches in **SylphxAI** with `sylphx_ci_gate=required`. | Same 232 targets. Organization scope prevents expansion to the other four organizations. |
| [`scripts/apply_merge_queue.py`](../scripts/apply_merge_queue.py) | Enumerates the four organizations in [`policy/merge-queue.json`](../policy/merge-queue.json); skips a repository when its property value is exactly `sylphx_delivery=delivered`, as well as the policy's explicit exclusions. | Already cross-organization. A new null delivery value does not satisfy the skip predicate; enumeration and policy targets are unchanged. It does not read `sylphx_ci_gate`. |
| [`scripts/is-delivered.sh`](../scripts/is-delivered.sh) | Reads one named repository; exits 0 only for exact `delivered`, 1 for a readable non-delivered result, 2 for an unreadable result. | A readable absent value and a readable null value both return 1. |
| [`scripts/set-delete-branch-on-merge.sh`](../scripts/set-delete-branch-on-merge.sh) | Enumerates the requested owners (default SylphxAI), using `is-delivered.sh`; skips delivered or unreadable repositories. | Does not discover additional owners from the promoted properties. A readable null value does not change the decision. |
| [`scripts/public_repo_consumers.py`](../scripts/public_repo_consumers.py) | Enumerates one `--org` (default SylphxAI); skips exact `delivered`. The scheduled workflow explicitly passes `--org SylphxAI`. | Scheduled scope and delivery exclusions are unchanged. **A manual run against another organization can behave differently:** its “any custom properties readable” guard can now pass because the new CI property makes the map nonempty. It still requires the separate `sylphx_consumer` value and may report missing consumers. |
| [Stack-conformance action](../.github/actions/stack-conformance/action.yml) | Reads the current repository's delivery value; skips only exact `delivered`. | No fleet enumeration. Null does not skip or install a new workflow. |
| [Workflow-lint performance check](../.github/actions/workflow-lint/perf_gate.py) | Reads the current repository's event property map, falling back to its property API when that map is absent; skips exact `delivered`, and skips unreadable API state in Actions. | No fleet enumeration. Null and absent delivery keys in a readable map are both non-delivered. This is a predicate comparison, not a claim that event payloads or credential-readability states never change. |
| `SylphxAI/infra`, `infra/addons/desk/manifests/desk-gh-state.py` (`repo_view`, `owner_repos`, `print_repos`) | Caches and displays all `custom_properties` for one explicitly requested owner. | **Its output changes:** the other organizations can now display `sylphx_ci_gate=required`. It does not turn that property into a check, enqueue work or enumerate new owners. |
| `SylphxAI/renovate-config`, `.github/workflows/dependency-updates.yml` | Iterates the 75 repositories explicitly listed in `runner/config.json`: 32 SylphxAI, 37 Cubeage, 4 EpiowAI, 2 OzyrixLtd. The delivery API read is conditional on owner being SylphxAI, and exclusion requires exact `delivered`. | New property metadata in the other organizations does not alter this workflow's decisions. Its comment saying the property exists only in SylphxAI is now stale; the code still has that owner condition. This is not evidence that future delivered values outside SylphxAI would be excluded. |

No operational code reader of `sylphx_ci_gate` was found beyond the GitHub rule;
the saved Enterprise candidate is unapplied and explicitly organization-scoped.
The audit found no current cross-organization automation that starts applying CI
or writing to the extra 409 repositories merely because the properties appeared.
It did find the display and manual-check differences above. This conclusion is
about the inspected source, not an assertion that every external integration or
historical workflow execution was inspected.

## Evidence and search limits

Current default-branch revisions read:

- `.github`: `bad84251961919376fb4f9aac6b943f4b345df70` (plus this documentation-only correction).
- `infra`: `cd87fbc1b0d02d3f6b381b8b2d9da6d77cbeb50c`.
- `renovate-config`: `3b9d1473242c034c84c6f323542df4401779b7cf`.
- `anyagents`: `8cd1741e71c2ec21e24d0e59e127aec0932cd224`.
- `desk-tools`: `2f0095d9bd57e59136be275d23300b1703d423ac`.

`anyagents`' `spawn_admin_sylphx_delivery` is message delivery, not a GitHub
property reader. `desk-tools`' `operator-api-sweep` mentions the delivery property
in a comment but actually excludes BGCA by explicit repository name; its portfolio
and seven live Cubeage-title inputs are independent of these properties.

The search covered both literal names across all five organizations using GitHub
code search; each response reported `incomplete_results=false`. Hits in the other
four organizations were documentation only. The `.github` and `infra` source
search also covered `custom_properties`, `properties/values` and property-condition
references, to catch generic readers. Search indexing, accessible repositories
and dynamically constructed names limit an absence claim; this is not a census
of every possible executable integration.

## Before changing a property definition or default

1. Read the full repository-property population and live ruleset conditions.
2. Search both literal names **and generic property API readers**, including
   organization/repository enumeration and scheduled workflow callers.
3. Compare absent, null, explicit exemption, delivered and unreadable states.
   A value-comparison predicate and a property-presence guard are not equivalent.
4. Require explicit organization scope and a delivered exclusion for the saved
   SylphxAI Enterprise CI candidate. Property defaults alone now span every member
   organization, including live titles and customer repositories.
5. Read back the full population and verify a real merge through a required
   repository; a successful property API write does not prove queue continuity.
