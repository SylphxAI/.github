# Stack conformance

`.github/actions/stack-conformance` fails a change that adds a departure from
the default stack in SylphxAI/skills `skills/self-challenge/references/stack.md`: backend services in
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
every repository with a desk checkout (136 repositories): only SylphxAI/spiron
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

## Knowledge and CRM tables

The company's knowledge graph, its memory spaces and its CRM customer graph
are Sylphx Knowledge, a platform service with one permission space per record
(SylphxAI/cloud
[ADR-01M4AJK2TXTJWZ4X5B06HPT0R4](https://github.com/SylphxAI/cloud/blob/main/docs/adr/ADR-01M4AJK2TXTJWZ4X5B06HPT0R4-sylphx-knowledge-permission-aware-graph.md),
D6). A product does not hold one of these tables in its own repository. With a
base, the check reads every `.sql` file the change adds (not a rename, not a
`*.down.sql`) and fails on:

| Line | Found when |
| --- | --- |
| `knowledge-table <file.sql> <table>` | a `CREATE TABLE` names a knowledge-graph or CRM record: a table whose name starts `kg_`, `knowledge_` or `crm_`, or is `entities`, `entity`, `relations`, `relation`, `contacts`, `contact` or `customer_profiles` |

Tables the base already has are the existing instances of the widened rule.
They are recorded in [policy/knowledge-tables.json](../policy/knowledge-tables.json),
one entry per table with the `found` date and an expiry date; until the expiry
a repository with a recorded table passes, and after it the entry stops
applying and its next change that touches a SQL file fails until the table has
moved onto Sylphx Knowledge and been deleted. An entry whose table is gone is
a notice; delete it. Extending a date is a pull request here with its reason.

The baseline was seeded on 2026-10-07 from a scan of the default branch of
every repository with a desk checkout: only SylphxAI/spiron has instances
(`knowledge_entries`, `knowledge_relations` and the `bots_graph.relations`
projection). Its expiry, 2026-11-30, leaves room after the knowledge migration
items for their estimate to slip.

The fixture that proves the rule is a SQL file added by a change that creates
one of these tables; it fails with the `knowledge-table` line and the ADR, and
passes again once the table is recorded for that repository or the file makes
no such table.

`owner_repos` (SylphxAI/cloud, where the service lives) is not checked for
these tables. A delivered customer repository (`sylphx_delivery` = `delivered`)
is not checked at all, as for the agent-runtime parts.

## Keel game boundaries

A Cargo workspace that depends on `keel`, a `keel-*` crate or `cubeage-kit`
is a game consumer. The shared engine/commerce owner workspaces themselves
are excluded by their package declarations, not by a product-name list.
The same stack check rejects:

| Line | Found when |
| --- | --- |
| `keel-local-sku <path>:<line> <struct>` | game source declares `struct *Sku` rather than using `keel_commerce`, `keel::commerce::iap` or `cubeage_kit` commerce types |
| `keel-cpu-pixels <path>:<line> <function>` | game source declares `fn *_pixels(...) -> Vec<u8>` rather than using Keel shaders or shared primitives |
| `keel-boilerplate <path>` | the game contains `docs/BUILDING.md` or `scripts/measure-rust-debug.py`; the build/debug defaults belong in shared CI |

Comments and string literals are not source declarations. Importing an engine
commerce type does not excuse a second local SKU struct. These rules cannot
be seeded or waived in a game's own departures baseline; no boilerplate-file
exceptions are approved. Any exception needs a change to this shared check.
The stack lane runs for every change; it includes source, boilerplate and
manifests as well as documentation and configuration.

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
   `needs`. Require `stack` to succeed through `needs-pass`; the lane runs
   for every change, including documentation-only changes.

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

## Architecture classes C1–C7

The existing action also checks structural regressions against the seven-class
[architecture contract](https://github.com/SylphxAI/owner/blob/main/company/architecture-audit.md).
The governing references are SylphxAI/skills
[`architecture.md`](https://github.com/SylphxAI/skills/blob/main/skills/self-challenge/references/architecture.md)
and [`stack.md`](https://github.com/SylphxAI/skills/blob/main/skills/self-challenge/references/stack.md),
plus the company's `AGENTS.md`.

| Class | Supported seed | Clean control |
| --- | --- | --- |
| C1 | An explicit first-party or network-trust branch immediately returns authorization success | The branch uses the ordinary grant check; consumer names in comments and documentation |
| C2 | Direct provider constructors or provider-key reads in a product | An ordinary platform call; a provider adapter in the capability's owning platform |
| C3 | A shared engine imports downstream behavior, compiles a downstream identity, or bundles company Skills | Consumer-supplied generic configuration; isolated examples and test modules |
| C4 | Literal environment reads lack declarations; unconditional Python defaults conflict | One declared setting and override precedence |
| C5 | Unbounded read/sleep loops, state-polling intervals, request-driven idle floors or unnamed progress cron | Events, finite transport retries, UI animation/debounce, frame loops, named calendar obligations |
| C6 | Unexplained lint suppressions or executable routes declared retired by a completed migration | A specific suppression reason; a route whose migration remains incomplete |
| C7 | Failure-only CI verdicts or merge-only completion branches | Canceled dependencies fail the existing `needs-pass` verdict; completion carries environment evidence |

Run the same public entry point locally:

```sh
python3 stack_conformance.py check --base BASE --head HEAD \
  --repository ORG/REPO --report /tmp/stack-conformance-report.json
```

The JSON report gives repository, immutable source and checker commits, class,
file and line, reason, violated contract, exact source-file hash, decisions,
inherited findings, reviewed/excluded paths, unreadable input and known limits.
It contains no source excerpts or credential values. The action's `report`
output names this file in the runner's temporary directory. A finding marked
`inherited` remains open debt; an exception is reported as an exception, never
as fixed. A passing static check does not certify a semantic source audit.

Each changed file is examined with its surrounding control flow. Editing a
loop header therefore cannot hide an unchanged polling body. Changes to the
environment declaration or architecture decision/migration files recheck the
consumers. Relevant unreadable blobs, invalid Python, malformed configuration
and stale decisions fail explicitly. Other languages and unrecognized
constructs are reported limits, not a claim of zero violations.

The action requires Python 3.11 or newer. It supports Rust, Python,
JavaScript/TypeScript, Go, shell, TOML and ordinary YAML blocks. It excludes documentation, dependency/build output, test/fixture
paths and inline Rust `#[cfg(test)] mod` bodies. Generated production source is
checked, including `bundled_skills_generated.rs`. It does not resolve arbitrary
macros, YAML templates/aliases, cross-module setting precedence or general
program behavior. C2 does not prove database ownership from arbitrary SQL, and
C6 does not establish that arbitrary code has no callers. Source review retains
those responsibilities.

### Existing debt uses the same baseline

`scan --repository ORG/REPO` also emits exact records of the form:

```text
# architecture-ratchet-v1
architecture C5 polling-loop src/jobs.rs:12 SOURCE_FILE_SHA256
```

On first adoption, only findings independently present at the base can be
recorded. After adoption these records may only shrink; the marker remains
even when the last record is removed. A stale record fails until removed.
Baseline records expose existing debt and never suppress a changed violation
or replace a decision. The existing Rust/Atlas baseline and central ownership
policies retain their behavior.

### Recorded decisions

`.github/architecture-decisions.json` has `version: 1` and a `decisions` array.
Every entry names `repository`, `path`, `line`, `class`, `rule`, `source_sha256`,
`owner`, `reason`, `contract`, `expires`, `authority`, `evidence` and
`reconsideration`. The hash covers the exact file, so changed context cannot
silently inherit an old decision. Contract, authority and evidence are HTTPS
links; the reason must be specific. Wildcards, duplicate entries, expired
entries, wrong repositories and unmatched/stale records fail. A decision
explains why a static candidate is justified; it cannot authorize a backdoor,
unauthorized spend or loss of user data.

TOML `min_instances` and Knative `min-scale`/`minScale` above zero need such
a recorded always-on decision. TOML `[env.NAME]` and `[env] NAME = ...`
declare environment settings. HPA `minReplicas` and KEDA `minReplicaCount` above zero are checked
when `conformance.request-driven: "true"` or an HTTP trigger identifies the
workload as request-driven. Unclassified autoscalers remain a stated limit.
A CronJob that is the work itself names its contract in
`conformance.calendar-obligation: "https://...#daily-report"` (or its expiry or
safety-resync contract). A cron frequency alone is not a calendar obligation.
Finite transport retry is recognized from finite ranges or an attempts bound
with progress; a bare `allow` comment does not suppress polling.

A completed migration can declare `{"completed": true, "retired_routes":
["/old"]}` in `.github/architecture-migrations.json`. This is an explicit
migration contract, not a route-deletion instruction inferred from a match.

### Required CI and rollout evidence

The optimistic gate template runs `stack` for every change, including
configuration-only and documentation-only changes, and requires its successful
result through the existing `needs-pass` action. This repository uses that same
required chain. Adoption in another repository means moving its shared pin and
required workflow together. A merged action or template is not portfolio
rollout: each included repository still needs a seeded red run and a clean
green run at its actual required verdict. Delivered customer repositories stay
excluded. Refresh the repository population across all declared organizations,
including Cubeage-limited and the declared product organizations, before rollout.

The former standalone proposal in PR #283 is consolidated here: its progress
polling rules and paired negatives are part of stack-conformance. This delivery
does not introduce a second `standards-lint` action or a second baseline.

### Pinned repository identities

C3 derives product identities from the dated architecture-map product rows,
resolved by numeric repository ID in `policy/architecture-repositories.json`;
the checker contains no hand-maintained product list. Engine, service and tool
repositories do not become downstream products merely because of their org. The dated paginated census
covers SylphxAI, Cubeage, Cubeage-limited, EpiowAI, OzyrixLtd and TseFamily
(657 repository IDs, 218 unarchived at the 2026-10-29 read). Numeric IDs retain identity across canonical
names. Archived rows remain visible as history and do not seed import rules.
The policy declares which layers are shared, including Cubeage's platform,
kit and tycoon engine; Cubeage layers use their own title population.

Refresh from a complete metadata snapshot with
`scripts/build_architecture_catalog.py --snapshot SNAPSHOT.json --products PRODUCTS.json`; its input
records each organization's query, page sizes, read time and repository count.
It rejects missing organizations, count mismatches and duplicate identities.
The generated file ships with the action's immutable pin. It is a bounded
identity vocabulary, not an assertion that every repository name is a product
or that every import has been semantically classified. New aliases appear on
the next catalog refresh; a name-only rule cannot infer an arbitrary renamed
crate or a downstream relationship hidden behind a generic interface.

C2 includes the witnessed provider-host forms (Stripe, Nexmo, Apple receipt
verification, Android Publisher, FCM, fal.ai and Replicate). An owning service
path permits its own capability's adapter; placing a Stripe call in the Auth
service does not turn it into a Money adapter. Provider literals in comments,
documentation and fixtures pass. These are regression candidates for migration,
not authorization to remove live-user continuity or the existing payment path.
