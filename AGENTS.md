# .github — local agent notes only

Static organization standards live in `SylphxAI/skills` and the installed host
constitution. Live work, claims, runs, and effects belong to Sylphx Enact when
its authenticated runtime is available. This file must **not** restate, weaken,
or fork those authorities (including PR-vs-direct-trunk delivery).

Local truth: `PROJECT.md` and `project.manifest.json`.

## Boundary hazards

- Never commit secrets, tokens, `.env` files, or credentials.

## Local commands

- `python -m pytest` (narrowest target first)

## Validation notes

- Prefer the **narrowest** affected check before full workspace runs.
- Report layers honestly: local diff · trunk FF · deploy · prod proof (do not collapse).
