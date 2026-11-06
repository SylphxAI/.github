# Automated PR review

The `PR review` default-branch workflow reviews a pinned pull request through
Sylphx AI and publishes the `Sylphx PR Review` Check Run from `sylphx-ai`
(App 1299545). The check is advisory. This change does not add a required check,
an approval requirement, or a merge-queue gate.

The trusted wrapper listens to the repository's existing PR CI workflow using
`workflow_run` with `requested` only. Each new CI run starts one review;
rerunning CI on the same head retains that head's existing review. Dispatch with a pull request number reviews its
current head again, including a previous unavailable review. Normal merge-queue
checks continue independently; this advisory check does not claim a review of
the queue's combined merge commit.

## Credentials and configuration

Declare the exact wrapper `.github/workflows/pr-review.yml`, default branch,
numeric repository/owner IDs and events in Access. The finite CI key needs
`ai:inference` and `broker:exchange`. The wrapper needs GitHub `contents:read`,
`pull-requests:read`, `actions:read`, and `id-token:write`. GitHub source reads use
the caller's read token. Writes use only the Broker's one-repository installation
token with `checks:write`, and reject another App issuer or wider permissions.
There is no App private key in Actions.

Before activation, verify the chosen environment's owned/shared paid-account
capacity and zero metered budget through the ordinary AI policy API. An
exhausted account is unavailable; the client never changes models, selects an
area, retries a refusal, or adds a billed fallback. Configure repository variables:

| Variable | Meaning |
| --- | --- |
| `SYLPHX_REVIEW_TRUST_POLICY` | Existing exact CI subject's one-repository checks-only Broker policy |
| `SYLPHX_REVIEW_MODEL` | Approved, routable Sylphx AI catalog model ID |
| `SYLPHX_REVIEW_REASONING_EFFORT` | Optional setting supported by that model |
| `SYLPHX_REVIEW_MAX_OUTPUT_TOKENS` | Optional output budget; absent uses the gateway default |

In another repository, install the same wrapper with its own CI declaration,
listen to its existing PR workflow (for example `CI`), and use the published
`.github/actions/pr-review` action at an immutable shared release SHA. Check out
only trusted shared code. Never check out or execute the PR head in this job.

## Review and failure behavior

The collector enumerates all changed paths and reconciles their count. It reads
the live base policy, resolves the merge base and reads both source sides at
immutable commits. Each file is verified against its Git blob hash. Input version
2 carries full-file SHA256, byte/line counts and literal source fragments with
original UTF8 byte offsets and physical line/column coordinates. Git computes
complete hunks over the literal segment view, with external diff/text conversion
disabled. Every literal change is included. Small files retain full surrounding
source; large files retain 40 surrounding view segments per hunk.

Changed ordinary files that contain NUL or invalid UTF-8 use a finite
`binary-metadata-v1` representation instead of fatal text decoding. Each present
side retains its verified full-byte SHA256 and size, with status, rename identity
and the bundle's pinned commit identities. Raw binary bytes, base64 and lossy
replacement text never enter the prompt. Text sides of text/binary transitions
retain their literal hunks. Binary sides have no invented lines or annotations.

The result includes `binary_reviews` (empty for text-only scopes). Every binary
path needs an explicit source/derivation assessment and limitation. A complete
review requires verbatim quoted input-source and generator/build relationship
evidence for each binary path; unknown or materially unreviewable provenance
stays incomplete. The reviewer can request related text through normal pinned
context recovery. Partition manifests and synthesis retain binary identities,
assessments and their validated evidence. A completed partition may retain a
provisional unresolved binary assessment when provenance belongs to another
part; other parts carry relationship quotes in related evidence, and final
synthesis still requires resolved source/derivation evidence for every binary.
The Check summary makes the limitation
visible: source/derivation review does not decode the artifact or prove
regeneration/semantic equivalence. Byte hashes prove identity, not correctness.
Missing, truncated, corrupted or unsupported objects still fail closed before
inference. Policy and related-context reads remain text-only.

JSON is split lexically outside strings, without parsing or serializing values.
Numeric/string spelling, key order, duplicates, whitespace and invalid input are
preserved exactly. Concatenating view segments recovers the source. Long literals
and other long lines split at bounded character boundaries without bisecting
Unicode surrogate pairs or CRLF. View separators are not original line breaks;
annotations always use physical source lines. Base-side `AGENTS.md` instructions
provide context.

An identity- and schema-valid incomplete review can request related files through
`context_requests=[{path, side}]`, where side is `base` or `head`. The collector
reads ordinary UTF-8 files only from the sealed merge-base/head commits, verifies
the Git blob identity, and adds their side and commit SHA to the source bundle.
It never accepts a model-supplied ref, URL, command or repository. Renamed base
evidence uses the previous path; head evidence uses the current path. A missing
component in a complete pinned Git tree supplies `{path, side, commit_sha,
absent:true}` instead of invented empty content. This proves only that exact path
is absent at that commit, not that a dependency is unnecessary or the change is
correct. Present and absent evidence from a request batch are delivered together;
a later absent path never discards already collected source. HTTP failures and
unsupported source are not absence evidence.

The recovery bounds live in `recovery.mjs` and the request schema: at most three
augmentation rounds, sixteen added source identities (including absence), and one MiB of added text,
further limited by the qualified request-byte budget. Duplicate requests are
coalesced; source already supplied in full cannot trigger another augmentation.
Each genuine augmentation reseals the entire input and re-runs whole-scope
review, including partitioning and synthesis, using the same configured model,
output settings and original absolute deadline. The final Check and receipt bind
the final source digest. Earlier conclusions are replaced, not unioned across
enriched rounds: newly supplied guards or callers can disprove them.

When augmentation reaches its round/file/byte bound, has no new source to add,
or the model declares incompleteness without requesting source, one bounded final
verdict pass is allowed within the remaining original deadline. The input is
resealed with `context_recovery="exhausted"`; this pass requests no further context
and receives no corrective inference retry. It must complete the supplied scope
with explicit assumptions or grounded defects, never invented evidence. A valid
assessment that still declares incompleteness fails the Check as `Review incomplete`,
without manufacturing a code annotation. A final refusal, outage or deadline
remains unavailable. Previously grounded findings remain visible if that final
pass is incomplete or unavailable; a complete assessment can reassess an earlier
incomplete finding.

A pinned absence changes the evidence digest once and cannot trigger repeated
reads. It cannot be quoted as source and never proves correctness. Linked or
unavailable requested source, an expired deadline, and an unverified/changed head
stop recovery honestly. External dependency contents, including pinned action or
package revisions, cannot be collected from another repository: review the local
syntax, identity and call contract and state that limitation in `context_notes`,
not as an omitted path. Binary source/derivation requirements still apply; an
unverified equivalence limitation alone does not undo a completed provenance
review. There is no quota fallback, model change or manual verdict.

Explicit review paths take precedence over tier0/generated paths; unknown paths
default to review. A `[review]` title also requests a review. Known tier0-only
changes without the marker receive neutral with their classification. The model
receives source as data with no tools, credentials, repository execution, or
GitHub publishing authority.

Calls use the ordinary `/v1/chat/completions` gateway with strict JSON
schema output. The parser checks repository, base/head, source digest, reviewer
version, completeness, zero omissions and verbatim quoted evidence. Findings
must reference changed physical source coordinates. Partial-line evidence carries
exact start/end byte offsets; complete-line evidence uses null offsets and
canonical LF quotes. Deleted-code findings cite the base side in
the check summary; head-side findings become line annotations. Medium or higher
concrete defects fail; clean reviewed code passes. Low findings remain advisory.
If a finding points outside the changed ranges, its evidence is validated first.
The first verbatim-validated evidence overlapping any changed range supplies the
anchor, including another changed file or renamed base source, with severity
unchanged. If no evidence touches the change, retain the observation, original
severity and causal explanation as a visible non-blocking outside-the-change
note, never an off-diff annotation. Unknown finding anchors still fail validation.
A quote that cannot be grounded triggers the existing bounded correction attempts.
Once those attempts are exhausted (or disabled for the single final verdict pass),
discard only the ungrounded related-evidence item or its entire finding. Every
retained quote still passes verbatim validation. The part can complete without
that unsupported claim; identity, source coverage and binary-provenance checks
remain unchanged. Partition and synthesis results both preserve the discarded-item
count in the Check Run summary: `N items discarded: evidence not in source`.
No discarded quote or finding prose is published.

Each outbound response schema binds repository, base/head, policy, reviewer and
`input_digest` to that exact request using single-value enums. For a partition,
recovery round or synthesis, `source_digest` identifies the overall source bundle;
it is not that request's `input_digest`. A miscopied input digest receives bounded
correction within the existing three-attempt limit (one attempt in the final
verdict pass). The parser never rewrites a returned identity. A persistent digest
mismatch fails explicitly as `Review invalid`, retaining the failed part and
reason instead of publishing neutral. Actual gateway refusal, transport failure
or an unverifiable current head still uses the normal unavailability path.

Before inference, the action reads the configured model's supported public catalog
row at `/v1/public/models/{model}`. It reserves the configured output budget, or
the published output maximum when that setting is omitted, without changing the
request's output setting. It then reserves 4096 tokens for protocol framing and
uses the remaining context as a conservative limit on the entire serialized
request's UTF8 bytes, including escaping, instructions and schema. This is an
upper bound for byte-based text tokenization, not provider token usage. Missing,
invalid or mismatched metadata yields unavailable; provider admission remains
authoritative.

If the whole review does not fit, complete files, hunks and eventually individual
source fragments are distributed across bounded requests. Every part carries the
complete changed-path/hunk manifest and original source digest. Base instructions
accompany parts when they fit, or are completely partitioned as context units.
No path or hunk is dropped to meet a cap. Each part returns a behavior summary
and verified related evidence. A final bounded synthesis examines cross-file
interactions using those summaries and excerpts. All validated findings are
unioned; synthesis cannot discard them.

A failed part, unfit manifest/synthesis, missing material context or expired
deadline cannot produce a clean review. Model incompleteness can enter bounded
recovery and the final verdict pass described above; operational unavailability
cannot. Already validated blocking defects still fail the Check. With no blocking
defect, real unavailability is neutral, and a valid still-incomplete final
assessment fails explicitly. Incomplete work never passes. The runner retains
full validated results in `pr-review-results.json` and records part counts,
metadata read time and serialized request sizes in `pr-review-receipts.json`.
These byte receipts are not token-usage receipts. At larger input sizes, part
count grows while the existing review deadline stays fixed.

Every result-schema node declares its type, including enum and const leaves.
These leaves can be valid ordinary JSON Schema without a type while still
being refused by strict structured-output inference. The outbound-request
contract test checks the recursive typed schema before a live invocation.
An `invalid_json_schema` refusal remains unavailable; fix and publish the
shared action, adopt its immutable pin, and inspect the new consumer result
before claiming review acceptance. Do not retry an unchanged request.

The Check Run is the canonical verdict. The review command and finalizer exit
non-zero for a failure, cancellation, or unavailable/incomplete neutral result,
and put the reason in the step summary. A successful review exits zero. The
explicit `Review outside path policy` neutral is also a pass: the pinned policy
requires no review, so its receipt carries `applicable: false`, including when
reusing a completed canonical App Check Run. No other neutral title is exempt.
If one workflow reviews several heads, any non-passing result fails the job;
all Check Runs and receipts still publish before the command exits. A trusted
trigger with no eligible open heads performs no review and exits zero.

An inference refusal, timeout, unsupported source, invalid binary evidence or malformed
output receives neutral with a bounded reason when no grounded blocking finding
is already present. A model's valid incomplete assessment instead uses bounded
recovery and its final verdict pass; persistent incompleteness is failure, not
neutral or success. A superseded head is cancelled. A new head has a distinct
check identity and cannot inherit an old result. One workflow serializes duplicate
deliveries; creation looks up the stable external ID before its single POST.

A complete result requires `completion="complete"`, `omitted_paths=[]`,
`context_requests=[]` and `reason=""`. Explanations and non-blocking scope notes
belong in `context_notes`. Public completion refusals identify the declared
incomplete/unavailable state, omitted-path count and nonempty-reason flag
independently. They never echo incomplete model prose or requested paths.

After schema and pinned-identity validation, bounded private diagnostics retain
the actual reason (4096 characters), up to 32 exact known omitted names (1024
characters each), sixteen context notes (1024 each), and validated context
requests. A stale identity retains no model diagnostics. Per-round digests and
partition scope preserve provenance in `recovery_diagnostics` inside the
mode-0600 runner-local `pr-review-results.json`; these diagnostics are not
returned in public receipts, Checks, annotations, stdout or step summaries.
Recovery receipts include only machine-authored counts/stop codes, accumulated
serialized request sizes, and bounded `recovery_sources` outcomes. Each outcome
binds the requesting input digest, side, exact commit, SHA256 of the requested
path, status (`present`, `absent` or `unavailable`), and collected byte count.
Neither requested paths nor model prose cross this public boundary. Collected
bytes can exceed sealed `added_files` progress if a later operational failure
stops a batch; outcomes distinguish collection from delivery. A diagnostic
explains what the model reported, not whether its explanation is a proven root
cause.

A schema- and identity-valid result rejected for a failed evidence anchor retains
one bounded `validation_failure` in the final receipt and runner result. When
correction attempts instead discard unsupported finding or related-evidence
items, the receipt carries only their count and the runner result keeps opaque
item fingerprints for deduplication across cached parts and final assessment. Version 1
records the first rejected finding/related-evidence index, its exact input digest,
side, integer line/byte coordinates, and expected/returned UTF8 byte lengths and
SHA256 digests. `quote_mismatch` means those supplied coordinates resolved to
source with different bytes; `source_unavailable` means they did not resolve.
Only a known supplied path with a conservative printable spelling is emitted;
unknown or unsafe paths become null. Neither source quotes nor model prose enter
this receipt, Checks, annotations or summaries. Partition/synthesis failures keep
the failing part's digest, not an invented whole-review identity. A stale or
schema-invalid response retains no anchor receipt. This diagnostic does not
weaken verbatim validation, prove a root cause, add an inference retry or turn
unavailable work into a pass. The existing CLI prints and saves the safe receipt,
so it survives the ephemeral runner without exporting private round diagnostics.

The shared model deadline is ten minutes from review-step start; the process is stopped
at thirteen minutes and the always-run finalizer settles unfinished checks as
neutral. The job reserves twenty minutes for authentication, source collection
and finalization. A runner loss or hard job termination can prevent the finalizer;
dispatch the same PR to recover. If publishing credentials cannot be obtained,
the workflow fails with the exchange reason: it cannot manufacture an App check
without App authority.

## Acceptance and evidence

The lightweight Node suite runs in the existing Python unittest discovery and
tests exact-head isolation, real source diffs/windows, defect/clean result
publication, malformed/refused/incomplete responses, quota failures, strict
evidence and the narrowed credential exchange. These are contract tests with
fake network responses, not a live model quality claim.

The bounded-input cases include a two-megabyte minified fixture, exact literal
preservation, complete path/hunk partition coverage, byte-qualified evidence,
real rename/deletion coordinates, cross-file synthesis and retained findings
after a later capacity refusal within one review scope. Recovery cases also
cover pinned related-source acquisition, stale-round rejection, replaced findings,
partition/synthesis requests, no-progress and resource/head/deadline stops, and a
full CLI fixture proving mode-0600 diagnostic persistence without public leaks.
Three-part regressions exercise a part-2 digest mismatch after pinned context
recovery, persistent mismatch failure, exact request-bound schemas, and a part-2
unsupported quote without losing complete source coverage. These reproduce the
observed rejection classes, not the unavailable raw model response bytes.

Activation acceptance records the real Check Run ID/App/head, event-to-check and
review elapsed times, concrete findings, a new-push result, and a real defect/
clean fixture pair. Audit the first recorded PR population for incorrect defects
or unnecessary refusals before considering enforcement. Required-check changes
are a separate, evidence-based rollout.

Primary sources read 2026-10-30 UTC:

- [GitHub Copilot code review](https://docs.github.com/en/copilot/concepts/agents/code-review),
  12:39–12:44: automatic PR/new-push reviews and contextual findings.
- [CodeRabbit reviews](https://docs.coderabbit.ai/guides/code-review-overview),
  12:39–12:44: contextual and incremental review, actionable severity findings.
- [GitHub workflow_run](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_run),
  12:44: default-branch execution, requested/in-progress events, and trusted-code boundary.
- [GitHub Check Runs API, version 2022-11-28](https://docs.github.com/en/rest/checks/runs?apiVersion=2022-11-28),
  12:39–12:44: immutable head association, App publication and annotation batches.

Schema source read 2026-10-31 13:34 UTC:

- [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs),
  supported typed schema subset, required fields and strict-schema refusals.

Context metadata read 2026-10-31 22:43 UTC:

- [Configured review model catalog](https://api.sylphx.com/v1/public/models/openai/gpt-6.1-sol):
  `context_window=1050000`, `max_output_tokens=128000`. The implementation reads
  current metadata for the configured model, rather than hardcoding these values.

## Re-review after an interrupted review

CI reruns on the same head do not re-review; re-run the review run or dispatch.
The review remains valid for that head because the diff has not changed. A new
head starts a new review through `workflow_run: requested`.

If the first review was cancelled, neutral, or unavailable, inspect its cause and
confirm no review is already active for the current PR head. Re-run that review
with `gh run rerun <review-run-id> --repo <owner/repo>`; this preserves the original
`workflow_run` event. Alternatively, dispatch the review workflow with its
`pull-request` input only where the repository's Access policy allows
`workflow_dispatch`. Cubeage Platform permits `workflow_run` only, so recover
its automatic reviews by re-running the review run rather than dispatching.
Do not rerun CI solely to obtain another review.

Source: [GitHub workflow events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_run),
read 2026-11-06 at 20:58:42 UTC. GitHub does not emit `requested` for CI reruns.
