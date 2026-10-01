# Disarm auto-merge on push

A head-pinned arm is not permanent approval for later pushes. The reusable
workflow disables a stale arm when a `pull_request: synchronize` event arrives.
It uses the standard `GITHUB_TOKEN` with `pull-requests: write`, never an App
key, and never checks out or executes pull-request code. The existing
`sylphx-linux-standard` pool keeps private-repository GitHub spend at $0.

Call it from a dedicated workflow, pinned to a reviewed shared commit:

```yaml
name: Disarm auto-merge on push
on:
  pull_request:
    types: [synchronize]
permissions:
  contents: read
  pull-requests: write
jobs:
  disarm:
    uses: SylphxAI/.github/.github/workflows/disarm-automerge-on-push.yml@<commit>
```

No secrets are inherited. Fork pull requests receive the normal restricted
token and cannot mutate the repository; do not switch to `pull_request_target`
or run their code to bypass that boundary.

## Decision

One GraphQL read gets the live head, auto-merge arm time, and queue entry:

- Skip if the event's PR snapshot was not armed, the PR is closed, or its live
  head no longer equals `event.after`.
- Skip if a merge-queue entry already exists on that same head. This workflow
  never dequeues a same-head PR.
- Disable only if live `autoMergeRequest.enabledAt` is strictly earlier than
  the synchronize payload's `pull_request.updated_at` push time. A re-arm at
  or after the push, including a same-second arm, is preserved.

## Remaining race

GitHub exposes no auto-merge arm-head SHA and no conditional disable mutation.
Timestamps are second-resolution, and a re-arm or queue entry can occur
between the read and mutation. The event's `updated_at` is a push-time proxy,
not a separately signed receive timestamp. This workflow is stale-arm hygiene,
not the merge authorization boundary. The merge-group review stamp gate is the
backstop: failed/pending trusted stamps always block, and scoped missing stamps
block where the repository has enabled missing-stamp enforcement. Repositories
with deferred missing-stamp enforcement do not yet have that latter guarantee.

The arm workflow remains responsible for reviewing the exact head and using
`--match-head-commit`; this workflow grants no new approval.
