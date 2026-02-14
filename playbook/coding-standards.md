# Coding Standards

## Stack

| Layer | Technology |
|-------|-----------|
| Framework | Next.js (App Router) |
| API | Hono (RPC) |
| Validation | Zod |
| ORM | Drizzle |
| Database | Neon (Postgres) |
| Hosting | Vercel |
| CSS | Tailwind CSS v4 |
| Components | Base UI (headless) |
| Auth | Better Auth |
| Payments | Stripe |
| Email | Resend |
| AI | AI SDK (Vercel) |
| Package Manager | Bun |
| Lint/Format | Biome |
| Testing | Bun test |
| Migrations | Atlas |

## Package Manager

**Bun only.** Never use npm, npx, yarn, or pnpm.

```bash
bun install          # install deps
bun run build        # build
bun run lint         # lint
bun test             # test
bunx <package>       # run CLI tools
```

## Linting & Formatting

**Biome only.** Never use ESLint + Prettier.

```bash
bun run lint         # check
bun run lint:fix     # auto-fix
```

Biome config lives in `biome.json` at repo root. It handles both linting and formatting in one tool.

## Code Conventions

### TypeScript
- Strict mode always
- No `any` — use `unknown` + type narrowing
- Prefer `type` over `interface` (unless extending)
- Use `satisfies` for type checking without widening

### Hono API
- Split route files by entity (e.g., `users.ts`, `posts.ts`)
- Chain routes in a single call — separate `.route()` calls break type inference
- Use Zod validators inline

```typescript
// ✅ Good — chained
const app = new Hono()
  .get("/users", async (c) => { ... })
  .post("/users", zValidator("json", schema), async (c) => { ... })

// ❌ Bad — separate route() calls break RPC types
app.route("/users", userRoutes)
app.route("/posts", postRoutes)
```

### Drizzle ORM
- Define schema in `src/db/schema/`
- Write migration SQL directly — do NOT use `drizzle-kit generate`
- Use Atlas for migration management (see [CI/CD](ci-cd.md))

### Imports
- Use path aliases (`@/` for `src/`)
- Biome handles import sorting automatically
- No barrel files (`index.ts` re-exports) — import directly

### Testing
- Test behavior, not implementation
- Unit → Integration → E2E (priority order)
- Use `bun test` (Vitest-compatible API)
- Co-locate test files: `foo.ts` → `foo.test.ts`

### i18n
- Use `next-intl`
- Split translation files by feature/page
- Never put all translations in one large file

### Comments
- Comments explain **why**, not **what**
- No TODO comments — finish the work or file an issue
- No dead/commented-out code — delete it
