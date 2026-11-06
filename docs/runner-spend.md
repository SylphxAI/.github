# Runner spend contract

GitHub spend stays US$0. Standard `ubuntu-latest` is allowed only when the
executing repository is affirmatively public. Reusable workflows execute and
bill in their **caller's** context, not this public library's context.

Use this selector for a fixed hosted job:

```yaml
runs-on: ${{ github.event.repository.visibility == 'public' && 'ubuntu-latest' || 'sylphx-linux-standard' }}
```

Rust's optional `runs-on` input adds `inputs.runs-on == 'ubuntu-latest'` after
the visibility check. Inputs can choose an allowed label, never establish
visibility. Missing, private, internal or unknown visibility stays self-hosted.
Do not use `!repository.private` or `repository.private == false`: a missing
property is null, and Actions' truthiness/loose equality can admit it.

## Required library check

`tests/test_reusable_runner_spend.py` runs in the required Project control
unit-test job. It checks **every** `workflow_call` workflow, including workflows
with no local caller, and every action metadata file. Its scanner,
`scripts/reusable_runner_spend.py`, accepts only these constructive proofs:

- A literal `sylphx-linux-*` label, or a literal list containing `self-hosted`.
- A `format(...)` whose first argument is a literal starting with
  `sylphx-linux-`, before any interpolation.
- A runner input constrained by `startsWith(input, 'sylphx-linux-')`, returning
  that same input or a literal self-hosted fallback.
- The affirmative visibility expression above, optionally followed by an
  explicit `ubuntu-latest` input comparison, with a self-hosted fallback.

Unrecognized expressions fail closed, rather than being declared safe after
sampling a few inputs. Nested workflows must resolve to this inspected library;
an uninspected remote workflow is refused. Actions inherit the job's runner and
may not declare `runs-on`. No shared action was changed for this contract.

The named Rust tests cover private, public and missing-repository payloads on
`pull_request`, `push`, `merge_group`, `workflow_dispatch` and `schedule`, with
and without the hosted override. Omitted input preserves both existing sizes
and their same-size merge labels.

## Caller compatibility census

Read 2026-11-06 UTC using org-wide GitHub code search across SylphxAI and
Cubeage, then fetching each matched default-branch workflow through the API:

| Reusable workflow | Matched calling workflow files | Actual runner choices |
| --- | ---: | --- |
| `image-lane.yml` | 14 (16 calling jobs) | Default `sylphx-linux-standard`; explicit `sylphx-linux-standard-merge`, `sylphx-linux-large-merge` |
| `changes.yml` | 7 | `sylphx-linux-standard`, `sylphx-linux-standard-merge` |
| `rust-ci.yml` | 2 | This public repository opts into `ubuntu-latest`; the private caller omits the new input |

Thus the image/changes self-hosted-family constraint preserves every observed
caller value. No private caller in this indexed census passes Rust's hosted
override. Code search covers indexed default branches, not unpublished branches
or external organizations; the fail-closed selector protects those callers too.

## Primary sources

Read 2026-11-06 UTC:

- [Reusable workflow reference: GitHub context and hosted runners](https://docs.github.com/en/actions/reference/workflows-and-actions/reusing-workflow-configurations):
  the called workflow's `github` context is always the caller's, and hosted
  runner assignment/billing uses the caller's context.
- [Contexts reference](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts):
  `github.event` is the full webhook payload and a missing property evaluates
  to an empty value. No payload presence is assumed by this guard.
- [Repository object](https://docs.github.com/en/rest/repos/repos#get-a-repository):
  the repository object's `visibility` field identifies a public repository.
- [Expressions](https://docs.github.com/en/actions/reference/workflows-and-actions/expressions#operators):
  loose equality converts null and false to numeric zero; affirmative string
  equality avoids that false-public result.
