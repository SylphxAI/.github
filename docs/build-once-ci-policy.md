# Build once, deploy the artifact

**SSOT for Sylphx-owned product repos that deploy on Sylphx Platform.**

## Rule

| Plane | Owns | Must not do |
| --- | --- | --- |
| **Platform** | Sole production packaging (`source_sha` → immutable `artifact_digest`) | Rebuild between environments |
| **Repo CI** | Verification only (lint, typecheck, unit, contracts) | Discarded production packaging |

Production packaging includes:

- `next build` / `bun run build` (app production)
- `cargo build --release` for ship binaries
- `docker build` / `docker buildx` for deploy images **not** published as the ship artifact

Publish workflows that **are** the distribution authority (npm native packages, CLI release binaries) remain packaging paths for those products — they must still publish the exact artifact they built (not rebuild later).

## Required customer CI shape

```text
lint / typecheck / unit / pure contracts
  → required check green
Platform build (parallel under After Verification)
  → digest D
deploy D (never rebuild)
```

## Forbidden residual

- Application build job that is not consumed as a deploy artifact
- Verify prebuild of `gateway-server --release` thrown away after tests
- CI `docker build` “validation” when Platform builds the same Dockerfile for deploy

## Auto Deploy

Prefer production **After Verification**: wait for repo CI green, deploy the **Platform** artifact for that exact SHA.
