# Native device capture in CI

`device-capture` runs the published `sylphx devices run --scenario --size` client
through an ordinary customer Android lease. The title owns its APK producer,
scenario, target environment and exact workflow identity. This shared action
contains no title names or default customer parent.

Before using it:

- Publish and install an **exact** CLI version exposing both scenario flags.
  Installing an older version or a mutable dist-tag does not prove capability.
- Declare the caller's numeric repository/owner, workflow authority, ref/event
  and canonical environment through ordinary Access. Grant only
  `sandboxes:read`, `sandboxes:write`, and `sandboxes:exec`; never put a person's
  stored key or a different workflow's token in CI.
- Retrieve and verify the test APK with the existing `run-store` handover.
- Grant `id-token: write` on the title-local capture job. A composite action
  preserves that job's workflow authority; a reusable workflow has its own
  `job_workflow_ref` and needs its own declaration.

Inputs are `ci-name`, `environment`, local `app`/`scenario` paths, one `size`
label, exact `cli-version`, and a new `output` directory. Repeat as separate
matrix jobs for phone/tablet so each run gets fresh application state.

The action checks the installed CLI version and flags before requesting any
identity. It requests Actions OIDC with audience `sylphx-access`, exchanges the
declared CI name, honors `active_after_ms`, and requires `/v1/whoami` to match
the exact environment and three scopes. The key stays in memory and the CLI
child's environment, never a file, argument, action output or login store.
There are no automatic exchange retries; unknown request receipts require
inspection before another dispatch.

Safe `identity-receipt.json` records request UTC/status/identifiers, environment,
principal and CLI exit. The CLI writes native PNGs, hashes, step receipts,
logcat and JUnit under `output/device`, and releases its lease on exit. Retain
that directory through the existing CI handover mechanism, including failures.

A zero exit proves the scenario executed with package/foreground/native-size
guards, **not** visual acceptance of a named lobby/table/shop. Inspect actual
PNGs before calibrating unattended navigation. It proves no account, purchase,
store rollout or continuity outcome.
