# Agent-native Fast Trunk CI/CD (fleet policy)

**Canonical ADR (Platform):**  
[ADR-01KYTNE0B96325DDAD28FD1341 — Agent-native Fast Trunk CI/CD](https://github.com/SylphxAI/platform/blob/main/docs/adr/ADR-01KYTNE0B96325DDAD28FD1341-agent-native-fast-trunk-cicd.md)

Do not invent alternate ADR filenames (e.g. `ADR-01KYTNAGENTFASTTRUNK01`).

## One sentence

Internal agents land small batches on **direct-trunk**. Repository CI verifies **source correctness only**. Platform builds the **production artifact once**. CI and build run **in parallel**. Deploy promotes the **same immutable digest**.

## Authority

| Scope | Authority |
| --- | --- |
| Work / claim / review / progress | Enact |
| Source history / commit / PR | Git / forge |
| Source correctness | Repository CI |
| Production packaging | **Sylphx Platform only** |
| Deploy / health / rollback | Sylphx Platform |
| Production correctness | Live runtime evidence |

## Repository CI

**Do:** lint, typecheck, affected unit/integration (test-profile), schema/proto/migration, contracts, narrow security.

**Do not:** disposable production packaging (`next`/`bun` production app build, `cargo build --release` for ship binaries, validation `docker build` thrown away).

Aggregate source verdict: `source-ci/pass`.

## When CI runs

| Lane | Scope |
| --- | --- |
| Local / pre-commit | fast affected checks only (p95 &lt; 2 min) |
| Internal `main` push | formal source CI once + Platform build once (parallel) |
| External PR | presubmit source feedback only |
| Scheduled / postsubmit | full matrix, fuzz, deep security, perf, broad E2E — non-blocking |

## Concurrency (mandatory fleet standard)

**Do not** use `github.sha` in concurrency groups.  
**Do not** add a `free-runners` (or any API cancel-superseded-main) job.

```yaml
concurrency:
  group: ci-${{ github.workflow }}-${{
    github.event.pull_request.number || github.ref
  }}
  cancel-in-progress: true
```

| Event | Cancels |
| --- | --- |
| New commit on same PR | Previous PR CI for that PR |
| New push to `main` | Previous `main` CI for that workflow |
| Unrelated PR | Nothing (different group) |

`github.ref` on `push` is `refs/heads/main`, so tip supersession is native—no custom free-runners controller.

Merge Queue: **off by default**. Do **not** declare `merge_group:` on ordinary CI workflows unless MQ is intentionally enabled for that repository.

## Platform

- One production build per SHA → OCI digest  
- Artifact smoke on that digest  
- Promote same digest across environments (build once, deploy many)  
- After Verification: CI success ∧ build ready ∧ artifact smoke → deploy that digest  

## Agent occupancy

After source lands: checkpoint exact SHA → `work.defer` for CI/build/deploy → release claim/Run. Do not babysit delivery in-session.

## Template

See [workflow-templates/verification-only-ci.yml](../workflow-templates/verification-only-ci.yml).

## Allowed exceptions

- **npm / CLI / native binary publish** workflows that are the ship path for that product.  
- **Unity / Firebase / non-Platform** ship pipelines.  
- **Monorepo test dist** builds required only to run tests (not production packaging).  
