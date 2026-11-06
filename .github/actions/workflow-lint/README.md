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
- Every independently triggered `pull_request` or `merge_group` job must be
  in that event's `ci-ok` transitive `needs` closure. The shared `ci-ok` action
  is also recognized under a different job ID, including local action paths.
  Legacy `ci-ok-polling` gates cover their nonignored observed checks across
  workflows; ignored or unresolved check names are not assumed covered.
- An intentionally nonblocking workflow declares a header comment with a reason:
  `# optimistic-merge: advisory — Nonblocking diagnostic report.`
  A bare marker, a comment in a run body or an independent reusable-workflow
  run cannot establish coverage. A reusable call in the gate does not await a
  separate PR-triggered run of that workflow; use `workflow_call` for required work.
- Without an explicit aggregate graph, the legacy failure-masking check still
  treats every job as required. Advisory jobs still need timeouts.

`contract-mode: report-only` emits warnings and a count for legacy timeout
and failure-propagation findings. Gate coverage, enrolled declaration drift, manual approval/maintenance booking and
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


## Resolved expected-check declaration

Repositories with independently triggered required checks enroll by committing
`.github/required-workflow-checks.json` and requiring the SHA-pinned
[organization verifier workflow](../../workflows/expected-checks.yml) in an
organization ruleset. **A verifier installed inside the evaluated repository
cannot establish trust:** the PR could replace it with a same-name no-op.
Do not install a consumer-local verifier workflow.

The [v2 schema](expected-checks.schema.json) is runtime data, not a second YAML
interpreter. Generate it from a checkout with:

```sh
python3 path/to/workflow-lint/expected_checks.py --write
```

The compiler resolves exact check names, triggering events and path filters from
that checkout. Enrolled drift blocks even in `report-only` mode. Repositories
without the file are not enrolled automatically. A declaration alone activates
neither a required workflow nor a verdict runtime.

Each record has `name`, `workflow`, `events`, `paths` and `paths_ignore`. Distinct
event filters produce separate records: a filtered PR run cannot make an
unconditional merge-group run optional. `paths: null` means always required;
ignore-only triggers normalize to `paths: ["**"]` plus the original
`paths_ignore`. The shared [glob vectors](../../../tests/fixtures/github-path-glob-vectors.json)
cover GitHub's documented grammar, including ordered exclusion/reinclusion;
Python `fnmatch` is not equivalent. Unsupported syntax is refused. Conditional
or matrix children need a direct, always-running aggregate that executes the
pinned shared `needs-pass`/`ci-ok` action over `toJSON(needs)`, without advisory
exemptions. The job guard must remain true after failure and cancellation;
`always() && success()`, `always() && !cancelled()`, or an `always()` string
literal cannot prove a boundary. Reusable `ci-ok` closures are pruned only across
these proven direct edges; a no-op aggregate or transitive pass-through is not
evidence of failure propagation. Required expression/matrix check names are refused; immutable
reusable calls expand to `caller / callee` names. A local call nested inside a
remote reusable stays in that remote repository at the same SHA. Literal non-matrix
producer names are checked before pruning covered jobs, including reusable
children and advisory workflows: a covered success cannot stand in for an
independent check with the same name. A matrix's unsuffixed base name is not a
literal emitted check identity and is not added to this census; required matrix
identities still need a proven static completion boundary.

Consumers match the complete changed-file set: PR base...head from fully
paginated PR files (GitHub's 3,000-file cap fails closed), or the whole merge
group's event `base_sha`...`head_sha`, never only the current PR's files. Unknown,
incomplete or empty diffs cannot prove irrelevance. Unrepresentable event
relevance is always required. A runtime reads JSON at the evaluated SHA and never
parses workflow YAML.

Completeness uses **two independent gates**. The verdict runtime requires a
present, schema-valid declaration at the evaluated SHA and evaluates its
resolved check identities. The organization ruleset separately requires the
pinned verifier workflow to succeed; GitHub enforces that workflow's identity
at enqueue/merge. A consumer-local same-name no-op cannot satisfy the rule.

Do not try to authenticate the organization verifier from ordinary REST check
metadata. The execution test on 2026-11-06 UTC showed that both required-source
and consumer-local runs report the target repository and local-relative
workflow path, with empty `referenced_workflows`. GitHub Actions app identity,
check name and `CheckRun.isRequired` do not prove which source executed. The
organization rule, not a guessed runtime provenance field, is the trust anchor.
A forged/incomplete declaration may mislead the runtime but cannot pass the
independent trusted verifier and cannot merge.

The org-required source checks out consumer and verifier separately. Verifier
code comes from `SylphxAI/.github` at `github.workflow_sha`, never from consumer
code. It checks consumer HEAD against PR head / merge-group SHA, reads workflow
YAML and declaration bytes from that **Git tree**, and compares canonical output
byte-for-byte. A dirty working tree cannot add, omit or shadow a check. Only
`.github/workflows/*.yml` and `*.yaml` are workflows; nested directories cannot
shadow a top-level file. Its reserved verifier record is always emitted;
consumer-local checks may not reuse the name. Missing or drifted declarations
refuse. A path-selected lint job is not the trusted completeness receipt.

**Boundary:** PR declarations bind the PR head. Ordinary PR Actions may use the
synthetic PR merge commit for local workflow source, so head-only verification
does not claim to describe base-branch changes introduced after divergence.
Mandatory merge-group verification over the actual merge-group tree is the
final merge-safety receipt. A PR that changes YAML and JSON consistently (for
example deleting a test job) is a CI-configuration change, not generator drift.
Workflow/action configuration must receive the automated high-risk code review;
the verifier does not decide whether the requested test policy is appropriate.

The same execution test proved `github.workflow_sha` equals the organization
rule's pinned source SHA, while `github.sha` identifies the target PR merge.
Pin the org rule to source repository `SylphxAI/.github` (ID `1091169653`), path
`.github/workflows/expected-checks.yml`, and the published full source SHA.
Stage declarations before activating the rule. At coordinated cutoff enable
the source rule, runtime declaration consumption and the temporary poller's
verifier requirement together. Existing PR heads without the declaration need
a normal branch update; missing data never means an empty inventory.


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
