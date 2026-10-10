# run-store

`.github/actions/run-store` hands a file or directory from one job of a
workflow run to another without GitHub artifact storage. Artifact storage is
metered per organization in GB-hours accrued over the billing cycle; once the
quota is spent, every `actions/upload-artifact` in the organization fails
("Artifact storage quota has been hit") and deleting artifacts does not give
the accrued hours back. CI therefore never depends on it.

The bytes go to the platform's BuildCache gateway through its Turbo door
(`{TURBO_API}/v8/artifacts/<key>`, SylphxAI/cloud `services/build/README.md`),
the store our CI caches already use. The job's GitHub OIDC token is exchanged
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
    - uses: SylphxAI/.github/.github/actions/run-store@<sha>
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
    - uses: SylphxAI/.github/.github/actions/run-store@<sha>
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
| `build-cache-url` | `https://build-cache.sylphx.net` | Gateway URL |
| `build-cache-network` | `public` | `cluster` for jobs on the platform's network |

Outputs: `key`, `found` (get), `bytes`.

Like `actions/upload-artifact`, a directory's contents are stored (not the
directory itself), and a single file is unpacked as `<path>/<file name>`.

## JUnit discovery across runs

The gateway has no wildcard/list API. Diagnostic producers MUST explicitly name
their top-level step `run-store put <name>`, with the same `name` input beginning
with `junit-`. For matrix jobs expand the same matrix value in both fields, for
example `run-store put junit-rust-test-${{ matrix.shard }}` and
`name: junit-rust-test-${{ matrix.shard }}`. Publish passing and failing testcases
with `if: always()`, `required: "false"` and `continue-on-error: true`.

The action's `get-junit.py <owner/repo> <run-id> <destination>` helper pages the
authenticated run jobs API (`filter=latest`), validates the run identity and
discovers exact keys from these step names, excluding skipped producers. It then
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
that completed producer run ID. The dispatch calls the reusable consumer at the
selected branch, and `ci-ok` includes its verdict. The automatic completion
consumer does not duplicate this dispatch. The pre-merge dispatch is necessary
for a newly added workflow: completion triggers load the default-branch caller.
These checks need no builder App secret or manually copied credential.

## Behaviour

- The key is `run-store.v1.<repository id>.<run id>.<name>`. Re-running only
  the failed jobs of a run reads the entries the successful jobs stored in the
  first attempt.
- A `protected` entry is written once. When a re-run producer writes a key the
  first attempt already wrote, the first entry (built from the same commit) is
  kept and a notice says so; a `dev` entry is replaced.
- A put packs with `tar | gzip -1`, refuses a bundle over 1 GiB (the gateway's
  largest entry) before uploading, retries three times, treats a write the
  gateway accepted but dropped (`x-build-cache: dropped`, its store was down)
  as a failure, and reads the entry back with `HEAD` before it succeeds.
- A get retries three times, a miss included (a gateway whose store is down
  answers reads 404), then fails with the key and run it looked for.
- Entries expire with the gateway's bucket after 7 days and count toward the
  organization's BuildCache byte cap (50 GiB by default).
- Linux and macOS runners; `jq` is used when present, otherwise `python3`.
  Both tokens are masked first and passed to curl on stdin only.
