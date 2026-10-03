# Keel repin

When Keel cuts a `keel-verified-*` tag, every title that builds on Keel gets a pull
request that moves its pin to that tag, so no title waits for someone to remember.

## The flow

1. Keel's tag workflow cuts `keel-verified-YYYY-MM-DD-N` and starts its `keel-repin-fanout`
   workflow. The fan-out reads the list of title repositories from Keel's repository
   variable `KEEL_REPIN_TITLES` and dispatches each repository's `keel-repin.yml` in waves
   (see Keel's `docs/CONSUMER_GUIDE.md`, "Repin bot"). The list holds repository names, so it
   lives in a variable and not in Keel's tree, which names no customer.
2. Each title's `keel-repin.yml` (from `workflow-templates/keel-repin.yml`) runs the
   `keel-repin` action on our own runners.
3. The action resolves the tag to its commit with `git ls-remote`, moves every pin, refreshes
   `Cargo.lock` for the Keel crates, runs the title's build check, and opens or updates the pull
   request `chore/keel-repin-<tag>` with the repository's single `owner:*` label.
4. The pull request is never merged by the bot. A failing build (an API break, say) still opens
   it, as a draft with the last lines of the error, so the break is visible.

Each title also runs the same workflow once a day with no tag (`latest`), the catch-up when a
dispatch was missed. A branch that already exists is left alone, so a repeat run never overwrites
a fix someone pushed to it; close the pull request and delete the branch to rebuild it.

## What counts as a pin

- A `rev = "<40 hex>"` or `tag = "keel-..."` on a `Cargo.toml` line that names the Keel git
  repository, and every `deps/keel.rev`.
- The old full commit, and its 12, 8 and 7 character prefixes, in any other tracked text file
  (Dockerfiles, cargo config, CI, docs). `vendor/`, `target/`, `Cargo.lock` and `CHANGELOG.md` are skipped.
- A `# Keel tag: <tag>` comment in a `Cargo.toml` moves with the pin.

A title that needs more (re-vendored sources, a patch block) keeps `tools/repin_keel.sh <tag>`.
When that file exists the action runs it instead of the rewrite and the lock refresh, then runs
the check.

## Inputs worth knowing

| Input | Meaning |
| --- | --- |
| `tag` | A Keel tag, or `latest` (the newest `keel-verified-*`). |
| `dry-run` | Print the pull request it would open (title, draft, label, files, body); push nothing. Rebuilds even if the branch exists. |
| `reader-app-id`, `reader-app-key` | A read-only App that can read the private Keel repository. |
| `extra-read-owner`, `extra-read-repos` | A second private source the build fetches, such as a title kit. |
| `writer-app-id`, `writer-app-key` | An App with `contents: write` and `pull_requests: write` on the repository. Its pushes start CI. Without it the workflow token is used and CI does not start on its own (close and reopen the pull request). |
| `check-command`, `check-dir` | The build check. Default `cargo check`. |

## Tests

`python -m unittest tests.test_keel_repin`: pin discovery and rewrite, the build check, the
draft pull request, supersession of older repin pull requests, no merge call anywhere, and no
GitHub-hosted runner label. A consumer run: `workflow_dispatch` the title's `keel-repin.yml` with
`dry_run` true.
