# Keel pin check

The Keel consumer guide says a title pins a weekly tag, never a moving main and never an arbitrary
commit. The `keel-pin-check` action enforces that on every pull request, so a pin cannot drift off
Keel's trunk or split in two without somebody seeing it.

## What it reads

Every Keel revision the repository pins, at any depth (so `vendor/` is covered too):

| Where | What counts as a pin |
| --- | --- |
| `KEEL_PIN`, `keel-ref`, `deps/keel.rev` | the first line: a commit (7 to 40 hex) or a `keel-verified-*` / `keel-weekly-*` tag |
| `Cargo.toml` | `rev`, `tag` or `branch` on a `git = ".../SylphxAI/keel"` dependency (one line, or a `[dependencies.x]` table); a Keel git dependency with none of them follows Keel's default branch |
| `Cargo.lock` | the commit after `#` in a `git+https://github.com/SylphxAI/keel...` source |
| `.cargo/config.toml` | `[source."git+https://github.com/SylphxAI/keel?rev=..."]` tables |

`target/` and `node_modules/` are skipped. A repository with no Keel pin passes with a notice.

## What it decides

| Finding | Result |
| --- | --- |
| a pin is not reachable from Keel main (an arbitrary commit, a side branch, a typo), or is a tag Keel does not have | fail, naming the commit and the files |
| a pin follows a branch, or a Keel git dependency names no revision | fail |
| the files pin more than one Keel commit (a lockfile with two revs, `KEEL_PIN` and `Cargo.lock` apart) | fail, listing each commit and its files |
| a pinned commit is on Keel main but is not the commit of a `keel-verified-*` or `keel-weekly-*` tag | warning, naming the newest tag |

"On Keel main" is `git merge-base --is-ancestor <pin> main` against a clone the action fetches from
Keel: its `main` and its verified and weekly tags, **commits only** (`--filter=tree:0`, so no trees and
no blobs; a few megabytes). The fetch is deliberately not depth-limited: with a cut history git cannot
tell a pin older than the cut from one that is off main. A short commit is expanded in that clone, and
the "tagged" test is on the tag's own commit (an annotated tag is peeled), not on any commit that merely
sits in a tag's history, because the guide asks a title to pin the tag itself.

## The ratchet

A failure only stands when the pull request **changes the set of Keel commits the repository pins**,
comparing the pull request's merge commit with its first parent (the base). On a `merge_group` run the
base is the first parent of the group's commit even when it has only one (a squash or rebase queue), so a
pin that `main` already carries does not turn the queue red. A pull request that leaves
the pin alone gets the same findings as warnings marked `[existing]`, so a title that already forks Keel
is not turned red by an unrelated change; the moment someone moves a pin, everything must be clean
(which also makes the pull request that repairs a fork pass). A push, a manual run (`base:` empty) or a
base that is not in the checkout judges every pin strictly. The starter passes `base: ''` on
`workflow_dispatch`, so a manual run is the full every-pin audit.

## Using it

Copy `workflow-templates/keel-pin-check.yml` to the title's `.github/workflows/`, replace
`PINNED_COMMIT_SHA` with the reviewed commit of this repository, and give it the read-only reader App
secrets the repin and web-smoke workflows already use. The checkout needs `fetch-depth: 2`. The job runs on
our runners only (the action refuses a GitHub-hosted one). Once a title's pin is clean, add the
`keel-pin-check` job to its required checks.

## Rolling it out

The ratchet keeps an existing fork green on pull requests, but a manual run, a push and the first pull
request that moves the pin judge every pin. So a rollout first audits each target's default branch with
every rule enforced, from any machine that can read the repositories and Keel:

```sh
python3 .github/actions/keel-pin-check/keel_pin_check.py audit \
  --keel-remote https://github.com/SylphxAI/keel --keel-dir /tmp/keel --work-dir /tmp/audit \
  https://github.com/Cubeage/<title> ...
```

Each repository is cloned at depth 1 without blobs over 2 MB (pin files are small) and gets one
`pass`, `FAIL` or `COULD NOT RUN` line with its findings; the exit status is the worst one (0, 1 or 2).
A repository that fails gets the pin fix in the same pull request that adds the check, or is skipped and
listed in the rollout's record with what it pins; never land the check alone onto a branch that fails it.

On [optimistic merge](optimistic-merge.md) the job also becomes a job of the gate: call the file from
`.github/workflows/ci.yml` and list the job in `ci-ok`'s `needs:` (docs/optimistic-merge.md, "The pull
request"). Required on its own, a check GitHub has not seen on the head is admitted, so the queue can
merge before this check finishes. A title where the pin check should not gate the merge keeps it
standalone and carries `# optimistic-merge: gate-external`.

The Keel repin bot ([keel-repin.md](keel-repin.md)) only pins a `keel-verified-*` tag's commit, so its
pull requests pass; listing this workflow in its `ci-workflows` puts the check on them too.

## Tests

`tests/test_keel_pin_check.py` builds a Keel history and fixture titles in a temporary directory and fetches
the fixture exactly as the action fetches Keel, with no network: an off-main pin fails naming the commit, a
verified tag's commit passes, an untagged main commit passes with a warning, two revs in one `Cargo.lock`
fail, and the ratchet, each pin location, short hashes, tags, branches, the exit codes and the rollout audit
(a fork the ratchet only warns about fails it) each have a case.
Run them with `python -m unittest discover -s tests`.
