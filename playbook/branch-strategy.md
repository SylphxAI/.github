# Branch Strategy

## Branches

| Branch | Purpose | Deploys to | Protected |
|--------|---------|-----------|-----------|
| `main` | Production | Vercel Production | ✅ |
| `dev` | Development | Vercel Preview | ✅ |
| `feature/*`, `fix/*` | Work branches | Vercel Preview (PR) | ❌ |

## Rules

1. **Never commit directly to `main` or `dev`** — always use PRs
2. **All PRs target `dev`** — never target `main` directly
3. **`main` is updated by merging `dev` → `main`** (promotion)
4. **Squash merge** for all PRs into `dev`
5. **Regular merge** (or merge commit) for `dev` → `main` promotions

## Branch Protection (both `main` and `dev`)

- Require pull request before merging
- Require 1 approval
- Require `build` status check to pass
- Block force pushes
- Block branch deletion

## Workflow

```
feature/my-feature ──PR──→ dev ──promote──→ main
fix/bug-123 ──PR──→ dev ──promote──→ main
```

### Day-to-day
1. Create branch from `dev`: `git checkout -b feature/my-feature dev`
2. Make changes, commit, push
3. Open PR targeting `dev`
4. CI runs, review happens
5. Squash merge into `dev`

### Promoting to production
1. Ensure `dev` is stable and tested
2. Open PR: `dev` → `main`
3. Review and merge (regular merge, not squash)
4. Vercel auto-deploys production

## Branch Naming

- `feature/<description>` — new features
- `fix/<description>` — bug fixes
- `fix/<issue-number>-<description>` — fixes linked to issues
- `chore/<description>` — maintenance, deps, config
