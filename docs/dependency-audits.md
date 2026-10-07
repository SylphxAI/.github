# Dependency advisories do not gate unrelated pull requests

A package-manager audit reads a mutable advisory database, not just the commit.
An advisory published between two runs can therefore fail unchanged code while
the last check of `main` stays green. Re-running or upgrading an unrelated pull
request is not a repair of that branch.

## Decision and reference

Keep `bun audit`, `npm audit`, `pnpm audit` and `yarn audit` out of pull-request,
merge-group and reusable gate workflows. The existing `workflow-lint` action's
non-deterministic-gate check enforces this alongside the timing-gate rule; it
retains the delivered-customer exemption. Use a separate post-merge workflow,
not an event condition inside a workflow that also runs before merge.
Secret detection remains a commit-based gate and is not affected.

For launched products, use the existing dependency-update owner's advisory
path: [Renovate vulnerabilityAlerts](https://docs.renovatebot.com/configuration-options/#vulnerabilityalerts)
or [Dependabot security updates](https://docs.github.com/en/code-security/dependabot/dependabot-security-updates/about-dependabot-security-updates).
These open the dependency repair, rather than sending each unrelated author to
repair the same trunk dependency. Renovate's GitHub vulnerability alerts require
GitHub Dependabot alerts and the bot's access to them; do not assume that an
ordinary package-update schedule enables advisory updates.

Company law defers security audits and hardening for unlaunched products to the
launch gate (`SylphxAI/owner`, `standards/security.md`, and the accepted risk for
unlaunched products). Do not add a daily security job during development.

## Launched-product ownership

At launch, the repository owner:

1. Enables the existing update bot's advisory updates, through its supported
   repository settings/configuration. Do not add a second bot.
2. Adds a separate default-branch audit workflow with `push: branches: [main]`,
   a daily `schedule` and `workflow_dispatch`, never `pull_request`,
   `merge_group` or `workflow_call`. Scheduled GitHub workflows use the default
   branch; checkout that branch explicitly on manual runs too.
3. Uses the committed lockfile, pinned package manager, production dependencies
   and the launch gate's chosen severity threshold. A Bun example is
   `bun audit --audit-level=high --prod`. The audit must fail on findings and
   registry errors, with a bounded job timeout; do not use `continue-on-error`.
4. Assigns a failing audit to the repository owner and links the bot's repair.
   A scheduled failure is an advisory-maintenance signal, not proof that a new
   source commit caused a runtime regression. Do not feed it to source-culprit
   automatic rollback or add it to the pre-merge `ci-ok` aggregation.
5. Runs on self-hosted runners, or standard GitHub-hosted runners only where the
   repository is public and the job guards that condition. GitHub spend is $0.

## Adoption, rollback and proof

Existing callers pin `workflow-lint` by SHA. Remove the pre-merge audit when
adopting the updated action; keep required job names and secret scanning intact.
No schema, account, dependency-version or production change is needed. Revert
these workflow/pin changes to roll back; do not rewrite lockfiles or data.

The guard's tests cover Bun, npm, pnpm and Yarn audits across every pre-merge
trigger, mixed trigger workflows, the allowed separate scheduled workflow,
comments/step names, unchanged secret detection and delivered-customer skips.
A consumer invocation at the new action SHA proves adoption; the launch gate
separately proves scheduled failure ownership and bot repair creation.

A remaining limitation is indirect commands (for example `bun run security`
whose script invokes an audit). The workflow lint scan is static and does not
execute package scripts or recursively inspect third-party actions. Review
those scripts when adopting the policy; it is not a general shell interpreter.
