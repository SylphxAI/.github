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

## Agent-runtime parts

Sylphx Agents is the one owner of agent definitions, session logs, turn loops,
agent memory, the tool gateway, credential injection and the egress guard
(SylphxAI/cloud
[ADR-01M495TCCBZGSQ68A4P4G428H2](https://github.com/SylphxAI/cloud/blob/main/docs/adr/ADR-01M495TCCBZGSQ68A4P4G428H2-sylphx-agents-hosted-agent-runtime.md),
D5). The same check fails a product repository that holds one of these parts:

| Line | Found when |
| --- | --- |
| `agent-runtime-package <manifest> <name>` | a `Cargo.toml` `[package]` or `package.json` name contains `vault-proxy`, `egress-guard`, `credential-crypto` or `agent-harness` (`_` reads as `-`) |
| `agent-runtime-table <file.sql> <table>` | a `.sql` file creates `session_entries`, `session_turns`, `tool_calls`, `memory_entries` or an `agent_memory*` table, and no later `.sql` file (in path order, the order of timestamped migrations) drops it |

These lines are never recorded in a repository's own baseline. The only
allowance is [policy/agent-runtime.json](../policy/agent-runtime.json) here:
one entry per existing instance, each with an expiry date. Until that date the
check passes; after it, the entry stops applying and the repository's next
change that touches a manifest or SQL file fails until the part has moved onto
Sylphx Agents and been deleted. An entry whose part is gone is reported as a
notice; delete it. Extending a date is a pull request here with its reason.

`owner_repos` (SylphxAI/cloud, where `services/agents` lives) is not checked
for these parts. A delivered customer repository (organization custom property
`sylphx_delivery` = `delivered`) is not checked at all; the action reads the
property with the job token, and an unreadable property leaves the check on.

The baseline was seeded on 2026-10-06 from `scan` over the default branch of
every repository with a desk checkout (136 repositories): only SylphxAI/agents
has instances. Its expiry, 2026-11-30, leaves room after the agent app's switch
and cleanup items for their estimate to slip.

## Work-engine tables

A company's own work (what its people and agents owe) runs on Work; a
product's customers' records (tickets, approvals, cases) stay in the product
(SylphxAI/work
[ADR 0010](https://github.com/SylphxAI/work/blob/main/docs/adr/0010-group-companies-on-work.md),
D1 and "Guard for the class"). With a base, the check reads every `.sql` file
the change adds (not a rename, not a `*.down.sql`) and fails on:

| Line | Found when |
| --- | --- |
| `work-obligation-table <file.sql> <table>` | a `CREATE TABLE` has a status column (`status`, `state`, `stage`, or a name with one of them as a word), an assignee column (`assignee*`, `assigned`, `role`, `owner`, `executor`, `executing`, `handler`, `responsible`) and a due column (`due`, `deadline*`, `sla`, `overdue`, `escalat*`), and the comment lines before the file's first statement say neither whose records they are |

The header is one comment line before the first statement:

```sql
-- Work resource: items (workspace ozyrix, kind operate)
```

when the rows are the company's own obligations and the migration feeds
Work, or

```sql
-- customer records (Work ADR 0010 D1)
```

when they are the product's customers' records. The header makes the D1 test
a decision the reviewer sees. Tables already on the base are never checked,
so adopting needs no baseline; there is no allowance list.

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
   `needs`. The lane runs when `sylphx.toml`, a `package.json`, a
   `Cargo.toml`, a Dockerfile, a `.sql` file or the baseline changes.

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
