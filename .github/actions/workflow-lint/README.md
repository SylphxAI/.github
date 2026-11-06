# Workflow lint

The action runs checksum-pinned actionlint 1.7.12, the pre-merge performance
rule, and a workflow contract check. Actionlint resolves `needs` and `steps`
references and rejects malformed workflows; contract mode never disables it.

The performance rule permits `lhci collect`, which records reports without
asserting budgets. Before merge, `lhci assert --config=path.json` is allowed
only when the repository-local JSON declares score thresholds exclusively for
accessibility, best-practices and SEO. JavaScript configs, timing categories,
metrics, presets, budget files and extra CLI overrides remain refused. An
explicit step condition excluding `pull_request`, `pull_request_target` and
`merge_group` with `&&` permits that step's post-merge Lighthouse assertion;
it exempts neither its job nor another step. Dependency audit policy is unchanged.

```yaml
- uses: SylphxAI/.github/.github/actions/workflow-lint@<commit>
  with:
    contract-mode: enforce
```

The contract scans `.github/workflows/*.yml` and `*.yaml` in the checkout:

- Every executable job declares `timeout-minutes`. A reusable-workflow call
  job cannot have this field under GitHub's schema; check the called workflow's
  executable jobs instead.
- Jobs on the transitive `needs` graph of `ci-ok`, a job named `ci-ok`, or a
  job using the shared `needs-pass`/`ci-ok` action cannot set `continue-on-error`
  except to literal `false`, at job or step level, or run `|| true`.
- With no aggregate, or an aggregate without `needs` (the API-based `ci-ok`
  action), every job is treated as required. Optional jobs outside an explicit
  aggregate graph may tolerate failures, but still need timeouts.

`contract-mode: report-only` emits warnings and a count for legacy timeout
and failure-propagation findings. Manual approval/maintenance booking and
[R8 merge-lane routing](../../../docs/merge-lane.md) remain blocking in either
mode. R8 follows all workflows in the checkout and their local reusable calls,
including matrices, input defaults and job conditions, using the same core as
the immutable-source required verdict and fleet audit.
Runner selectors are evaluated separately for each declared triggering event;
local reusable workflows inherit the caller's event. PR labels and supplied
inputs remain unknown until bound, so every reachable pool is checked. A PR
label filter is absent on merge groups. Unsupported or unresolved runner
selectors fail closed with the construct or context named in the finding.
R8 applies only to merge groups; IO prerequisites apply whenever any event can
reach the IO pool. An unreachable IO literal does not allocate an IO runner.
It is the initial default so callers can inventory and repair violations before
switching their gate to `enforce`. The org-wide rollout must pin the new action,
repair the inventory, require its gate, and then change the shared default to
`enforce`; publishing this action alone does not complete that rollout.

Delivered customer repositories are excluded via the same `sylphx_delivery`
property check as the performance rule. In Actions, an unreadable property
skips the rule rather than changing a customer's workflow.

The runner needs Python 3 and pip. The action installs PyYAML 6.0.3 into a
runner-temporary directory; it does not modify the runner's system packages.
For an offline inventory after installing that dependency, run from the repo:

```sh
CONTRACT_MODE=report-only python3 path/to/workflow-lint/contract.py
```

`ignore` and `args` configure actionlint only; the contract always scans the
checkout's workflows. Python tests are `python -m unittest discover -s tests
-p test_workflow_contract.py` from the shared action repository.


## IO pool prerequisites

Jobs whose evaluated `runs-on` can select `sylphx-linux-io` (including expressions,
lists, label mappings and literal matrix choices) must meet these blocking rules even
in report-only mode. Publishing the checker does not activate this runner pool.

1. No inline `run:` steps.
2. Every remote action uses a full 40-hex commit SHA. Pinned remote composites
   are trusted at that SHA; the checker does not fetch or recurse into them.
3. Local actions require an identifiable preceding checkout of `SylphxAI/.github`
   with an explicit 40-hex `ref`. The checkout path must provide the action;
   conditional, PR-head, dynamic-ref and unrelated checkouts do not qualify.
4. For `workflow_run`, the job condition or every io routing branch requires
   `github.event.workflow_run.head_repository.full_name == github.repository`.
   Mentioning the comparison in an unrelated OR branch does not qualify.
5. Job-level permissions explicitly include `contents: read`. Other permitted
   read scopes are `actions`, `pull-requests`, `checks` and `statuses`. Only the
   pinned shared `pr-review` action (including a qualifying local checkout) may
   request `id-token`, `checks`, `statuses` or `pull-requests` write access.
   Secrets are denied by default, including inherited workflow environment
   values. `io-reviewer-secrets` names the reviewer token exceptions, separated
   by commas; only reviewer jobs may use them. Dynamic secret lookup is refused.

Run `python -m unittest discover -s tests -p 'test_*contract.py'` for the workflow
contract and io prerequisite cases. The permission and expression semantics are
based on the [GitHub workflow syntax](https://docs.github.com/en/actions/writing-workflows/workflow-syntax-for-github-actions)
read on 2026-11-06 UTC. Expressions whose io safety cannot be established must
use the explicit job condition or a conjunction on each io branch.
