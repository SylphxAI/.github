# Automated PR review

The `PR review` default-branch workflow reviews a pinned pull request through
Sylphx AI and publishes the `Sylphx PR Review` Check Run from `sylphx-ai`
(App 1299545). The check is advisory. This change does not add a required check,
an approval requirement, or a merge-queue gate.

The trusted wrapper listens to the repository's existing PR CI workflow using
`workflow_run` with `requested` and `in_progress`. Requested starts promptly;
in-progress covers reruns. Both deliveries converge on one result per exact
base/head and reviewer version. Dispatch with a pull request number reviews its
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

JSON is split lexically outside strings, without parsing or serializing values.
Numeric/string spelling, key order, duplicates, whitespace and invalid input are
preserved exactly. Concatenating view segments recovers the source. Long literals
and other long lines split at bounded character boundaries without bisecting
Unicode surrogate pairs or CRLF. View separators are not original line breaks;
annotations always use physical source lines. Base-side `AGENTS.md` instructions
provide context.

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
deadline leaves the aggregate explicitly incomplete. Already validated findings
remain published: blocking defects fail the check; incomplete work with no
blocking defect is neutral. Incomplete work never passes. The runner retains
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

An inference refusal, timeout, incomplete result, invalid evidence, unsupported
source or malformed output receives neutral with a bounded reason. It never
becomes success. A superseded head is cancelled. A new head has a distinct
check identity and cannot inherit an old result. One workflow serializes duplicate
deliveries; creation looks up the stable external ID before its single POST.

A complete result requires `completion="complete"`, `omitted_paths=[]` and
`reason=""`. Explanations and non-blocking scope notes belong in `context_notes`.
Completion refusals identify the declared incomplete/unavailable state, omitted
path count and nonempty-reason flag independently. Model reason prose and omitted
path values are not published in these diagnostics. A diagnostic identifies the
failed contract conditions; it does not establish why the model returned them.

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
after a later capacity refusal.

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
