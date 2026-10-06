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
