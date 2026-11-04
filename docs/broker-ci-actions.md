# Broker-backed CI actions

These source adapters are not evidence that a CI principal, TrustPolicy,
Connection or registry publisher has been deployed or approved. Keep caller
activation separate from source publication. No live exchange or publisher run
was executed to validate this change.

## Signed caller identity

The `image-lane` and `red-main` **actions** run inside the caller's top-level
job. They do not introduce a reusable workflow identity. Declare the actual
signed workflow and job demand; do not copy a previous reusable workflow grant
or replace `job_workflow_ref` with `workflow_ref` as though they meant the same
thing. The existing reusable workflows and `git-app-credentials` remain
unchanged for external tenants.

GitHub primary sources read on 2026-11-03 at 18:00:09 UTC:
[OIDC with reusable workflows](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-with-reusable-workflows)
and [action metadata](https://docs.github.com/en/actions/reference/workflows-and-actions/metadata-syntax).
Composite steps cannot own job permissions, environments, timeouts or job
concurrency. Those belong to the top-level caller.

## One repository per exchange

`broker-write-token` retains `trust-policy`, `ci-name`, `repository`, `api-url`
and `soft-fail`. Optional `permissions` is a JSON permission map. Setting
`read-only: 'true'` requires explicit nonempty read permissions and selects
`ExchangeReadToken`, not an ordinary exchange followed by a client-side check.
There is no write-exchange fallback. Every call requests exactly one full
`owner/name` repository.

The action reads the runner's OIDC URL/token directly, requests audience
`sylphx-access`, issues one Access key, waits for its native propagation
interval, then checks Whoami against the key's issued stable subject. Only the
explicit `key_not_yet_propagated` readiness read can repeat; issuance and Broker
exchange are never retried. Errors do not echo provider response bodies.

Outputs include `token`, `issuer`, `connection`, `expire-time`,
`token-exchange`, `permissions`, `app-slug` and `failure`. Returned repository,
permissions, resource parents and expiry are checked before exposing a token.
`expected-issuer` and `expected-connection` optionally pin the exact caller's
resources. There is no company-global issuer, Connection or bot-slug default.
When a bot identity is needed, pass `app-slug`; its public App id must equal the
Broker issuer. The action persists no provider token file or credential URL.

## Independent source credentials

`broker-git-credentials` inputs:

- `ci-name`: exact Access CI declaration.
- `repositories`: comma/newline-separated full `owner/name` values.
- `trust-policies`: JSON map from each requested repository to its TrustPolicy;
  missing, extra and duplicate targets fail before issuance.
- `api-url`, `expected-issuer`, `expected-connection`: ordinary endpoint and
  optional exact pins.

The helper requests a separate `contents: read`, `metadata: read` token for
each repository. It exports `CARGO_NET_GIT_FETCH_WITH_CLI=true`,
`CARGO_GIT_CREDENTIALS_FILE` and `CARGO_GIT_CREDENTIAL_HELPER`, plus job-scoped
Git configuration. The file is a 0600 TSV (`lowercase owner/name`, tab, opaque
token) inside a random 0700 runner-temp directory, not Git credential-store
format and not a token-bearing URL. The output is `credentials-file`.

The POSIX `credential.sh` answers only HTTPS `github.com` requests naming an
exact approved repository path (optional `.git` suffix). It never answers a
host-only request, another host, unknown repository or store/erase operation.
It works unchanged on the host and inside a container without Python.

Preserve existing `GIT_CONFIG_COUNT` entries. The helper appends resets for
stale credential helpers and generic/GitHub checkout extraheaders, then adds
the path-aware helper. A fresh global config preserves only `safe.directory`;
non-GitHub command-scoped source-cache routing and session headers survive.
Consumers must not put secrets in remote URLs, argv or logs.

For BuildKit, mount both exported paths as secrets and configure the build's
Git process with `credential.useHttpPath=true` and a helper such as
`!sh /run/secrets/cargo_git_helper /run/secrets/cargo_git_auth`. Never feed the
TSV to `credential.helper=store`. Remove the credentials file with an
`if: always()` step immediately after source reads, before minting registry
credentials. No token may enter an image layer or build artifact.

## Image lane

`image-lane` preserves image, tag, source-sha/depth, context, Dockerfile,
platforms, build/evidence/verify commands and evidence-prefix inputs, and all
five outputs: `image_tag`, `digest`, `evidence_tag`, `evidence_digest`,
`verify_receipt`.

Use `phase: all` for one top-level job, or two jobs named `preflight` and
`publish` with `publish.needs: preflight`, calling `phase: preflight` and
`phase: publish` respectively. Each job needs its own exact signed publisher
admission. The preflight must succeed before compilation starts. The publish
job needs a self-hosted runner, a 180-minute bound, `contents: read`,
`actions: read`, `packages: read`, and `id-token: write`.

Private source inputs are `broker-ci-name`, `broker-trust-policies`, and
`source-repositories`, with the same optional endpoint/resource pins as the
source helper. Empty source-repositories does not mint a source token.

The tools are copied from the immutable pinned action bundle, never checked
out from `main` or inferred from `job.workflow_sha`. The existing native-build,
OCI/evidence/readback checks and registry credential cleanup run unchanged.
Source credentials are removed before registry mint; registry credentials are
removed before a caller verifier and again on every terminal path.

## Red main

`red-main` supports `mode: repair` and `mode: lifecycle`, retaining the
candidate, protection, verify-workflow and lifecycle-event inputs. It requires
`broker-trust-policy`, `broker-ci-name`, `broker-app-slug`; optional endpoint and
resource pins are forwarded. It only acts on its own caller repository.

The caller owns the self-hosted job, environment and repository concurrency;
use a 120-minute repair bound and grant `contents: read`, `actions: write`,
`issues: write`, `checks: read` and `id-token: write` to the workflow token.
Actions dispatch/read and a failed-mint notice use that separate workflow
authority. Source/issue/PR writes never fall back to it.

Repair still reads committed `sylphx.toml` before acting. It preserves the
classification, counted proof, confirmation, trace and migration protections.
Lifecycle requests only issues write plus metadata read. Repair requests
contents/issues/pull_requests/workflows write plus metadata read. A new Broker
token is issued after the potentially hour-long trace; failure prevents all
post-trace writes, without falling back to the old token. The bot identity is
derived from that freshly verified issuer/slug, not global App variables.

## Maintaining and testing the adapters

`scripts/generate-ci-composites.py` renders both composites from the existing
workflow programs; `--check` detects drift. Change the shared workflow behavior
once, then regenerate the adapters. Composite tests compare unaffected run
bodies, parse every shell program, compile lifecycle Python and run a flattened
top-level projection through actionlint. Fake-wire tests exercise the real
Broker adapter and real Git tests exercise the portable credential helper,
including independent grants, refused scopes, stale credentials and preserved
source-cache sessions. These are source tests, not deployed acceptance.
