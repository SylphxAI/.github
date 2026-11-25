# Auth surface guard

Checks three source patterns from [The one sign-in flow](https://github.com/SylphxAI/skills/blob/main/skills/deliver-and-verify/references/auth.md#the-one-sign-in-flow):

| Rule | What it reports |
| --- | --- |
| `AUTH-R1` | Standalone signup, sign-up, register, forgot-password, and reset-password route files under recognized app, pages, or routes layouts |
| `AUTH-R2` | Literal password inputs in TSX, JSX, Vue, and Svelte, except [clearly identified masked integration credentials](../../../docs/stack-conformance.md#passwordless-auth-surface) |
| `AUTH-R3` | Recognized SMS sign-in identifiers such as `sendSmsOtp` and `signInWithPhone` in covered product source |

Use one **Continue** entry via the shared Sylphx Auth component or hosted portal.
This source check does not verify live sign-in behavior, redirect semantics,
or dynamically constructed inputs and routes.

Use the standalone action to make passwordless auth a required check before
adopting [stack-conformance](../../../docs/stack-conformance.md#passwordless-auth-surface).
Run the caller workflow on both `pull_request` and `merge_group` when using a merge
queue. Check out the candidate head (`HEAD` by default) and make the base commit
available locally. For example:

```yaml
- uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
  with:
    fetch-depth: 0
    persist-credentials: false
- uses: SylphxAI/.github/.github/actions/auth-surface-guard@<published-full-sha> # main
```

The default is `mode: diff` with `strict: 'true'`. The action uses `base-ref`
first, then the legacy `base` input, then the triggering event's exact base SHA:
`github.event.pull_request.base.sha`, `github.event.merge_group.base_sha`, or
`github.event.before` for a push. For other events, supply `base-ref`. The
action does not fetch missing commits. A missing or unavailable base fails
with exit code 2, including an all-zero push `before` (for example, a new
branch). In that case supply an available base or use `mode: full`.
`strict: 'false'` keeps the legacy diff checker and allows a first-parent
fallback only on events without an event base contract, when no base is
provided and the parent is available.

Set `mode: full` to audit the entire committed tree, regardless of which files
changed. A base is not required, even with `strict: 'true'`. Full mode uses
tracked Git blobs at the selected head, not the index or worktree. It still
uses an explicit base or the PR or merge group base when supplied, to prove
that baseline records and continuity exceptions are inherited. It does not
use the push `before` SHA automatically. To run a strict head-only audit:

```sh
python3 /path/to/shared/.github/actions/auth-surface-guard/check.py \
  --strict --mode full --head "$HEAD_SHA"
```

In a strict head-only audit, a nonempty baseline and continuity markers
cannot waive findings: there is no proof of inheritance. To verify inherited
departures, pass `--base "$BASE_SHA" --baseline .github/auth-departures.txt`.
With a base, a first baseline can contain only exact base-existing findings.
An invalid supplied base fails, as does an invalid, missing, or grown
baseline. Stale entries emit notices; they never authorize reintroduced findings.
A committed empty file is valid. A deliberate
non-strict head-only audit can use its own baseline and continuity exceptions
but cannot verify inheritance.

Test paths and shared implementation prefixes are out of scope. Full mode and
the strict diff gate recognize a standalone `//` or `#` comment containing
`auth-surface-guard: allow(legacy-continuity)`. With a base, the comment only
carries forward exact findings from an already annotated base file; it cannot
waive new surfaces. Preserve live legacy account continuity during cleanup.
