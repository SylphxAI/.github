# Pages publish gate

A title's web preview must serve the newest verified commit of main and must never go back to an older
one. The `pages-publish-gate` action decides one thing: is the commit this run would publish **ahead of**
the commit the preview serves now.

## Why not "is this run the main tip"

A deploy that waits for Verify (or CI) to pass on main finishes after newer commits have usually landed
on a busy main. A deploy step that skips every run whose commit is no longer the tip then skips almost
every run, and the preview freezes. GitHub also supersedes older pending runs of a concurrency group, so
the newest run is the one that always survives. Deploying from every passing run through a non-regress
gate gives both properties: the preview lags main by at most one Verify duration, and an older run that
finishes late is a no-op.

## What it decides

| Served commit vs the run's commit (GitHub compare) | `publish` |
| --- | --- |
| the run's commit is `ahead` of the served one | `true` |
| nothing served: the URL is 404/410, or the body holds no sha | `true` |
| `identical`, `behind` or `diverged` | `false` |
| the served sha or the comparison cannot be read (network, HTTP error, unknown commit) | `false`, with a `::warning::` |

An undecidable case skips instead of publishing: the next passing run corrects it, while a regress puts
an older build in front of whoever uses the link. The gate never fails the job over a decision; it fails
only for a malformed `run-sha` or a GitHub-hosted runner (our own runners only).

## Inputs and outputs

| Input | Default | Meaning |
| --- | --- | --- |
| `run-sha` | required | The 40-hex commit the deploy would publish: the verified run's `head_sha`, or `github.sha`. |
| `served-sha-url` | empty | A URL whose body is JSON with a `git_sha` field (a `product.meta.json`) or plain text holding the sha (`GIT_SHA.txt`). |
| `served-sha-key` | `git_sha` | The JSON field read from `served-sha-url`. |
| `served-sha` | empty | The served commit, when the caller already has it; wins over the URL. |
| `repository` | `github.repository` | owner/name the commits belong to. |
| `token` | `github.token` | Needs contents read; the workflow token is enough. |

Outputs: `publish` (`true` or `false`), `reason` (one line), `served-sha`.

The preview has to publish its identity for the gate to read: the commit it was built from, as
`git_sha` in `product.meta.json` or a `GIT_SHA.txt`, written by the publish step. A short sha (7 to 40 hex)
is accepted.

## How a title's web deploy uses it

Copy `workflow-templates/pages-publish.yml` to the title's `.github/workflows/` and replace the commit
placeholder with the reviewed commit of this repository.

- **Trigger**: `workflow_run` on the workflow that gates main (`Verify` or `CI`), `types: [completed]`,
  `branches: [main]`; the job runs only when the run's `conclusion` is `success` and its `event` is `push`.
  The deploy checks out `workflow_run.head_sha`, the commit that was verified.
- **Concurrency**: one group, `cancel-in-progress: false`. A deploy is never cancelled mid-publish, and the
  gate makes a stale queued run a no-op. Never put `cancel-in-progress: true` on a main deploy.
- **Gate**: the action runs right after checkout; the pack and publish steps run only when
  `steps.gate.outputs.publish == 'true'`. The readback after the publish still waits for the run's own sha.
- **Manual run**: `workflow_dispatch` skips the gate, so a person can republish and recover a preview whose
  served commit GitHub can no longer compare (a rewritten history).
- **A deploy inside the Verify workflow** (a job that `needs` the build) keeps its own `if: github.ref ==
  'refs/heads/main'` and calls the action the same way with `run-sha: ${{ github.sha }}`; it must not skip
  on "not the main tip" and must not set `cancel-in-progress: true`.

## Tests

`tests/test_pages_publish_gate.py` covers every row of the table with a stand-in compare call (ahead,
identical, behind, diverged, compare error, unknown status), reading the served commit from JSON and
plain text, the 404 and server-error paths, the outputs written to `$GITHUB_OUTPUT`, the token header of
the compare request, the runner guard, and that the starter deploys from `workflow_run` with
`cancel-in-progress: false`. Run them with `python -m unittest discover -s tests`.
