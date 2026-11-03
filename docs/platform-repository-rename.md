# Platform repository rename compatibility

The platform repository is GitHub repository ID **1117000933**. Its current
name is `SylphxAI/cloud`; the authorized destination is `SylphxAI/sylphx`.
The rename itself is a separate operation after dependent compatibility has
landed. This repository does not rename it.

## Shared policy compatibility

The shared actions accept both exact names for the same platform-owned paths:

- Agent runtime and Knowledge owner declarations retain their ownership.
- Notify and the conversational operator bot retain their path-scoped chat
  sender allowances; unrelated repositories and paths remain rejected.
- AI catalog/model-selector exemptions and immutable catalog sources accept
  either name; commit and content-digest requirements remain unchanged.
- Money and entitlement paths retain economy-cache enforcement; infrastructure
  and credentials do not accidentally acquire that domain rule after renaming.
- Merge-queue required checks and the `deploy/pins` gate match explicit
  `repo_aliases`. A renamed repository does not produce a phantom missing
  ruleset. Required-check matching still requires the exact ruleset ID and name.

The architecture catalog already accepts both shared-layer names and resolves
products by numeric repository ID. Its dated population and the force-push
audit remain historical evidence, not newly renamed snapshots. The runner-label
source is also an immutable, dated read, not a repository-name authorization.

`tests/test_platform_rename.py` exercises both names, wrong organizations,
similarly named unrelated repositories, wrong paths and wrong rulesets. It also
runs the required stack-conformance CLI and the queue apply/readback fixture.

## GitHub Actions sequencing

Read on **2026-11-03 at 15:12:43 UTC**: GitHub's [repository rename
documentation](https://docs.github.com/en/repositories/creating-and-managing-repositories/renaming-a-repository)
says action calls do **not** follow rename redirects. A full commit pin does
not make the old `uses:` repository path valid after renaming.

At `.github` commit `2d3754ae9e91dba47d443e582a91f95cde5acbcc`, there are no
`uses: SylphxAI/cloud/...` consumers in this repository's actions, reusable
workflows or starter templates. There are no platform-name-bound Broker trust
policies, OIDC subject allowlists or registry image paths here to rewrite.
Broker and registry actions pass the caller's numeric `github.repository_id`;
trust declarations belong to their caller's platform configuration. Preserve
currently published image paths independently of the source repository rename.

Organization code searches on 2026-11-03 found no old-name executable `uses:`
consumer in indexed workflow/action/template files. This is an indexed search,
not a guarantee about unindexed branches or external consumers. A stale example
exists in `SylphxAI/sylphx-clients/localize-action/README.md`:
`SylphxAI/cloud/clients/localize-action@main`. Update that published example
through its source owner, and inventory all affected live callers again at
cutover. The existing public `sylphx-clients/localize-action` can provide a
rename-independent action location once its exact pinned revision is verified.

1. Merge compatibility here and in callers' trust/identity configuration.
2. Move executable action consumers to an already existing, verified action
   location, or prepare a coordinated post-rename update. Never point a running
   workflow at a destination repository that does not yet exist.
3. Rename repository ID 1117000933, then read its new identity back.
4. Update source links, current policy names and any deferred `uses:` paths;
   retain dated historical receipts. Pin updated shared actions to a full SHA
   followed by `# main`, using the ordinary caller pin updates.
5. Verify an actual post-rename queue merge and platform release/build/publish/
   deploy paths. Remove temporary old-name compatibility after callers and
   trust configuration have moved. The longer failure observation is separate
   from cutover verification.
