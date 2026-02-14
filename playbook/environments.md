# Environments

## Overview

| Environment | Branch | URL | Database |
|------------|--------|-----|----------|
| Production | `main` | Custom domain | Neon `main` branch |
| Preview | `dev` / PRs | `*.vercel.app` | Neon `dev` branch |

## Vercel

- **Production**: Auto-deploys on push to `main`
- **Preview**: Auto-deploys on push to `dev` and on PR creation
- Each Vercel project has separate env vars for Production and Preview targets

### Environment Variables

- **Production** env vars → only apply to `main` deployments
- **Preview** env vars → apply to `dev` and PR deployments
- **Never share production database credentials with preview** — use Neon dev branch

### Monorepo (SaaS)

For Turborepo monorepos with multiple Vercel projects:
- Each app has its own Vercel project with `rootDirectory` set
- Use `npx turbo-ignore --fallback=HEAD~1` as `ignoreCommand` to skip unchanged apps

## Neon (Database)

Each project has a Neon project with:
- `main` branch — production database
- `dev` branch — development database (copy-on-write from main)

### Setting Up Neon Dev Branch

```bash
# Create dev branch from main
curl -X POST "https://console.neon.tech/api/v2/projects/{project_id}/branches" \
  -H "Authorization: Bearer $NEON_API_KEY" \
  -d '{"branch": {"name": "dev", "parent_id": "{main_branch_id}"}}'
```

The dev branch is a copy-on-write clone — reads from main until written to, near-zero storage cost.

## Vercel ↔ Neon Connection

| Vercel Target | Neon Branch | DATABASE_URL |
|--------------|-------------|--------------|
| Production | `main` | Production connection string |
| Preview | `dev` | Dev branch connection string |

Set `DATABASE_URL` separately for Production and Preview targets in Vercel dashboard.
