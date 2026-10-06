# Stack conformance

`.github/actions/stack-conformance` fails a change that adds a departure from
the default stack in SylphxAI/owner `standards/stack.md`: backend services in
Rust, first-party schema under Atlas. Departures a repository already has are
listed in its baseline, `.github/stack-departures.txt`, which may only shrink.
This is a ratchet in the style of Betterer, scoped like a Backstage Tech
Insights check: existing debt is recorded once, and new debt fails at the pull
request instead of being found by a later audit.

## What counts as a departure

One line per departure, the same format in the baseline and in the output:

| Line | Found when |
| --- | --- |
| `migrations <sylphx.toml> <engine>` | a `sylphx.toml` sets `[database.migrations] engine` to anything but `atlas` |
| `ts-server <package.json> <package>` | a package's runtime `dependencies` include a TypeScript server framework (`hono`, `express`, `fastify`, `elysia`, `koa`, `@trpc/server`, `@nestjs/core`, `@hapi/hapi`, `h3`) |
| `bun-node-service <Dockerfile>` | a `dockerfile` named in `sylphx.toml` ends on a Bun or Node image, its command starts Bun or Node (or a shell script), and neither its nearest package nor a package its command names is a web app (`next`, `astro`, `vite`, `@sveltejs/kit`, ...) |

Web apps, tools and scripts on TypeScript and Bun are the default stack, so a
Next.js server or a static site is not a departure. Paths under
`node_modules`, `vendor`, `target`, `dist`, `build` and `.next` are skipped.

## The rule at a pull request

The check reads git objects at the base and the head; it needs no second
checkout.

- A departure in the head that the base's baseline does not list fails.
- A baseline line the base did not have fails: a recorded departure is only
  ever removed. Recording a new one is a company decision, landed by the
  repository's owner as its own change with the departure.
- A baseline line whose departure is gone fails until the line is deleted, so
  the baseline stays true and keeps shrinking.
- A base with no baseline file (the adopting change) accepts the head's file
  as the seed.

## Adopting

1. Seed the baseline from the default branch:
   `python3 stack_conformance.py scan > .github/stack-departures.txt`
   (run from the repository root, with the script from this repository). A
   repository with no departure needs no file.
2. Add the `stack` lane and job from
   [`workflow-templates/optimistic-gate.yml`](../workflow-templates/optimistic-gate.yml)
   to the repository's gate, pinned by commit, and add `stack` to `ci-ok`'s
   `needs`. The lane runs when `sylphx.toml`, a `package.json`, a Dockerfile or
   the baseline changes.

## The portfolio number

`stack_conformance.py all DIR...` scans each checkout at `HEAD` and prints one
line per repository (count and departures) and the share with no departure at
all: the stack part of "Products fully on our own stack" in SylphxAI/owner
`company/standards-map.md`. It runs on demand over fresh clones; there is no
schedule. A sparse clone is enough:

```sh
git clone --depth 1 --filter=blob:none --no-checkout <url> <dir>
git -C <dir> sparse-checkout set --no-cone '**/sylphx.toml' 'sylphx.toml' \
  '**/package.json' 'package.json' '**/Dockerfile*' 'Dockerfile*'
git -C <dir> checkout
```

## Limits

A backend with no framework dependency and no Dockerfile named in
`sylphx.toml` (for example a server started by a build strategy other than
`dockerfile`) is not found. Widen the rules here, with a test, when one is
seen; every caller gains the rule on its next pin move.
