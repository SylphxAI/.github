# Stack conformance

The economy cache rule concerns player balances, grants, rewards, purchases and
entitlements. Infrastructure caches, credential permissions and queue leases do
not become player economy authority because a function contains `claim` or
`grant`. Reviewed repository domains live in the checker's immutable
`cache-economy-profiles.json`: Spiron has no player economy; Cloud is checked in
Money and entitlement paths. Other rules still scan those repositories. The
domain policy is included in the checker source fingerprint, and existing cache
approval and shrinking-baseline checks still apply inside the economy domain.

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

## Model versions in product source

Product code uses the organization's runtime default (omit the model id), or a
Sylphx AI alias of the form `<provider>/<line>-latest`, such as
`deepseek/deepseek-flash-latest`. The catalog and runtime decide whether that
alias exists and which dated revision it resolves to. CI checks this naming
contract offline; a live catalog request cannot change a pull request's result.

The policy follows cloud's
[model identity, naming and price binding ADR](https://github.com/SylphxAI/cloud/blob/2eeaf2605f12ec2aec98b837870eccff5f66c0b0/docs/adr/ADR-01M58DAMBRRTBRYRD3HSQQVD1S-model-identity-naming-and-price-binding.md),
including the dated-provider-locator amendment in
[cloud#15331](https://github.com/SylphxAI/cloud/pull/15331). The cited source
commit is `2eeaf2605f12ec2aec98b837870eccff5f66c0b0`. Sylphx owns alias movement;
an upstream floating name is not the authority for the resolved revision.

Both dated ids (`gpt-5.1-2025-11-13`, `claude-opus-4-5-20251101`) and undated
version ids (`deepseek/deepseek-v4.1-flash`, `gpt-5.1`, `claude-opus-4-5`)
fail. The diagnostic tells the author to omit the id or use a catalog alias.
Recognition covers literal model names in Rust, Python, Go, JS/TS, C/C++, C#,
Java, Kotlin, Swift, Dart, Vue/Svelte, shell,
JSON, TOML, YAML, SQL and Protobuf. Variables, method names, comments,
documentation, tests (including inline Rust tests), fixtures and dependencies
are excluded. Computed or encoded ids still require semantic review.

Catalog and routing source in cloud owns upstream locators. The reasoned,
repository-and-path exemptions are declared in `model_pins.py`; every entry
has an exemption test and a test that the same path in a product still fails.
Other cloud source is checked normally. No repository-wide exemption is given
to cloud.

Existing hits use the normal baseline file with a rule-specific adoption marker:

```text
# model-alias-ratchet-v1
model-pin src/client.ts gpt-5.1-2025-11-13 1
```

A record is one path, model id and occurrence number; paths use URL escaping
for spaces. Moving a line within a file preserves its record, while copying
the same id creates a new occurrence that fails. First adoption can record
only hits independently scanned at the base commit. Thereafter the marker
cannot be removed, records may only shrink, and stale records must be deleted.
This category does not reseed or loosen existing stack or architecture debt.
`scan --repository ORG/REPO` prints these records with the other categories.
When checking without a base, the committed baseline is used, as for the
original stack rules; pull requests must supply their actual base.

For staged adoption, change the shared-action SHA and baseline in the same PR
using the [offline adoption helper](#adopting-new-rules-with-a-pin-bump);
older checkers do not understand model-pin records. Keep `# main` after the SHA
so Renovate can discover subsequent shared-action commits. A workflow planner
must run stack conformance for source/config changes, not only manifests.

An optional `model-aliases` action input (`--model-aliases FILE` in the CLI)
can tighten alias admission to an offline catalog declaration. The JSON shape
is `version: 1`, `aliases: ["provider/line-latest", ...]`, and
`source: {repository: "SylphxAI/cloud", commit: "<40-hex>", path: "...",
sha256: "<64-hex>"}`. This mode requires exact declared membership as well as
the naming contract and fails closed on an unreadable or invalid file. Refresh
the declaration from the catalog with its immutable provenance; no hand-kept
vendor-name list is used to authorize aliases. Until such an export is
published, the default naming-contract check is sufficient and has no catalog
availability dependency.

## Branch history in repository automation

Automation creates a fresh branch, pushes normally, and opens a pull request
through the repository's normal merge queue. The required `stack` lane refuses
literal branch pushes using `--force`, `-f` (including combined short options),
`--force-with-lease`, `--force-if-includes`, `+` refspecs or `--mirror`, and
GitHub branch-ref PATCH calls with `force=true`. The red-main handler creates
candidate and revert branches with a run/attempt suffix; a rejected create or
push preserves the existing remote branch and stops that attempt.

The recognizer reads shell scripts and libraries, workflow/action `run` blocks
(including nested workflows and root `action.yml`), package scripts, and
literal process calls in executable scripts. Literal shell helpers that forward
arguments to Git are followed without sourcing repository code. Commands in
comments, documentation and data strings are not executed commands. Computed
executables, arguments and refspecs remain a semantic review responsibility;
a passing static check is not a semantic census.

Explicit tag-only pushes are outside this branch-history rule.
Product VCS library methods offered to users are outside repository-automation scope.
Archived and delivered customer repositories are outside rollout scope.

Inherited occurrences use the same caller baseline, with an independent marker:

```text
# force-push-ratchet-v1
# stack-rule-adopted-v1 force-push
force-push scripts/publish.sh <command-sha256> 1
```

Use the [adoption helper](#adopting-new-rules-with-a-pin-bump) in the same PR as
the checker pin bump. Only exact base-existing, head-surviving occurrences may
seed a newly introduced rule. Each record binds the URL-escaped path, literal
command digest and occurrence number. Commands and remote URLs are omitted
from diagnostics because they may contain credentials. Moving a line preserves
its identity; copying or changing the command creates new debt and fails.
After adoption, markers cannot be removed, records cannot grow, and stale
records must be deleted. Neither this rule nor its baseline loosens another
stack category. `scan` prints records for inspection; its output alone does
not authorize adoption. A run without a base checks the committed ratchet
against itself. Unreadable automation inputs fail the check.

The [2026-10-30 audit snapshot](../policy/force-push-audit.json) records 12 sites
across 7 repositories, each with its observed default SHA and exact baseline
record: cloud (4), sylphx-clients (2), this shared repository (2), mcp-registry
(1), repomap (1), hk-mahjong-tycoon-sx (1), and mahjong-xgmj-java-sx (1).
The two mirror workflows retain their named owning repositories and are
baseline-only in this rollout. The shared red-main sites are repaired here;
other inherited records can only shrink through their owning repository's PRs.
This inventory is evidence, not an additional allowance file.

The paginated inventory covered 339 repositories: 190 archived, AnyAgents and
the delivered BGCA repository were excluded before source reads. All 147
eligible active snapshots were processed (7 empty). The bounded selector read
131,097 text files; 82 oversized or missing/truncated file reads across 25
repositories remain explicitly listed in the snapshot. None is under
`.github/`. Binary assets, dependency locks, vendored code and submodule
contents are not an absence claim. Snapshot findings do not assert that
moving default branches remain unchanged.

## Production database aggregate declarations

A production database declared by `sylphx.toml` needs a companion
`ops/tenant-read-functions.json`, relative to that manifest. The check recognizes
`[resources.database]`, resource-shaped `[database]`, and their
`[environments.production]` overlays. A preview-only resource or a
`[database.migrations]` table alone does not declare a production database.

The file is the existing database resource PATCH body:

```json
{
  "kind": "database",
  "deskReportFunctions": [
    {"name": "activity_summary", "args": "timestamp with time zone"}
  ]
}
```

The platform's [tenant reader declaration
contract](https://github.com/SylphxAI/cloud/blob/main/docs/runbooks/shared-tenant-read-only.md#declaration)
requires one to 64 distinct exact signatures, lowercase names and plain argument
types. `args` defaults to an empty string. Unknown fields, duplicate JSON keys,
duplicate signatures and unreadable or malformed declarations fail explicitly.
The source contract was read on 2026-10-30 from
`crates/sylphx-apps-controller/src/managed_resource/shared_read_only.rs`.

Missing files produce `C2/tenant-read-functions` findings naming the exact path
and the supported `PATCH /v1/resources/{id}` declaration. They fail even on an
unrelated change unless explicitly adopted through the existing helper under
`architecture/C2/tenant-read-functions`. The baseline identifies each
manifest's production database, so unrelated manifest edits preserve its debt.
Missing or unsupported well-formed declarations, including old `functions`
signature lists, are coverage gaps. Existing gaps can be seeded only from the immutable base during adoption; the
set then only shrinks. Adding the file requires removing its stale record;
removing the declaration after that fails and cannot re-add the old debt.
An architecture decision cannot waive this declaration requirement.

This offline check establishes source declaration shape only. It neither
applies the resource PATCH nor proves deployed functions, credentials or grants.
The scheduled owner catalog observation supplies that separate live verdict.
It does not infer databases provisioned outside the manifest or dynamically
generated manifests. Delivered customer repositories retain the action's
existing exclusion.

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

## Cache reads in economy handlers

The authority check (`C1/cache-economy`) flags a purchase, claim, grant, buy
or redeem function that reads a cache. It also recognizes the Java recharge
callback form. The contract is the [game economy
rule](https://github.com/SylphxAI/skills/blob/main/skills/self-challenge/references/games.md#hard-lines):
display caches never decide grants, and settlement honors the saved paid order
even when a display template expires.

| Language | Recognized source forms |
| --- | --- |
| Java | Named methods such as `buyLimitedTimeDiscountGiftsReq` and `rechargeCallBackReq`; `*TemplateCache.getCache().getTemplate(...)`, cache receivers and variables whose declared type contains `Cache` |
| TypeScript / JavaScript | Named functions, ordinary methods, assigned arrow functions and literal `post`/`put`/`patch` route callbacks; `offerCache.get(...)`, cache calls, cache fields and typed cache receivers |
| Rust | Named `fn` / `async fn`, including generic handlers; `offer_cache.get(...)`, `Cache::get(...)`, cache fields and receivers declared with a cache type |

Comments, strings, test bodies, invalidation-only calls, cache writes and
`fetch({cache: 'no-store'})` are excluded. Cache-key identifiers/builders and
simple cache-factory accessors are not cached-value reads. Assignment targets
are writes; equality comparisons and compound assignments still read values.
Typed permission-grant handlers and claims with a `CacheKey` parameter and
a typed `lease_*` parameter are excluded when their signature and body have no
economic terms or grant call. Economic names, types or operations keep those
handlers in scope. Recognition is structural; remaining ambiguous candidates
need classification, not a claim that an exploit exists. Indirect helpers,
renamed imports, computed routes and arbitrary data flow still need review.
Java is supported by this cache rule; it does not gain the other C1–C7 rules.
Legacy Java comment bytes are preserved when they are not UTF-8.

An allowed display read carries a reason and a named test on the read's line:

```text
const label = offerCache.get(id); // cache-display-only: response label only; test=tests/shop.test.ts#stale_price_reloads_authority
```

The test reference is a repository-relative path and a named Java `@Test`,
Rust `#[test]` / `#[tokio::test]`, or JS/TS `test` / `it` case with an executable
assertion. Inline Rust tests can be referenced in their source file. Missing
tests, comments posing as tests, empty reasons and deleted assertions do not
authorize the read. A central architecture decision cannot replace this
inline evidence. The test runner and review establish that the case changes
the authoritative row behind a stale cache and proves the granting transaction
re-reads it; the static check establishes the reference and assertion exist.
Deleting that test invalidates the exception even if its handler is unchanged.

Existing candidates use the same shrinking architecture baseline and offline
adoption helper below, with rule marker
`# stack-rule-adopted-v1 architecture/C1/cache-economy`. Seed only findings
present at the caller's base while moving its shared-action SHA. Duplicating a
read, including on the same line, creates another occurrence and fails; removing
a read requires deleting its stale baseline record. No repository-wide cache
allowance or separate baseline format is introduced.

First adoption of this rule also checks `cache-safe-reads.json` in the shared
action. Its reviewed display reads are bound to the repository, path, exact
expression hash and occurrence, with a reason. Unrelated lines may change;
changing or duplicating the read does not inherit its approval. Both the
offline adoption helper and the required check reject unapproved inherited
reads, including a pin-only bump that omits the adoption marker. Fix or delete
those reads before adopting. Existing CI on its current pin keeps its current
behavior. After adoption, the normal baseline may only shrink; this policy
does not permit growth, new reads or a repository-wide exception.

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
| `keel-pack-source <path>:<line> <stage>` | a Docker stage runs a web pack without importing `ARG SYLPHX_GIT_COMMIT_SHA` before its packing `RUN`; add that declaration after the stage's `FROM` |

The platform supplies the exact source commit as a Docker build argument. A
global declaration before `FROM` does not expose it inside a stage. A stage
may declare the argument itself or inherit it from a prior stage. The check
follows tracked wrapper scripts and literal pack argument arrays, and the
repin repair uses the same detector. Unrelated build/runtime stages, native
profiles and pack inspection commands need no web source argument. Script
generation and arbitrary computed commands remain outside static detection.

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
schedule. Fetch the Git objects in bulk without materializing a working tree:

```sh
git clone --depth 1 --no-checkout <url> <dir>
```

The checker never lazily fetches individual source blobs. With a `blob:none`
partial clone, it inventories missing objects for each compared tree, then
fetches missing source, configuration and policy candidates together from the
configured promisor remote. This also covers historical base blobs that an
ordinary depth-one checkout never materialized. Sparse checkouts and renamed
remotes use the same path; unrelated binary assets may remain absent. A failed
or timed-out bulk fetch fails the check and names the affected revision rather
than silently skipping its source. Complete clones retain their existing
missing-object behavior without additional Git processes.

The check prints each scanning phase, announces each bulk fetch, bounds that
fetch to 60 seconds, and reports Git-read progress at least every five seconds
while it walks blobs.

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
checked, including `bundled_skills_generated.rs`. Protocol/FlatBuffers compiler
output with its exact generated header is
listed as excluded; authored schemas remain checked, and a generated filename
alone grants no exemption. It does not resolve arbitrary
macros, YAML templates/aliases, cross-module setting precedence or general
program behavior. C2 does not prove database ownership from arbitrary SQL, and
C6 does not establish that arbitrary code has no callers. Source review retains
those responsibilities.

### Existing debt uses the same baseline

Architecture records identify the exact violation span and occurrence:

```text
# architecture-ratchet-v2
# stack-rule-adopted-v1 architecture/C5/polling-loop
architecture C5 polling-loop src/jobs.rs VIOLATION_SPAN_SHA256 1
```

Only findings independently present at the base, for rules absent from every
old checker pin, can be adopted. After adoption each rule's recorded set may
only shrink; its marker remains even when the last record is removed. A stale
record fails until removed. Paths are URL encoded. The span hash survives
unrelated workflow pin edits and line movement; edited violation spans and
additional identical occurrences fail. The whole-file `source_sha256` remains
separate and still binds exception decisions to exact file evidence. Legacy
v1 architecture baselines fail closed and require an explicit reviewed
migration; the helper does not silently convert them. Existing Rust/Atlas
ratchets and central ownership policies retain their behavior.

### Adopting new rules with a pin bump

Use an immutable checkout of the newly published shared release. Its
`.github/actions/stack-conformance/adoption.py` helper reads local git objects,
never executes an old checker, and never accesses the network. It discovers
actual `stack-conformance` pins from the fresh caller base, including multiple
different old pins. Unrelated shared actions' pins do not determine checker
capabilities. Its bundled `checker-capabilities.json` stores reviewed source
hashes and enforced rule identities, with commit mappings for offline lookup.
An unlisted commit is recognized only when its immutable local source exactly
matches a reviewed profile. Missing source or an unreviewed profile refuses
adoption. A rule already enforced by any old checker cannot receive new
baseline records.

Stage the caller's full-SHA pin updates, each with `# main`, and any required
workflow fixes. Keep its baseline unchanged. Then use a staged tree OID so the
entire update can be published in one commit:

```sh
base=$(git rev-parse origin/main)
git add .github
candidate=$(git write-tree)
python "$shared/.github/actions/stack-conformance/adoption.py" adopt \
  --repo "$caller" --base "$base" --head "$candidate" \
  --shared-repo "$shared" --repository ORG/REPO \
  --output "$caller/.github/stack-departures.txt" \
  --evidence /tmp/adoption-evidence.json
git add .github/stack-departures.txt
git commit -m 'Update shared CI pins and adopt newly introduced rules'
python "$shared/.github/actions/stack-conformance/stack_conformance.py" check \
  --repo "$caller" --base "$base" --head HEAD --repository ORG/REPO
```

Run the git commands in the caller checkout. `$shared` may be an immutable
worktree whose local git object store contains the old pinned commits. The
helper verifies those old sources against the bundled registry and appends
only base-existing, head-surviving findings for newly introduced rules. The
original baseline bytes remain an exact prefix; existing records are never
reseeded. New head debt, duplicate baseline lines, modified candidate baseline
text, unreadable source, and unknown checker provenance refuse adoption.
The evidence JSON is a local review artifact containing immutable base/head
identities, old pins/source hashes, introduced rules and added records.

The Python API is
`plan_adoption(base_tree, head_tree, repository, *, shared_repo, today, baseline=...)`.
It returns `baseline_text`, `introduced_rules` and `evidence`, or raises
`ValueError`. It does not write either input tree. The CLI writes output only
after planning succeeds. A run without `--base` uses the committed baseline
as its ratchet, including architecture records.

Ordinary Renovate pin-only updates and shrinking baselines do not consult the
capability registry, so a later merged checker SHA need not be registered for
routine updates. Before shipping a new rule, refresh the reviewed profiles for
old checker sources that need adoption. Commits with identical reviewed source
do not need separate mappings when their local Git objects are available.
`adoption.py register --shared-repo PATH --rev SHA` emits a profile for review;
include its content and an optional commit mapping in the bundled registry.
The required CLI uses the same immutable fingerprint resolution: its explicit
`--shared-source` checkout when supplied, otherwise the public GitHub source
reader already used for pin provenance. It verifies complete trees and Git blob
identities and never executes historical code. Unavailable source refuses the
adoption request. Unknown profiles fail only an adoption request. Keep
rule identities stable; an intentional new rule gets a new identity and its
own adoption marker. No custom updater or broker is involved.

For an initial caller, the base has no checker pin and the staged candidate must
add an immutable checker caller. The same helper adopts only findings already
present in that exact base, with one marker per rule; it refuses new findings
and unapproved cache reads. Existing markers still prevent reseeding. The
required CLI validates this initial adoption through the same rule permissions.
When the base has no baseline file, the helper also seeds exact inherited stack
departures; a new departure in the candidate is refused.

### Project portability

`architecture/C1/project-portability` refuses new cluster-dependent destinations
in application and runtime source: literal or template `.svc`,
`.svc.cluster.local`, `.cluster.local` and `.sylphx.internal` hosts, single-label
HTTP service URLs, Kubernetes service-account mount paths, Kubernetes endpoint
environment reads and explicit in-cluster client constructors. A configurable
public URL passes; an optional private default is still recorded source debt.
Comments, ordinary fixture paths, inline Rust tests, bare refusal suffixes and
diagnostic examples are excluded. Native client languages and runtime shell
entrypoints use the same rule.

Deployment manifests may name infrastructure. The allowlist applies to YAML,
TOML, JSON and XML under deployment/infra/Helm/GitOps/CI directories, plus
`sylphx.toml` and `*.deployment.profile.toml`. Source code in those directories
is still checked. There is no blanket platform-repository exemption.

Use the existing rule-scoped `adoption.py adopt` pin-bump route above to record
only base-existing, head-surviving findings in `.github/stack-departures.txt`.
Every inherited portability occurrence needs its exact baseline record, even
on an unrelated PR. After adoption the records can only shrink; copying,
editing or resurrecting a removed occurrence fails. Architecture decisions
cannot exempt this rule. The capability profile binds the executable detector
module, preserving historical profile identities when that module is absent.

The existing required stack verdict carries this rule. A caller qualification
must include one actual failing private-destination change and a clean passing
change, and demonstrate that failure reaches its required aggregate. This
lexical check does not certify computed hosts, API publicness, key permissions,
or all Kubernetes aliases; these remain census and owning-writer review work.

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
Polling-loop recognition uses explicit infinite loop forms or a retry counter
that never advances. Trait implementations, collection iteration, finite
counted loops and conditional event loops are not inferred to be unbounded.
Self-rearming state-read timers without an exit are polling too; guarded timers
and animation callbacks are separate paired negatives.

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
covers SylphxAI, Cubeage, Cubeage-limited, EpiowAI and OzyrixLtd
(653 repository IDs, 214 unarchived at the 2026-10-29 read). TseFamily is a
personal organization and is excluded from company censuses, baselines and rollouts. Numeric IDs retain identity across canonical
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
