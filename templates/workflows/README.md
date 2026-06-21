# ADR-29 Admission Template

Use `adr29-admission.yml` as the thin repo-local caller for the organization
ADR-29 admission workflow.

Rollout rules:

- keep `policy-mode: observe` until the repo has branch and `merge_group`
  evidence for `risk-classification/pass` and `trunk-admission/pass`;
- add the repo's existing required CI lanes to `trunk-admission.needs`;
- set `runs-on` to the repo's standard self-hosted runner label when
  GitHub-hosted runners are not allowed;
- do not switch branch protection from raw contexts to `trunk-admission/pass`
  until the repo has a working postsubmit backstop plan;
- migrations must have expand/contract proof, side effects must have
  idempotency plus flag or kill switch, and runtime behavior must have a
  canary/progressive rollout guard before enforcement.
