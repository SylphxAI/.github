# CI/CD

## GitHub Actions

Every repo has a CI workflow at `.github/workflows/ci.yml`:

```yaml
name: CI
on:
  push:
    branches: [main, dev]
  pull_request:
    branches: [main, dev]

concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: oven-sh/setup-bun@v2
      - run: bun install --frozen-lockfile
      - run: bun run lint
      - run: bun run build
      - run: bun test
```

### Key Points
- Triggers on `main` and `dev` (push + PR)
- Concurrency group cancels stale runs
- Job name must be `build` (branch protection checks for this)
- Uses Bun, not npm
- Lint is blocking (must pass)

## Vercel Deployment

| Trigger | Target | Domain |
|---------|--------|--------|
| Push to `main` | Production | Custom domain |
| Push to `dev` | Preview | `*-git-dev-*.vercel.app` |
| PR opened | Preview | `*-git-branch-*.vercel.app` |

Vercel deploys automatically via GitHub integration. No manual deploy needed.

## Atlas Migrations

Database schema changes use Atlas (not drizzle-kit):

```bash
# Create a new migration
bunx atlas migrate diff <name> \
  --to file://drizzle/schema.ts \
  --dev-url "postgres://..." \
  --format golang-migrate

# Apply migrations
bunx atlas migrate apply \
  --url "$DATABASE_URL" \
  --dir file://migrations
```

Atlas config lives in `atlas.hcl` at repo root.

### CI Migration Check
CI validates that migrations are up-to-date with the schema. If you change the Drizzle schema, you must generate a corresponding Atlas migration.
