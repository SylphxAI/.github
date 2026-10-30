# Workflow pin check

A workflow that calls another repository's reusable workflow or action pins it by commit
(`uses: owner/repo/path@<40-hex>`). Pinning the head of that repository's *unmerged* pull request
works while the branch lives. When the pull request is squash-merged and its branch deleted, the
commit is unreachable, and the next `merge_group` run fails at startup with no job and no annotation
(the case that stopped a title's merge queue on a pin to a studio pull request head).

`workflow-pin-check` fails the pull request that adds such a pin, while it is still open. It also
rejects a main-branch commit that lacks the referenced action or reusable workflow. Main ancestry
alone does not prove the target existed when that commit was made.

## What it decides

For every `uses: owner/repo[/path]@<40-hex>` in `.github/workflows/` and `.github/actions/`, where
`owner` is one of our organizations (the `orgs` of `policy/optimistic-merge.json`, or the `owners`
input), it captures the repository's default-branch commit, compares that immutable anchor with the
pin, and reads the pinned commit's complete Git tree:

| Compare says | Result |
| --- | --- |
| `identical`, `behind`, with the exact target present | pass |
| `ahead`, `diverged` (the pin is on a branch or pull request, not the default branch) | fail |
| unknown commit (404), repository or token unreadable | fail |
| action directory has neither `action.yml` nor `action.yaml` | fail |
| reusable workflow's exact `.github/workflows/*.yml` or `*.yaml` file is absent | fail |
| truncated or unreadable target tree | fail |

Only regular files count as targets; a symlink or submodule does not provide action metadata.
Each repository's anchor and each distinct commit's proof are read once per run, even when several
files reference different actions at that commit. The source reader and target resolution are shared
with the installed stack-conformance guard.

For `SylphxAI/.github`, both entrypoints also inspect the referenced action or workflow's shared
dependencies at their pinned commits. A valid parent workflow cannot conceal a missing action or
off-main pin in an older source revision. Failures name the caller's file and line, the failing source
file and line, and the dependency chain. Nested source references retain the source repository's
version-comment exemption.

Local reusable workflows and `$/` self-repository actions are read at the running source commit.
Ordinary `./` actions resolve in the caller's checked-out workspace and are not read from the remote
source. This follows [GitHub's workflow syntax](https://docs.github.com/en/actions/reference/workflow-syntax-for-github-actions)
(read 2026-10-30). Immutable source blobs are cached, and repeated dependencies are inspected once per
root reference.

A failure prints `file:line: pin owner/repo@sha: reason` and a `::error file=,line=` annotation. Tags,
branches and `./` calls are not judged here; third-party owners (`actions/*`) are out of scope. After
the dependency merges, repin to its merge commit.

## Using it

Repositories already running required `stack-conformance` use its shared-pin provenance check for
`SylphxAI/.github` references; they do not need another required job. This standalone action also
checks references to other repositories in the configured organizations.

For repositories that need only the shared-reference contract, set `shared-only: 'true'` on this
action. That opt-in mode calls the same validator as stack-conformance: every `SylphxAI/.github`
reference needs a full SHA, `# main`, main ancestry, an existing target, and valid shared dependencies.
Malformed branch, tag, missing and short-SHA refs fail. Other repositories are outside this mode,
so the ordinary read-only job token only needs to read the public shared source. The `owners` input
is ignored in this mode. Generic standalone behavior remains the default.

Copy `workflow-templates/shared-pin-check.yml`, replace its commit placeholder with a reviewed main
commit, and require `workflow-pin-check` on pull requests and merge groups. This template runs on
every pull request, so an unrelated change still receives its required verdict. Install one required
provenance gate per caller; a repository already requiring the current stack-conformance gate keeps
using that gate. Existing invalid references and missing version comments must be repaired in the
same caller change.

The equivalent local invocation is:

```
python3 scripts/workflow_pin_check.py check --shared-only --dir REPO
```

For a standalone installation, copy `workflow-templates/workflow-pin-check.yml` to the repository's `.github/workflows/`, replace
`PINNED_COMMIT_SHA` with the reviewed commit of this repository, and add the `workflow-pin-check` job
to the required checks. The token must read every repository the workflows pin (the reader App the
repin and web-smoke workflows already use). It runs on our own runners only.

## Fleet sweep

```
python3 scripts/workflow_pin_check.py audit --org SylphxAI --org Cubeage --json report.json
```

reads every non-archived repository's workflows and nested composite actions (hands-off repositories of the policy are skipped) and
lists each offender by repository, file and line. It is read-only, and exits 1 on any offender.

## Tests

`tests/test_workflow_pin_check.py` runs on a fake GitHub: an off-default pin and a missing action on
main fail with file and line; a valid action or exact reusable workflow on main passes; an unknown
commit or truncated tree fails; the default branch name is read rather than assumed; and each distinct
commit's proof is read once. `tests/test_shared_pin_provenance_gate.py` exercises the installed caller
entrypoint with valid, missing-target and off-main fixtures.
It also covers nested missing and off-main targets, valid source chains, same-commit reusable
workflows, self-repository actions, repeated references, and caller-workspace action paths.
`tests/test_shared_pin_action.py` runs the actual installed action shell in shared-only mode with a
read-only GitHub fixture, including malformed refs, missing comments, incomplete acquisition,
direct and nested violations, a clean YAML target, and an unrelated private reference.
