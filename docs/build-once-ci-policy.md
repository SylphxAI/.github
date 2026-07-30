# Agent-native Fast Trunk CI/CD (fleet policy)

**Canonical ADR (Platform):**  
[ADR-01KYTNAGENTFASTTRUNK01 — Agent-native Fast Trunk CI/CD](https://github.com/SylphxAI/platform/blob/main/docs/adr/ADR-01KYTNAGENTFASTTRUNK01-agent-native-fast-trunk-cicd.md)

**Auto Deploy product modes:**  
[docs/features/auto-deploy.md](https://github.com/SylphxAI/platform/blob/main/docs/features/auto-deploy.md)  
(`On Commit` / `After Verification` / `Off`)

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

Prefer native concurrency:

```yaml
concurrency:
  group: ci-${{ github.workflow }}-${{
    github.event.pull_request.number || github.ref
  }}
  cancel-in-progress: true
```

Merge Queue: **off by default**.

## Platform

- One production build per SHA → OCI digest  
- Artifact smoke on that digest  
- Promote same digest across environments  
- Never rebuild for “safety” between staging and production  

## Template

See [workflow-templates/verification-only-ci.yml](../workflow-templates/verification-only-ci.yml).

## Allowed exceptions

- **npm / CLI / native binary publish** workflows that **are** the ship path for that product (build once and publish that artifact).  
- **Unity / Firebase / non-Platform** ship pipelines — those systems remain packaging authority for those channels.
