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
retain the original exit-code annotation. Keep the provider's actual annotation
in lane signatures: different ordinary/self-cancelled signatures do not confirm
one another. Prefer uploaded JUnit for test identity; no original assertion text
is invented. Same-step failures without JUnit retain the existing lane-level
rather than test-level granularity.

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
