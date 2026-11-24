# GitHub Actions cache retention

`actions-cache-prune.yml` runs on `sylphx-linux-standard` and acts only on the
current `SylphxAI` repository. The nightly schedule in `.github` prunes `.github`
only. Other repositories can opt in to their own nightly runs using the reusable
workflow below. Manual dispatch is a dry run unless `apply` is checked. Both
paths require the caller's default branch; PR and tag events cannot prune.

```yaml
name: Repository cache retention
on:
  schedule:
    - cron: '43 3 * * *'
  workflow_dispatch:
    inputs:
      apply:
        description: Delete eligible caches (off by default)
        type: boolean
        default: false
permissions:
  contents: read
  pull-requests: read
  actions: write
jobs:
  retention:
    uses: SylphxAI/.github/.github/workflows/actions-cache-prune.yml@<FULL_MERGED_SHA> # main
    with:
      apply: ${{ github.event_name == 'schedule' || inputs.apply == true }}
```

Replace `<FULL_MERGED_SHA>` with the `main` commit containing the workflow.
Grant the repository access to `sylphx-linux-standard`. The caller's ephemeral
`GITHUB_TOKEN` has `actions: write` for cache deletion, `pull-requests: read` for
PR state and `contents: read` for checkout and refs. It cannot write caches in
other repositories. A reusable workflow cannot elevate the caller's grants;
no personal token, App key, or `secrets: inherit` is needed.

## What gets removed

- Cache entries under exact `refs/pull/<number>/merge` refs only if GitHub
  affirmatively reports that PR as closed (including merged).
- Cache entries under non-default branch refs only if the branch no longer
  exists or its cache was last accessed **more than** three days ago. A merge
  queue branch is additionally protected unless its associated PR is closed.
- `main`, `master`, the repository's actual default, open PR merge refs, tags,
  and unknown ref forms are never selected. This workflow has no API calls for
  releases, workflow artifacts, packages, or other organizations.

The workflow prioritizes larger entries and attempts at most 50 per run. It
records the cache ID, ref, size, reason and GitHub request ID in its job log.
Before each DELETE-by-ID it rereads the exact cache, default branch and relevant
PR or branch state. GitHub does not offer a conditional DELETE tied to ref/PR
state; these are fresh checks, not an atomic transaction with a concurrent PR
reopen or branch change. An API failure, unexpected response, throttle or quota
reserve stops the run without retrying writes. If a DELETE acknowledgement was
lost, check that ID's presence via the cache list before considering a fresh run.

To preview from a trusted checkout with Python 3.10+ and an already authorized
`GH_TOKEN`, run `python3 -I scripts/prune_actions_caches.py SylphxAI/<repo>`.
`--apply` is explicit and `--limit` can reduce or increase the cap (maximum 250).

## Optional future fleet schedule

No org-wide schedule or credential is installed. Prefer repo-scoped callers.
If central scheduling is needed, use a dedicated GitHub App installation
restricted to explicitly enrolled `SylphxAI` repositories. Give it repository
Actions write, Contents read, Pull requests read, and the implicit Metadata
read, with no organization permission. Store its key in a dedicated `.github`
environment limited to the protected default branch. Mint the token in that job
with pinned `actions/create-github-app-token`, explicit repository names and
permission inputs. Run the same script once per allowlisted repo, dry-run first.
Do not reuse `SYLPHX_BUILDER_PRIVATE_KEY`: it is not shared with `.github`, and the
Builder App lacks Actions write. `Cubeage/.github` is separate and not enrolled.
