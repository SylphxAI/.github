# Platform repository rename compatibility

GitHub repository ID **1117000933** was renamed from `SylphxAI/cloud` to
`SylphxAI/sylphx` on **2026-11-20**. Use `SylphxAI/sylphx` in new policy,
configuration and links.

## Shared policy compatibility

- The Tier-1 merge queue settings and the `deploy/pins` gate use the
  canonical name and keep the exact former name in `repo_aliases`. Alias
  matching does not replace ruleset name and ID checks.
- Agent runtime and Knowledge ownership, path-scoped chat senders and model
  exemptions, and economy-cache coverage keep their existing scope under
  either name. Historical immutable model catalog sources remain readable.
- The stack checker accepts the exact former decision scope when the caller
  is `SylphxAI/sylphx`, so it can read decisions from a pre-rename base. This
  does not change the finding, source hash, expiry, or other scope checks.
  New decision entries use the canonical repository name.
- The architecture catalog keeps its dated product and repository census;
  it resolves products by repository ID and normalizes the shared-layer
  name. The force-push audit and recorded run fixtures retain the names
  GitHub reported when they were captured. The runner-label source points
  to the same commit, path and read time at its canonical location.

`tests/test_platform_rename.py` covers both names and rejects unrelated
repositories, paths and rulesets. The stack-conformance CLI regression also
checks pre- and post-rename architecture decisions together.

## Pre-rename GitHub Actions notes

Read on **2026-11-03 at 15:12:43 UTC**: GitHub's [repository rename
documentation](https://docs.github.com/en/repositories/creating-and-managing-repositories/renaming-a-repository)
says action calls do **not** follow rename redirects. A full commit pin does
not make the old `uses:` repository path valid after renaming.

At `.github` commit `2d3754ae9e91dba47d443e582a91f95cde5acbcc`, there are no
`uses: SylphxAI/cloud/...` consumers in this repository's actions, reusable
workflows or starter templates. There are no platform-name-bound Broker trust
policies, OIDC subject allowlists or registry image paths here to rewrite.
Registry actions pass the caller's numeric `github.repository_id`. Broker's
write-token action instead sends an explicit caller-supplied `owner/name`
repository request alongside its OIDC exchange. Check both that request and the
caller's trust/identity binding in the platform configuration at cutover;
there is no platform-name-specific trust declaration in this repository.
Preserve currently published image paths independently of the source rename.

Organization code searches on 2026-11-03 found no old-name executable `uses:`
consumer in indexed workflow/action/template files. This is an indexed search,
not a guarantee about unindexed branches or external consumers. A stale example
was found in `SylphxAI/sylphx-clients/localize-action/README.md`:
`SylphxAI/cloud/clients/localize-action@main`. Update that published example
through its source owner, and inventory all affected live callers again at
cutover. The existing public `sylphx-clients/localize-action` can provide a
rename-independent action location once its exact pinned revision is verified.

Callers pinned to a pre-rename shared action need their normal full-SHA pin
update to consume the compatibility fix. Verify a real platform queue merge
and the platform release/build/publish/deploy paths before retiring aliases.
