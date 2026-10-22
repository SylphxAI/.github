# SylphxAI/.github

Organization-level GitHub configuration: community health files, the profile,
templates, brand references, reusable workflows, and shared actions used by
more than one repository. [PROJECT.md](PROJECT.md) states the boundary.
Company standards are in `SylphxAI/owner` `standards/`.

- A change to a shared action or workflow reaches every caller that pins it, so
  callers pin by commit SHA and move deliberately.
- Keep secrets, tokens, and `.env` files out of the repository.
- Test with `python -m pytest`, narrowest target first.

## Landing and the desk

- Work in your own worktree under `/scratch/wt/`, never in a shared clone; no
  `git stash`, no `--no-verify`. The desk is IO-bound, so run only the narrowest
  test locally and leave full runs to CI.
- Every change lands through the merge queue with no admin or bypass merge. Arm
  with `gh pr merge --auto --squash --match-head-commit <sha> <N>`, re-arm
  after any push, and read back `isInMergeQueue`. Never retry a red queue entry;
  quarantine a flaky test. The full procedure is the cloud
  [merge-queue runbook](https://github.com/SylphxAI/cloud/blob/main/docs/runbooks/merge-queue.md).
- Whoever reviews a PR names who arms it and how; a green, reviewed, unarmed PR
  is a defect. A shared workflow change reaches every caller, so it gets the
  same review as a platform change.
- Search callers org-wide (`gh search code "<name>" --owner SylphxAI`) before
  removing or renaming a shared action or workflow.
