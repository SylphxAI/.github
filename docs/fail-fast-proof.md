# Required failures before self-cancellation

A post-main workflow may release its concurrency slot after its first required
failure by force-cancelling its own run. GitHub then reports the failing job and
run as `cancelled`, and the aggregate job may never be created. A cancelled run
is not by itself either a failed proof or a successful proof.

## Opt-in producer contract

The exact step name **`Cancel failed current run`** is the required-job marker
(`FAIL_FAST_STEP` in the byte-identical CI-range and red-main proof helpers).
Callers put one shared, final hook with this name only in required jobs of the
reviewed main workflow. It runs only after `failure()` on a `push` to the tracked
branch, force-cancelling **its own** run with `actions: write`. Advisory jobs,
recording workflows, dispatches and merge-group jobs do not opt in. Changing the
name requires changing the shared contract and caller guards together.

The fallback returns `failure` only when:

- the full post-main push producer and its completed cancelled run are valid;
- the completed cancelled required job has exactly one marker;
- the marker completed successfully and an earlier completed step failed; and
- the job's run, attempt, SHA, linked check ID, Actions app, check suite, name,
  details URL, status and conclusion all match the authenticated producer.

The hook itself and later cleanup cannot supply failure evidence. Neither a
cancelled sibling nor a missing, renamed, duplicate, failed or unfinished hook
establishes failure. Unavailable producer evidence remains unverified.
**Success still requires the existing authenticated aggregate job/check.** The
fallback never authorizes deployment or a verified range baseline.

## Recovery consumers

`failed-jobs` normalizes only authenticated roots to `failure`, with steps
strictly before the marker. Lane selection, failed-unit signatures and infra
classification consume that same list, never all cancelled siblings.
Predecessor confirmation recognizes an authenticated cancelled failure and skips
plain cancellations. The per-unit regression window reads cancelled candidates
lazily within its 100-run bound, and admits their successful sibling lanes only
when the run is an authenticated required failure. A cancelled lane clears
nothing; a normalized failing lane clears a test only with the existing JUnit
coverage rules. Diagnostic candidate dispatches keep their normal behavior.

Force-cancel also replaces error annotations. The recorded probe's linked check
has only `The run was canceled forcefully by @github-actions[bot].`; it does not
retain the original exit-code annotation. For an authenticated self-cancelled
root, `failed-units` and `failed-evidence` recover the original error messages
from the provider's **attempt-specific** log ZIP. Ordinary failures still use
their linked check's complete annotations. This preserves matching against
ordinary diagnostic dispatches and infrastructure classification (such as cache
403s), without treating the cancellation message as the original failure.

The ZIP is streamed to temporary disk with a 35-second deadline and a 32 MiB
compressed-byte cap, enforced while receiving. The central directory's actual
records are counted before `ZipFile` can allocate entry objects; an advertised
EOCD count alone is not a bound. Selected members support only STORE and DEFLATE,
with bounded decompression and verified stream termination, actual size and CRC,
not the truncated output a false declared size can make `ZipExtFile` return.
Only unique failed-step files
before the marker are read, selected by the REST step number and provider job
directory convention used in `gh` v2.99.0. Job-directory collisions, missing or
ambiguous members, invalid step numbers, decoding/CRC errors, missing error
lines, more than 10,000 archive members, members over 4 MiB, or selected members
over 8 MiB stop diagnostics. The marker's file must exist, but neither it nor
later cleanup supplies error evidence. No timestamp or raw group-header guessing
is used: timestamps have second precision, and group headers contain commands,
not the reviewed step name. Unavailable logs cannot invent a match or authorize
recovery; authenticated failure metadata still cannot authorize deployment.

Prefer uploaded JUnit for test identity. Same-step failures without JUnit retain
the existing lane-level rather than test-level granularity.

## Provider receipt

Read on 2026-11-06 UTC: [force-cancel endpoint](https://docs.github.com/en/rest/actions/workflow-runs#force-cancel-a-workflow-run).
The endpoint bypasses `always()` work that ordinary cancellation can leave active.
An isolated self-hosted push probe in cloud, run
[42287638550](https://github.com/SylphxAI/cloud/actions/runs/42287638550), retained
its earlier failed step and successful marker after the run and job became
cancelled. Its pending successor passed the concurrency slot at 20:22:18 UTC,
when the first run completed, and succeeded at 20:23:37 UTC. The temporary probe
branch was deleted after reading both results. Provider fixtures and their exact
adaptation boundary are in `tests/fixtures/red-main/fail-fast/README.md`.

The same probe's attempt-1 archive was read on 2026-11-06 at 20:50 UTC. Its
`required/4_Synthetic required failure.txt` retains the original `Process
completed with exit code 1.` error; step 5 is the successful marker. The attempt
endpoint is documented in [Download workflow run attempt logs](https://docs.github.com/en/rest/actions/workflow-runs#download-workflow-run-attempt-logs)
(read 2026-11-06 UTC). The directory and step-number mapping follows the primary
[`gh` v2.99.0 implementation](https://github.com/cli/cli/blob/v2.99.0/pkg/cmd/run/view/logs.go),
read in the same session. An unsupported naming/collision case fails closed.

The ZIP layout follows [PKWARE APPNOTE sections 4.3.7 and 4.3.12](https://pkware.cachefly.net/webdocs/casestudies/APPNOTE.TXT)
(read 2026-11-06 at 21:18 UTC). Bounded DEFLATE uses the documented
[`zlib.Decompress.decompress(max_length)` and `eof`](https://docs.python.org/3/library/zlib.html#decompression-objects)
contract (read in the same session). Forged EOCD counts, unsupported BZIP2/LZMA,
false expanded sizes and incomplete/trailing compressed streams are regressions,
not trusted provider metadata.
