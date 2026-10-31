# run-store

`.github/actions/run-store` hands a file or directory from one job of a
workflow run to another without GitHub artifact storage. Artifact storage is
metered per organization in GB-hours accrued over the billing cycle; once the
quota is spent, every `actions/upload-artifact` in the organization fails
("Artifact storage quota has been hit") and deleting artifacts does not give
the accrued hours back. CI therefore never depends on it.

The bytes go to the platform's BuildCache gateway, the store our CI caches
already use. The default uses its Turbo door (`{TURBO_API}/v8/artifacts/<key>`);
an explicit `retention-days` uses `/v1/retained/<key>` (SylphxAI/cloud
`services/build/README.md`). The job's GitHub OIDC token is exchanged
for a run token scoped by the event: main, the merge queue and schedules write
the `protected` scope; a pull request or dispatch run writes `dev` and reads
`protected` then `dev`. A pull request can never write what a main run reads.

## Choosing the hand-over

| Data | Use |
| --- | --- |
| Small values (a version, a list, a short JSON) | Job outputs (`needs.<job>.outputs`, about 1 MB per job; gzip and base64 anything over about 100 KB) |
| Files or directories another job of the same run needs | `run-store` put and get |
| An image | The registry: build and push in one job |
| Anything cheaper to rebuild than to move | Rebuild in the consuming job |
| A diagnostic nobody downstream reads | Drop it, or keep `upload-artifact` with `continue-on-error: true` so it never fails a job |

`actions/cache` is not a hand-over: on our self-hosted runners an entry saved
by one runner class was missed by another in the same run.

## Use

The producer and every consumer need `permissions: id-token: write` (and
`contents: read` for checkout). In a reusable workflow, the caller's job must
grant it too.

```yaml
venue:
  permissions:
    contents: read
    id-token: write
  steps:
    # ... build into build/venue
    - uses: SylphxAI/.github/.github/actions/run-store@<full-commit-sha> # main
      with:
        mode: put
        name: venue-assets
        path: build/venue

build:
  needs: venue
  permissions:
    contents: read
    id-token: write
  steps:
    - uses: SylphxAI/.github/.github/actions/run-store@<full-commit-sha> # main
      with:
        mode: get
        name: venue-assets
        path: build/venue
```

| Input | Default | Meaning |
| --- | --- | --- |
| `mode` | (required) | `put` or `get` |
| `name` | (required) | The hand-over's name in the run, `[A-Za-z0-9._-]`, at most 100 characters |
| `path` | (required) | put: the file or directory to store. get: the directory to unpack into |
| `run-id` | this run | get: read another run's entry (a `workflow_run` or dispatch consumer) |
| `required` | `true` | get: fail on a miss, or with `false` set `found=false` and continue. put: with `false` a store failure only warns |
| `retention-days` | empty | put only: an integer from 1 to 30 retains an immutable bundle for that many days from publication. Empty keeps the existing seven-day cache policy |
| `build-cache-url` | `https://build-cache.sylphx.net` | Gateway URL |
| `build-cache-network` | `public` | `cluster` for jobs on the platform's network |

Outputs: `key`, `found` (get), `bytes`, `unavailable`, and `retained_until`
(retained put only, Unix seconds for the root's expiry).

Like `actions/upload-artifact`, a directory's contents are stored (not the
directory itself), and a single file is unpacked as `<path>/<file name>`.

## Retained handovers

Set `retention-days` on the producer when a bundle must remain available
beyond the default cache lifetime:

```yaml
- uses: SylphxAI/.github/.github/actions/run-store@<full-commit-sha> # main
  id: retained
  with:
    mode: put
    name: release-bundle
    path: dist/release.zip
    retention-days: "30"
```

Consumers use the same pin and ordinary `mode: get`; no retention input is
needed. Reads try the retained namespace first, then the Turbo namespace on
404, so existing seven-day handovers remain readable. A retained response
requires a matching SHA256 header before its bytes can be adopted.

The gateway confirms the requested duration and expiry for every entry.
Multipart pieces receive one extra day. The root commits last, with a deadline
ceiling equal to the earliest piece expiry; the gateway rejects publication
atomically if the requested lifetime would exceed that ceiling. Interrupted
uploads leave no root. A retry uses a fresh random piece generation, so stale
pieces cannot shorten the next completed bundle's lifetime. Unreferenced
pieces expire under the same retention policy.

A completed retained key keeps its first bytes and original expiry, in both
event scopes. A retry validates the existing root, every piece, assembled
digest and piece lifetimes before succeeding, including when another producer
wins a publication race. It returns the original `retained_until` and does
not renew the lifetime. A request for a longer duration than the original
receipt fails; choose a new name or run for a new publication. Empty
`retention-days` continues to use the existing cache policy and write behavior.

Retained aliases and their referenced blobs are exempt from cache eviction
until their deadline and count toward the existing organization byte cap.
A full store or a missing retention receipt fails the put. As with other
puts, `required: "false"` turns a failure into a warning; it does not establish
retention. Publication requires the gateway's retained API and its storage
lifecycle configuration, so an older gateway cannot silently accept a
retention request as an ordinary cache write.

## Retrieve an exact entry outside a workflow

Use `.github/actions/run-store/get.py` from the same full commit SHA as the
producer. It takes exactly four positional arguments: `owner/repository`,
producer run ID, stored name and destination directory. It uses the active
`gh` identity to read the target repository and Actions run, verifies that
their numeric repository/run identities agree, and derives the exact key.
It performs no wildcard search or latest-run selection.

The caller supplies their own ordinary Access secret key with organization-wide
`build:read`. Project-, environment- or resource-restricted keys cannot read
the shared CI cache. The helper exchanges that key at `POST /v1/tokens` using
`{"cache":"ci","access":"read","ttl_seconds":3600,"network":"public"}`;
the resulting token is read only. This uses the caller's existing identities
and grants and needs no workflow OIDC session.

For example, with the caller's key already in their credential manager:

```sh
op read 'op://Vault/Caller Access key/credential' |
  python3 shared-actions/.github/actions/run-store/get.py \
    owner/repository 123456789 release-bundle ./retrieved --api-key-stdin
```

Alternatively, supply the key through `SYLPHX_API_KEY`. Keys never belong in
arguments or source files. The helper passes authorization to curl over stdin
and suppresses GitHub mask directives in a terminal, where those directives
would expose the credential. `BUILD_CACHE_URL` and `BUILD_CACHE_NETWORK` use
the action's defaults unless the caller sets them explicitly.

The command needs `gh`, Bash, curl, tar, gzip and Python 3. It checks payload
SHA256, multipart size/digest, gzip integrity, all tar members and existing
destination symlinks before extraction. Missing, corrupt, mismatched or unsafe
entries exit nonzero without creating a destination or unpacking files. A
stored `release.zip` lands at `retrieved/release.zip`; the helper does not
extract that inner archive.

## JUnit discovery across runs

The gateway has no wildcard/list API. Diagnostic producers MUST explicitly name
their top-level step `run-store put <name>`, with the same `name` input beginning
with `junit-`. For matrix jobs expand the same matrix value in both fields, for
example `run-store put junit-rust-test-${{ matrix.shard }}` and
`name: junit-rust-test-${{ matrix.shard }}`. Publish passing and failing testcases
with `if: always()`, `required: "false"` and `continue-on-error: true`.

The action's `get-junit.py <owner/repo> <run-id> <destination>` helper pages the
authenticated run jobs API (`filter=latest`), validates the run identity and
discovers exact keys from these step names, excluding skipped producers. When
GitHub omits a completed job's step metadata, it reads that exact authenticated
job's log and accepts only a completed store receipt bound to the target
repository/run/key, or the protected-entry-kept receipt. Commands, action titles
and packed-byte messages do not prove publication. Every declared or attempted
producer must have its completed receipt; a failed upload cannot disappear from
the expected shard set. Reads use at most four
concurrent requests, 16 MiB and 60 seconds per log. Unavailable, oversized or
empty discovery fails explicitly; raw log contents are never echoed. It then
uses the existing exact-name get contract, unpacking each entry into its own
`<name>` directory. No static lane list or shard limit is assumed.
The repository id comes from the target repository API, not the shared workflow
source repository; run ids are always the producer's, including previous and
candidate runs. Callers need `actions: read`, `contents: read`, `id-token: write`
and `GH_TOKEN` (or `ACTIONS_TOKEN`) for those metadata reads.

The helper also writes `.run-store-lanes.json`, mapping each stored name to its
authenticated GitHub job name. Both failed-unit and regression-window parsers
use that mapping for sharded reports (including nested XML paths), so a test is
attributed to the actual matrix job rather than a guessed suffix. A partial
shard fetch publishes no reports: unread diagnostics cannot clear a failed test.

The handler checks out this helper at its own `job.workflow_sha`, never at a
failing run's commit. BuildCache's event-scoped token remains authoritative:
dev consumers can read protected entries but cannot overwrite them. Missing,
expired or unavailable diagnostics retain the handler's lane-evidence fallback;
they never become proof that a failed test passed.

## Authenticated consumer check

Project control publishes two real nested JUnit fixture shards (1 and 701) through
run-store. Its completion triggers `junit-consumer-control.yml`, which invokes
`junit-consumer.yml` in a different run with `actions: read` and `id-token: write`.
The reusable consumer checks out shared source at `job.workflow_sha`, calls the
unmocked discovery command and verifies both red-main parsers against the exact
matrix job names and passing/failing test identities. No GitHub artifact is used.
Publication, authentication, a missing shard or an identity mismatch fails this
check rather than silently skipping it. The summary records source SHA, producer
run URL and producer SHA for the consumer invocation evidence.

To exercise a replacement PR head before merge, first let its normal Project
control run finish publishing the fixtures. Dispatch the existing
`project-control.yml` workflow at that PR branch with `junit-producer-run` set to
that completed producer run ID. Supply `code-pr`, `code-base-ref`, `code-base`,
`code-head` and `code-merge` from that PR's fresh immutable snapshot as well, so
the stack and metadata gates check its actual base/head range. A dispatch without
that range scans all unbaselined historical findings and can fail unrelated to
the consumer. The dispatch calls the reusable consumer at the
selected branch, and `ci-ok` includes its verdict. The automatic completion
consumer does not duplicate this dispatch. The pre-merge dispatch is necessary
for a newly added workflow: completion triggers load the default-branch caller.
These checks need no builder App secret or manually copied credential.
Consumer qualification has a separate concurrency group so it cannot replace a
pending native PR run. Its successful dispatch is consumer evidence; the native
PR and merge-group checks still supply the required merge evidence.

## Behaviour

- The key is `run-store.v1.<repository id>.<run id>.<name>`. Re-running only
  the failed jobs of a run reads the entries the successful jobs stored in the
  first attempt.
- With the default policy, a `protected` entry is written once. When a re-run
  producer writes a key the first attempt already wrote, the first entry (built from the same commit) is
  kept and a notice says so; a `dev` entry is replaced.
- A put packs with `tar | gzip -1`, refuses a bundle over 1 GiB before
  uploading, and sends bodies of at most 64 MiB through ordinary authenticated
  entries. Default-policy bundles use the existing `ci-run-store-parts-v1`
  manifest wire format: `<key>.part-0`, `<key>.part-1`, then a small root
  manifest committed last. Retained bundles use `ci-run-store-parts-v2` with
  `<key>.parts-<32 hex generation>.part-<index>` pieces and a `generation`
  manifest field; the other fields remain `parts`, `size` and `sha256`.
  Readers accept both versions. Producers and consumers use the same action pin.
  The consumer also accepts existing unsplit bundles, verifies the assembled
  size and SHA256 before validating/unpacking, and leaves the destination
  untouched for missing or corrupt parts. Partial immutable part collisions
  fail before a manifest is committed. It retries three times, treats a write the
  gateway accepted but dropped (`x-build-cache: dropped`, its store was down)
  as a failure, and reads the entry back with `HEAD` before it succeeds.
- A get retries three times, a miss included, then fails with the key and run
  it looked for. A 404 is the only "no entry". While the gateway's object
  store is down it answers reads `503` (before 2026-10-19 it answered a false
  `404`), and the step says `store unavailable` and sets the `unavailable`
  output instead, so a failed get never reads as a producer that did not store
  the entry.
- A get validates every tar member before it writes anything: a name that
  escapes `path` (`..` or absolute), a symlink or hardlink, and a special file
  (fifo, device, socket) are refused, so an untrusted bundle (a pull request's
  cache, or another run's entry) can never overwrite a trusted file or reach
  outside the destination. Existing symlinks at the destination, its ancestors,
  or a member's target path are refused before extraction. A bundle of regular
  files and directories unpacks as before.
- Default-policy entries expire after 7 days. Explicitly retained entries
  use their confirmed deadline. Both count toward the organization's
  BuildCache byte cap (50 GiB by default).
- Linux and macOS runners; `python3` is required (`jq` is used when present).
  Workflow tokens are masked first and passed to curl on stdin only.

Upload diagnostics log packed byte count before transmission. A refused upload
records its HTTP status, transmitted request bytes and only a validated
Cloudflare ray or hexadecimal request identifier. Response bodies, arbitrary
headers, URLs and credentials are never printed. These diagnostics do not
attribute an earlier 413 whose response metadata was not retained.
