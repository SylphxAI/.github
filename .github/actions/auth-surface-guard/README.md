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
first, then the legacy `base` input, then the triggering event's exact base SHA
(`github.event.pull_request.base.sha` or `github.event.merge_group.base_sha`).
For other events, supply `base-ref`. The action does not fetch missing commits;
with strict checking, an empty or unavailable base fails with exit code 2.
Use `strict: 'false'` only if you deliberately want the legacy diff fallback to
the head's first parent (or no check in a single-commit checkout).

Set `mode: full` to audit the entire committed tree, regardless of which files
changed. Strict mode still requires a base. To audit just one revision without
an event base, run on a non-PR/non-queue event with `strict: 'false'` and omit
both base inputs. Full mode reads tracked Git blobs from the selected head, not
the index or worktree. To run the equivalent strict audit from the CLI:

```sh
python3 /path/to/shared/.github/actions/auth-surface-guard/check.py \
  --strict --mode full --base "$BASE_SHA" --head "$HEAD_SHA" \
  --baseline .github/auth-departures.txt
```

Omit `--baseline` to require zero findings. Without `--strict` or `--base`, the
CLI audits one revision against its own baseline; it cannot establish inheritance. With a
base, a first baseline can contain only exact base-existing findings. Invalid,
missing, grown, and stale baseline entries fail. A committed empty file is valid.

Test paths and shared implementation prefixes are out of scope. Full mode and
the strict diff gate recognize a standalone `//` or `#` comment containing
`auth-surface-guard: allow(legacy-continuity)`. With a base, the comment only
carries forward exact findings from an already annotated base file; it cannot
waive new surfaces. Preserve live legacy account continuity during cleanup.
