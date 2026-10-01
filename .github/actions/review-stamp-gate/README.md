# Review stamp gate

A merge-group-only composite action extracted from cloud#11840. Every queued
PR requires a trusted successful head review; missing stamps fail closed.
Explicit trusted `failure`, `error`, or `pending` also blocks, including an Ops
veto outside the Ops class scope.

## Ownership policy is data

The action ships [policy.json](policy.json), pinned with its code:

- Platform repositories (`cloud`, `infra`, `.github`, `janus`, and `hands` if
  present) require `ops-security/review` from the Ops trusted stamper, creator
  ID `8020099`, with description beginning `PASS`, for every PR.
- Security, money-path, and migration changes in any repository require that
  same Ops review. Mandatory labels and path globs are in `policy.json`;
  repository config may add scope but cannot remove mandatory triggers. Labels
  only add requirements: no exemption or product-review label downgrades a
  class path.
- Other product changes require the owning lane's independent Opus final
  reviewer, never the author's builder. Each repository records the trusted
  creator IDs and status context in its `productReview` data.

All current desk lanes post with the shared creator identity `8020099`.
Product configs therefore explicitly trust `[8020099]`, not an empty list.
GitHub cannot distinguish the author's builder from an independent Opus
reviewer using that identity, nor prove the reviewing model. Independence is
an owning-lane process requirement, not a claim the status API proves. The
code rejects author==stamper for distinct, non-shared identities when the PR
API supplies the author ID. The shared identity exception is explicit data in
`policy.json`; absent author identity also limits mechanical detection.

## Caller

Pin the shared action to a reviewed commit. Include the job in the `needs` of
the repository's required aggregate check. Skipping outside `merge_group` is
expected; failures must never be advisory.

```yaml
review-stamp:
  if: github.event_name == 'merge_group'
  runs-on: sylphx-linux-standard
  permissions:
    contents: read
    pull-requests: read
  steps:
    - uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5
      with:
        fetch-depth: 0
    - uses: SylphxAI/.github/.github/actions/review-stamp-gate@<commit>
      with:
        config-path: .github/review-stamp.json
        trusted-creator-ids: '8020099'
```

Python 3 and git are the only runtime dependencies. The checkout retains its
read-only credentials for queue-ref enumeration and target-branch fetch.
The token needs `contents: read` and `pull-requests: read`; it performs no
writes. `trusted-creator-ids` is a comma-separated Ops creator list and must
be a subset of the IDs authorized by the pinned shared policy. Product creator
lists are repository data read from the base commit.

## Repository-owned scope

```json
{
  "context": "ops-security/review",
  "enforceMissing": true,
  "requireStampLabels": ["security", "money", "migration"],
  "requireStampPaths": [".github/workflows/", ".github/actions/", ".github/review-stamp.json"],
  "requireStampPathGlobs": ["**/auth/**", "**/*billing*/**", "**/payment/**", "**/migrations/**"],
  "productReview": {
    "context": "ops-security/review",
    "trustedCreatorIds": [8020099]
  }
}
```

`requireStampPaths` matches exact files or directories ending in `/`.
`requireStampPathGlobs` uses shell-style matching across path separators;
a leading `**/` also matches root directories. Missing-stamp enforcement must
be true; false or empty product creator lists are invalid. The mandatory
shared Ops triggers are evaluated before additive repository scope.

The action reads repository config from the merge group's base commit so a
PR cannot loosen its own policy. Only first adoption, when the base has no
config file, reads the queued copy. Every entry carried by a multi-PR group is
checked; unrecognized commits fail closed. Status reads are paginated.

The workflow and creator input still come from the queued commit. Removing
this step needs review; a pinned required workflow is the eventual hardening
boundary. Cloud#11840 retains its local implementation until its owner
explicitly adopts the shared action.
