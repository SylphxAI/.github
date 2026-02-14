# New Project Checklist

When setting up a new project in the SylphxAI org:

## 1. Repository

- [ ] Create repo under SylphxAI org
- [ ] Add `CLAUDE.md` with project-specific context
- [ ] Add `biome.json` config
- [ ] Add `.github/workflows/ci.yml` (see [CI/CD](ci-cd.md))
- [ ] Set up `bun` as package manager (`bun init` or `bunx create-next-app`)

## 2. Branches

- [ ] Push initial code to `main`
- [ ] Create `dev` branch from `main`
- [ ] Set up branch protection on both (see [Branch Strategy](branch-strategy.md)):
  - Require PR, 1 approval, `build` check
  - Block force push and deletion
- [ ] Set default branch to `dev` (optional — keeps PRs targeting dev by default)

## 3. Vercel

- [ ] Import project to Vercel (Sylphx team)
- [ ] Connect GitHub repo
- [ ] Set Production branch to `main`
- [ ] Set environment variables:
  - **Production**: real credentials, production DATABASE_URL
  - **Preview**: dev credentials, dev DATABASE_URL
- [ ] For monorepos: set `rootDirectory` and `ignoreCommand`

## 4. Neon Database

- [ ] Create Neon project (Sylphx org)
- [ ] Note the `main` branch connection string → Vercel Production
- [ ] Create `dev` branch (copy-on-write from main)
- [ ] Note the `dev` branch connection string → Vercel Preview

## 5. Schema & Migrations

- [ ] Define Drizzle schema in `src/db/schema/`
- [ ] Add `atlas.hcl` config
- [ ] Generate initial migration: `bunx atlas migrate diff init`
- [ ] Apply to dev database: `bunx atlas migrate apply`

## 6. Domain (when ready)

- [ ] Add custom domain in Vercel
- [ ] Configure DNS (Cloudflare or registrar)
- [ ] Update `NEXT_PUBLIC_APP_URL` env var

## 7. Monitoring (optional)

- [ ] Add Sentry for error tracking
- [ ] Add PostHog for analytics
