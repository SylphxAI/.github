# Publish managed Artifact images

`artifact-image-publish` builds a runtime OCI image and an evidence OCI image,
publishes both through the public Sylphx Artifact API, and returns the caller's
promotion verification receipt. The composite runs in the caller's job, so
Access admits the source workflow's GitHub OIDC identity. Call it only from a
push job declared in Access. Other events fail before requesting credentials.

The only destination input is the canonical `project` resource name. The
action exchanges the job's GitHub OIDC token, with audience `sylphx-access`, at
`POST https://api.sylphx.com/v1/access/github/token` using `ci-name`. It calls
`POST /v1/{project}/artifacts:prepareUpload` for two unique upload references.
Runtime and evidence use the same server-selected `<org>/<project>-image`
repository. No registry name, repository, API key, or internal credential is
accepted as an input.

The action checks the returned credential's repository, exact pull/push grant,
audience and bounded expiry before use. These checks do not verify a JWT
signature locally: TLS authenticates the API answer, and the registry verifies
its signed credential. Runtime and evidence are pushed with `skopeo copy --all
--preserve-digests`. Authenticated registry HEAD and digest GET must match each
other and the exact local OCI transport entry. A rewritten manifest fails.

Each manifest is registered with `POST /v1/{project}/artifacts?managed_upload=true`.
The REST body is the Artifact itself, as specified by the public contract.
Both records have kind `image`, because managed registration supports OCI
images and indexes; the evidence record names the runtime Artifact as its
`subject`. The action reads each durable record back and checks its digest,
reference, kind, manifest size/media type, subject and UID. An existing-digest
conflict succeeds only after the same checks; a generic or failed record
cannot stand in for a managed upload.

Managed registration proves that the project's registry contains the bytes.
It does not claim signature or provenance verification. The required
`verify-command` retains the caller's existing promotion policy, including any
cosign/provenance checks. It runs once, after publisher credential cleanup,
against the authenticated registry manifests. The action emits promotion
outputs only after that command writes its receipt.

## Caller setup

The caller owns source checkout, native build engine setup, build dependencies,
and job timeout. Install Python 3.11+ and `skopeo` before this action. Docker or
Buildx is needed only when the build commands use it. The `platforms` input
must match the Linux runner's native architecture; emulation is refused.

Check out the exact `source-sha` before invoking the action. An empty input
uses `github.sha`; a mismatched checkout fails before authentication. Grant
the job `id-token: write` and the GitHub read permissions its commands need.
Access declarations independently bind the source repository ID, workflow,
ref, event, project and Artifact scopes.

Existing custom commands using shared scripts at `.image-lane/` must also
check out `SylphxAI/.github` there at the same full commit SHA used by the
action. That checkout is caller-owned. Exclude it using `.git/info/exclude`
for caller dirty checks; Buildx may still count ignored files, so commands
requiring pristine VCS provenance should relocate it outside the source tree
during the build and restore it afterward.

```yaml
on:
  push:
    branches: [main]

jobs:
  image:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      actions: read
      id-token: write
    steps:
      - uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5 # v4
        with:
          persist-credentials: false
      - uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5 # v4
        with:
          repository: SylphxAI/.github
          ref: <full-action-commit-sha>
          path: .image-lane
          persist-credentials: false
      - name: Prepare publisher tools
        run: |
          sudo apt-get update -qq
          sudo apt-get install -y -qq skopeo
          printf '.image-lane/\n' >> "$(git rev-parse --git-path info/exclude)"
      # Set up the caller's native build engine and build dependencies here.
      - id: publish
        uses: SylphxAI/.github/.github/actions/artifact-image-publish@<full-action-commit-sha> # main
        with:
          project: orgs/org_example/projects/prj_example
          ci-name: example-images
          platforms: linux/amd64
          build-command: bash scripts/build-image.sh
          evidence-command: bash scripts/package-image-evidence.sh
          verify-command: bash scripts/verify-image.sh
```

## Command environment

Commands run as Bash in the caller's current directory. Inputs travel through
environment variables and are never interpolated into an action shell body.
The action creates a private directory below `RUNNER_TEMP` for each invocation.

| Variable | Meaning |
| --- | --- |
| `LANE_ROOT` | Invocation directory; the verifier writes `verify/` here. |
| `OCI_DIR` | Runtime OCI layout output. |
| `BUILD_EVIDENCE_DIR`, `EVIDENCE_DIR` | Evidence bundle directory; must contain files. |
| `BUILD_EVIDENCE_OCI_DIR` | Evidence OCI layout output. |
| `IMAGE` | Server-selected repository, shared by runtime and evidence. |
| `IMAGE_TAG`, `BUILD_EVIDENCE_TAG` | Server-issued upload tags, stable throughout commands. |
| `PLATFORMS` | Requested native Linux platform. |
| `SOURCE_SHA` | Exact checked-out source commit. |
| `SOURCE_WORKFLOW` | Source job's `github.workflow_ref`. |
| `SOURCE_RUN_ID`, `SOURCE_RUN_ATTEMPT` | Source job's GitHub run and attempt. |
| `GH_TOKEN` | Caller job's standard GitHub token, with caller-granted permissions. |
| `ARTIFACT_REGISTRY_AUTH_FILE` | Temporary skopeo auth file, during build/evidence only. |
| `DOCKER_CONFIG` | Temporary Docker config directory during build/evidence. |

Custom builds may use the auth file to pull fixtures already in the same
project's managed repository. For example:

```sh
skopeo copy --authfile "$ARTIFACT_REGISTRY_AUTH_FILE" \
  "docker://${IMAGE}@${FIXTURE_DIGEST}" "oci:${FIXTURE_OCI_DIR}"
```

Credentials expire within one hour, the public Artifact mint cap. Build commands should pull their input
fixtures while the initial credential is valid. After the build, the action
exchanges fresh GitHub OIDC and calls `prepareUpload` again to refresh that
repository credential. It keeps the original two server-issued upload targets;
the refresh call's unused reference is discarded. Credentials are held in
owner-only temporary files, never action outputs, GitHub artifacts or caches.
The action cleans these files on errors and cancellation and removes them
before the verifier. The verifier does not receive `ARTIFACT_REGISTRY_AUTH_FILE`
and its `DOCKER_CONFIG` points to a separate empty location.

Verifier-only environment:

| Variable | Meaning |
| --- | --- |
| `REGISTRY_DIGEST` | Runtime manifest digest. |
| `BUILD_EVIDENCE_DIGEST` | Evidence manifest digest. |
| `IMAGE_REGISTRY_MANIFEST` | File containing exact runtime registry manifest bytes. |
| `EVIDENCE_REGISTRY_MANIFEST` | File containing exact evidence registry manifest bytes. |

Each OCI layout's `index.json` must name one transport entry. To publish an
image index with multiple children, including provenance attestations, wrap
those children in a blob and name that index from the layout root. Blob sizes
and SHA-256 digests are checked recursively before publishing. Evidence
publication is required; missing evidence is a failure.

## Outputs and receipt

The existing `digest`, `evidence_digest`, `image_tag`, `evidence_tag`, and
`verify_receipt` outputs are retained. `image_reference` and
`evidence_reference` are immutable `repository@sha256:…` references; `image`
is their shared repository. `artifact` and `evidence_artifact` are the two
registered resource names. Upload tags are unique server-generated names;
consumers promote immutable digests.

`verify_receipt` uses the existing base64 gzip tar format and 524288-byte
encoded limit. Extract it in a caller promotion job with:

```sh
printf '%s' "$RECEIPT" | base64 -d | tar -xzf - -C "$RECEIPT_DIR"
```

The action uploads no GitHub Actions artifact. A caller should promote only
after this action succeeds and its existing verification policy accepts the
receipt. Artifact registration retains the server's unverified state until a
separately authorized platform verifier changes it.
