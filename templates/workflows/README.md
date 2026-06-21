# ADR-29 Admission Template

Use `adr29-admission.yml` as the thin repo-local caller for the organization
ADR-29 admission workflow.

Rollout rules:

- use the direct composite action template when the check may later become a
  required branch-protection context; reusable workflows report nested check
  names such as `caller / callee`;
- for high-throughput repos with scarce self-hosted runner capacity, prefer
  embedding the action in the existing required CI job with `publish-status:
  true` and exact contexts such as `risk-classification/pass` and
  `trunk-admission/pass`; this preserves future branch-protection contexts
  without adding a post-check runner pickup;
- keep `policy-mode: observe` until the repo has branch and `merge_group`
  evidence for `risk-classification/pass` and `trunk-admission/pass`;
- add the repo's existing required CI lanes to `trunk-admission.needs`;
- add non-GitHub-Actions required commit statuses, such as `sylphx/preview`,
  to `required-status-contexts` so `trunk-admission/pass` can eventually replace
  raw branch-protection contexts without dropping runtime preview proof;
- set `runs-on` to the repo's standard self-hosted runner label when
  GitHub-hosted runners are not allowed;
- when using embedded status publication, grant `statuses: write` only to the
  workflow that publishes the admission contexts and keep `checks: read` off
  unless a repo has a separate documented check-run fan-in requirement;
- the action publishes and reads commit statuses against the pull-request head
  SHA, merge-group head SHA, or `GITHUB_SHA` by default; set `status-sha` only
  for non-standard status producers with a documented SHA binding;
- do not switch branch protection from raw contexts to `trunk-admission/pass`
  until the repo has a working postsubmit backstop plan;
- migrations must have expand/contract proof, side effects must have
  idempotency plus flag or kill switch, and runtime behavior must have a
  canary/progressive rollout guard before enforcement.
