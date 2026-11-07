# CI network requests

`curl_retry.py` bounds each logical HTTP request to 120 seconds, including
transfers, retries and backoff. It makes at most five attempts. Token requests
use a 10-second attempt limit; other reads and transfers use 30 seconds. The
backoff starts at 2 seconds and doubles (2, 4, 8, 16), with 0–25% added jitter.
Each attempt and wait is capped by the remaining budget. A slow operation can
therefore exhaust the budget before its fifth attempt.

Only curl exits 6, 7, 28, 35, 52 and 56, HTTP 429 and HTTP 5xx are retryable.
HTTP 401, 403 and 404 are permanent, even when the response body then times
out. There is no retry of a successful upload or an application-level refusal.
An uncertain upload reuses the same key, headers and bytes; the gateway's
immutable-entry conflict is still handled by run-store. A retained-key miss
can select the distinct legacy cache namespace once, not retry the same 404.

Every failed attempt emits a closed projection: operation label, attempt
number, curl exit, HTTP status, DNS/TCP/TLS/first-byte/total timings, remote IP,
retry delay and stop reason. Missing curl measurements are `unavailable`.
URLs, query strings, headers, curl stderr and response bodies are never copied
into diagnostics. Credentials and signed URLs use curl config on stdin;
`-q` disables user curl configuration. GitHub workflow credentials retain the
existing mask directives; terminal retrieval suppresses those directives.

Consumers:

- run-store: OIDC request, workflow/manual token mint, GET/HEAD/PUT;
- rust-sccache: OIDC request and token mint;
- unity-packages-cache: archive reads, retaining the existing per-hop redirect
  restrictions, exclusive destination creation, size and integrity checks;
- keel-pin-check: public crate metadata and source archives, retaining checksum
  and source-identity verification.

The Unity transport bounds disk writes even for unknown-length bodies on curl
versions whose `--max-filesize` checks only Content-Length. The download is
buffered in a private temporary directory before the existing archive consumer
reads it. Redirect hops share the enclosing deadline.

The actionlint and sccache release-archive installers already have bounded curl
retries and are unchanged. API-only clients and object-store bucket/lifecycle
mutations are not download helpers and are outside this policy's consumers.

Run-store accepts `RUN_STORE_REQUEST_BUDGET`, `RUN_STORE_REQUEST_MAX_TIME` and
`RUN_STORE_RETRY_DELAY` overrides in seconds. Its token-attempt limit remains
`BUILD_CACHE_MAX_TIME`. The sccache token helper also accepts
`BUILD_CACHE_REQUEST_BUDGET` and `BUILD_CACHE_RETRY_DELAY`. Defaults above apply
when unset. Python 3 and curl are required.
