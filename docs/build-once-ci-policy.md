# Agent-native Fast Trunk CI/CD (fleet policy)

**Canonical ADR (Platform):**  
[ADR-01KYTNAGENTFASTTRUNK01 — Agent-native Fast Trunk CI/CD](https://github.com/SylphxAI/platform/blob/main/docs/adr/ADR-01KYTNAGENTFASTTRUNK01-agent-native-fast-trunk-cicd.md)

## One sentence

Internal agents land small batches on **direct-trunk**. Repository CI verifies **source correctness only**. Platform builds the **production artifact once**. CI and build run **in parallel**. Deploy promotes the **same immutable digest**.

## Authority

| Scope | Authority |
| --- | --- |
| Work / claim / progress | Enact |
| Source history | Git / forge |
| Source correctness | Repository CI |
| Production packaging | **Sylphx Platform only** |
| Deploy / health / rollback | Sylphx Platform |

## Repository CI

**Do:** lint, typecheck, affected unit/integration (test-profile), schema/proto/migration, contracts.  
**Do not:** disposable production packaging (`next`/`bun` production app build, `cargo build --release` for ship binaries, validation `docker build` thrown away).

Aggregate deploy-ready check example: `source-ci/pass`.

## Concurrency (mandatory fleet standard)

**Do not** use `github.sha` in concurrency groups.  
**Do not** add a `free-runners` (or any API cancel-superseded-main) job.

```yaml
concurrency:
  group: ci-${{ github.event.pull_request.number || github.ref }}
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
- Promote same digest across environments  

## Template

See [workflow-templates/verification-only-ci.yml](../workflow-templates/verification-only-ci.yml).

## Allowed exceptions

- **npm / CLI / native binary publish** workflows that are the ship path for that product.  
- **Unity / Firebase / non-Platform** ship pipelines.
