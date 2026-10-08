# Workflow lint

The action runs checksum-pinned actionlint 1.7.12, the pre-merge performance
rule, and a workflow contract check. Actionlint resolves `needs` and `steps`
references and rejects malformed workflows; contract mode never disables it.

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

`contract-mode: report-only` emits warnings and a count, exiting successfully.
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
