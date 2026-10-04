# Keel web smoke

One check for every Keel title: its packed web build is opened in headless Chrome and has to show
what a player needs to see while it loads, and then come up. It is the `web-smoke` check run that
the [Keel repin bot](keel-repin.md) requires before it merges a repin.

The check itself is Keel's (`scripts/browser_smoke.py`, from the title's own `KEEL_PIN`). The
`web-smoke` action here fetches that script, installs Chrome for Testing, serves the pack as a static
host does (Keel's `static_host.py`, so the pack's `_headers` and Content Security Policy apply) and runs
three checks, always all:

| Check | Passes when |
| --- | --- |
| `slow-3g-splash` | a cold load (empty cache) on Chrome's "Slow 3G" (400 ms round trip, 400 kbit/s) paints the title's splash within 1 s, the splash holds the title's name, shows no failure, and its progress bar then moves. This is the web boot standard: no black page while the title downloads. |
| `boot` (`--live`) | the title draws a ready frame (`data-keel-ready`) within 10 s with no boot failure, the console holds no error (a panic, an uncaught exception, a failed request), no synchronous wasm instance, large module compile or XHR on the main thread, and, with `tap`, one touch tap on that control changes the accessibility mirror. A black screen never gets a ready frame, so it fails here. |
| `served-q11` | every wasm module in the pack that has a precompressed `<file>.br` beside it is within 3 % of brotli quality 11 of the module (served/q11 at most 1.03), and the copy decodes to the module. This is what a host with brotli_static, or a worker that serves the `.br` copies, sends to `Accept-Encoding: br`; a host that compresses on the fly sends about 1.25. A module with no `.br` copy is reported, and fails only with `require-precompressed: "true"`. |

The browser is a runner's headless Chrome on a software GPU, not a phone: it proves a title shows a
splash and starts and answers a touch, not how fast it draws. Frame times are judged on devices.

## Adopting it in a title

1. Copy [`workflow-templates/web-smoke.yml`](../workflow-templates/web-smoke.yml) to
   `.github/workflows/web-smoke.yml`, pin `SylphxAI/.github` to a reviewed commit, and replace its
   "Pack the web build" step with the steps the title's CI already uses to build the web pack
   (Keel CLI at `KEEL_PIN`, wasm-bindgen, wasm-opt, `keel pack --profile web`). The action judges the
   directory those steps wrote (`pack-dir`).
2. Keep the job named `web-smoke`: the job name is the check run name, and the repin bot's `settle`
   step requires a check run of exactly that name.
3. Add `web-smoke.yml` to the repin workflow's `ci-workflows` so the bot starts it on the repin branch
   (the workflow lists `workflow_dispatch`, as the template does).
4. Add `web-smoke` to the repository's required checks.

The job needs `CUBEAGE_CI_READER_APP_ID` and `CUBEAGE_CI_READER_APP_KEY` (the read-only App that can read
SylphxAI/keel) as the other Keel jobs do. It runs on our own runners only: on a GitHub-hosted runner the
action stops with an error before it does anything.

## Inputs

| Input | Default | |
| --- | --- | --- |
| `pack-dir` | required | Directory `keel pack --profile web` wrote. It must hold `index.html`. |
| `reader-app-id`, `reader-app-key` | required | The read-only App for the Keel repository. |
| `keel-ref` | the revision in `keel-pin-file` | Keel revision whose scripts judge the pack. Set a newer one to hold an older title to a newer standard. |
| `keel-pin-file` | `KEEL_PIN` | Where the title's pin is. |
| `slow-3g-splash` | `true` | `false` only for a title whose Keel has no web boot standard yet and is being repinned. With `true`, Keel scripts older than the standard fail the check with a message that says so. |
| `splash-budget-ms` | `1000` | First contentful paint budget on Slow 3G. |
| `tap` | empty | Accessibility-mirror id of the start control to touch-tap once. |
| `viewport` | `390x844,touch` | Boot check viewport. |
| `budget-s` | `10` | Seconds to a ready frame (and for the bar to move on Slow 3G). |
| `ignore-console` | empty | Console errors to ignore, one regular expression per line. Empty: a failed request, a CORS block, a panic or an uncaught exception fails the check. |
| `offline-dependencies` | `api\.cubeage\.com/cubeage\.v1\.AuthService/` | Endpoints the title calls at boot that the smoke cannot reach, one regular expression per line. The pack is served from `127.0.0.1`, which the Cubeage API does not allow as an origin, so the guest sign-in is CORS-blocked; a network or CORS failure naming one is skipped and listed in the output. A 5xx, a console error from the title, any other endpoint and a missing frame still fail. Setting it replaces the default (list the default too to add an entry); empty means none. |
| `max-served-ratio` | `1.03` | Largest served/q11 a wasm module may have. |
| `require-precompressed` | `false` | `true` fails a wasm module that has no precompressed `.br` copy in the pack. Run the title's precompress step before the action, then set it. |
| `chrome-version` | pinned in `action.yml` | Chrome for Testing version; cached in the runner's tool cache. |

Output: `keel-ref`, the Keel revision used. A run writes its verdict to the job summary.

## When it is red

- `the splash painted after N ms` or `nothing painted`: the page waits for script or wasm before it
  paints, or the HTML has no splash. Fix the title's pack or repin to a Keel with the boot shell.
- `the page has no #keel-splash`: the pack predates the web boot standard. Repin.
- `no ready frame` or a boot failure: the title does not start. The log shows the page's own message
  and the console error.
- `served/q11 ... over the bound` or `no precompressed ... .br`: the pack's brotli copy of a wasm module is weaker than quality 11, or missing. Re-run the title's precompress step at quality 11 (`lgwin` 24) and point the host at the copies.
- A console error: a panic, an exception or a failed request on the page.

Nothing here edits the title: a red check is a title to fix, not a check to skip. If the smoke itself is
broken (Chrome download down, a Keel script change), fix it here or pin `keel-ref` to the last good Keel.

## Maintaining

`web_smoke.py` (next to the action) holds the logic and `tests/test_web_smoke.py` covers it with a
stand-in Keel `scripts/` directory. The Chrome for Testing version moves on purpose, in one place.
