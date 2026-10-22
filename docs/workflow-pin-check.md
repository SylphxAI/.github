# Workflow pin check

A workflow that calls another repository's reusable workflow or action pins it by commit
(`uses: owner/repo/path@<40-hex>`). Pinning the head of that repository's *unmerged* pull request
works while the branch lives. When the pull request is squash-merged and its branch deleted, the
commit is unreachable, and the next `merge_group` run fails at startup with no job and no annotation
(the case that stopped a title's merge queue on a pin to a studio pull request head).

`workflow-pin-check` fails the pull request that adds such a pin, while it is still open.

## What it decides

For every `uses: owner/repo[/path]@<40-hex>` in `.github/workflows/` and `.github/actions/`, where
`owner` is one of our organizations (the `orgs` of `policy/optimistic-merge.json`, or the `owners`
input), it reads the repository's default branch and GitHub's compare of `default...sha`:

| Compare says | Result |
| --- | --- |
| `identical`, `behind` (the pin is on the default branch) | pass |
| `ahead`, `diverged` (the pin is on a branch or pull request, not the default branch) | fail |
| unknown commit (404), repository or token unreadable | fail |

A failure prints `file:line: pin owner/repo@sha: reason` and a `::error file=,line=` annotation. Tags,
branches and `./` calls are not judged here; third-party owners (`actions/*`) are out of scope. After
the dependency merges, repin to its merge commit.

## Using it

Copy `workflow-templates/workflow-pin-check.yml` to the repository's `.github/workflows/`, replace
`PINNED_COMMIT_SHA` with the reviewed commit of this repository, and add the `workflow-pin-check` job
to the required checks. The token must read every repository the workflows pin (the reader App the
repin and web-smoke workflows already use). It runs on our own runners only.

## Fleet sweep

```
python3 scripts/workflow_pin_check.py audit --org SylphxAI --org Cubeage --json report.json
```

reads every non-archived repository's workflows (hands-off repositories of the policy are skipped) and
lists each offender by repository, file and line. It is read-only, and exits 1 on any offender.

## Tests

`tests/test_workflow_pin_check.py` runs on a fake GitHub: an off-default pin fails with file and line, a
pin on or behind the default passes, an unknown commit fails, the default branch name is read rather than
assumed, and each distinct pin is read once.
