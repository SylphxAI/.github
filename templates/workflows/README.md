# ADR-29 Admission Template

Use `adr29-admission.yml` as the thin repo-local caller for the organization
ADR-29 admission workflow.

Rollout rules:

- use the direct composite action template when the check may later become a
  required branch-protection context; reusable workflows report nested check
  names such as `caller / callee`;
- keep `policy-mode: observe` until the repo has branch and `merge_group`
  evidence for `risk-classification/pass` and `trunk-admission/pass`;
- add the repo's existing required CI lanes to `trunk-admission.needs`;
- add non-GitHub-Actions required commit statuses, such as `sylphx/preview`,
  to `required-status-contexts` so `trunk-admission/pass` can eventually replace
  raw branch-protection contexts without dropping runtime preview proof;
- set `runs-on` to the repo's standard self-hosted runner label when
  GitHub-hosted runners are not allowed;
- do not switch branch protection from raw contexts to `trunk-admission/pass`
  until the repo has a working postsubmit backstop plan;
- migrations must have expand/contract proof, side effects must have
  idempotency plus flag or kill switch, and runtime behavior must have a
  canary/progressive rollout guard before enforcement.
