# Agent-first triage taxonomy

This document defines the minimum cross-repo GitHub triage contract for SylphxAI agent-first work. It is intentionally small: repo-specific area labels stay in each repo, while shared labels make org-level Signal/Triage scans reliable.

## Scope and guardrails

- Applies to issues and pull requests across SylphxAI repositories.
- Does not replace project-specific `PROJECT_BOUNDARY`, `AGENT_GUIDE`, `AGENTS`, `README`, ADR, or spec files.
- Agents must read repo boundary/source-of-truth docs before writing labels, comments, branches, commits, or PRs.
- Agents must not push directly to default/protected branches, force-push, change destructive settings, expose secrets, merge, or deploy unless explicitly authorized and gates are complete.

## Shared label groups

### State

| Label | Meaning |
| --- | --- |
| `state/blocked` | Work cannot proceed without a named dependency, failing gate, or explicit external decision. |
| `state/stale` | No meaningful update after the repo's stale threshold; requires revalidation before action. |
| `state/needs-triage` | Missing enough metadata to route owner/type/priority safely. |
| `state/ready-review` | Implementation is ready for human/agent review, subject to CI status. |

### Type

Use existing GitHub defaults where present: `bug`, `documentation`, `enhancement`, `duplicate`, `question`, `wontfix`, `invalid`.

Add only when useful and absent in a repo:

| Label | Meaning |
| --- | --- |
| `type/refactor` | Behavior-preserving internal cleanup or structure change. |
| `type/ci` | CI, workflow, test harness, or gate behavior. |
| `type/security` | Security-relevant fix, review, hardening, or vulnerability response. |

### Priority / risk

| Label | Meaning |
| --- | --- |
| `priority/p0` | User-/production-blocking or security-critical. |
| `priority/p1` | High-impact active work or queue unblocker. |
| `priority/p2` | Normal planned work. |
| `risk/high` | Security, data, billing, production, deployment, or irreversible behavior risk. |

### Agent workflow

| Label | Meaning |
| --- | --- |
| `agent/discovered` | Agent found the item during scan; needs human or owner validation before broad action. |
| `agent/proposed-action` | Agent proposed a concrete issue/comment/label/PR action backed by evidence. |
| `agent/in-progress` | Agent is actively working within an assigned role and repo boundary. |

## Repo-specific area labels

Keep domain labels in the owning repo, for example:

- `platform`: `auth`, `billing`, `infra`, `sdk`, `console`, `preview`, `storage`, `dx`.
- `spiron`: `channel`, `memory`, `runtime`, `agent-computer`, `scheduler`, `security`, `ci`.
- `filesystem-mcp`: `dependencies`, `github_actions`, `javascript`.
- `tryit`: existing product labels such as `feature`, `optimization`, `refactor`, `pipeline/pr-open`.

Do not force a global area taxonomy onto repos with mature local labels.

## Triage comment format

When a comment is needed, keep it short and evidence-backed:

```md
Signal / triage note (YYYY-MM-DD): <one-line classification>.

Observed:
- <specific PR/issue/check/status evidence>

Suggested next routing:
- <concrete action or owner/gate to unblock>

No merge/deploy action taken by Signal/Triage.
```

## Rollout plan

1. Add missing shared labels gradually by repo, preferably through scripted, reviewable changes where supported.
2. Do not mass-edit old issues until each repo's boundary and local taxonomy are checked.
3. Start with active/core queues: `platform`, `spiron`, `sylphx-ai`, `filesystem-mcp`, `alpha-foundry`, `doctrine`, `.github`, `gateway`, and repos with high open PR counts.
4. Treat queue-level CI failures as blockers before dependency-bump cleanup.
