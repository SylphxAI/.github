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

For required CI, use [stack-conformance](../../../docs/stack-conformance.md#passwordless-auth-surface).
The standalone action retains its legacy default diff behavior and accepts
`mode: full`, `base`, `head`, and `baseline` for committed-tree audits. Full mode
reads tracked Git blobs from the selected head, not the index or worktree:

```sh
python3 /path/to/shared/.github/actions/auth-surface-guard/check.py \
  --mode full --base "$BASE_SHA" --head "$HEAD_SHA" \
  --baseline .github/auth-departures.txt
```

Omit `--baseline` to require zero findings. Without `--base`, full mode audits one
revision against its own baseline; it cannot establish inheritance. With a
base, a first baseline can contain only exact base-existing findings. Invalid,
missing, grown, and stale baseline entries fail. A committed empty file is valid.

Test paths and shared implementation prefixes are out of scope. Full mode and
the strict diff gate recognize a standalone `//` or `#` comment containing
`auth-surface-guard: allow(legacy-continuity)`. With a base, the comment only
carries forward exact findings from an already annotated base file; it cannot
waive new surfaces. Preserve live legacy account continuity during cleanup.
