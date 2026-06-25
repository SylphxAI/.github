# `secret-scan` — `security:secrets` gate (ADR-6)

Org SSOT for the blocking secret-detection gate required by
[ADR-6 §2 / Amendment 2026-06-17](https://github.com/SylphxAI/doctrine).
Replaces per-repo copies of the gitleaks lane (the pattern originated inline in
`platform`'s Security Scan lane; this is the centralized, reusable form).

## What it does

- Installs the MIT-licensed `gitleaks` binary (no `GITLEAKS_LICENSE`).
- Scans **only the commits this push / PR / merge group adds** — a pre-existing
  historical leak is a separate rotation track and must not make the gate red on
  arrival; any **newly introduced** secret fails the job.
- `--redact`s findings so secrets never reach logs.

A verified-live reporter (TruffleHog `--only-verified`) and a dependency-vuln
gate (`security:vuln-policy`) are separate lanes; this action is the blocking
`security:secrets` context only.

## Adoption (per repo `ci.yml`)

Add one job; its `name:` becomes the required status context:

```yaml
  security-secrets:
    name: security:secrets
    runs-on: [self-hosted, sylphx, linux, standard]
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0          # required: full history for the commit range
      - uses: SylphxAI/.github/.github/actions/secret-scan@main
```

Then add `security:secrets` to the repo's required checks (org ruleset / repo
ruleset), per ADR-6 §4.

## Inputs

| input | default | purpose |
|-------|---------|---------|
| `gitleaks-version` | `8.30.1` | gitleaks release to install |
| `config-path` | `""` | optional `.gitleaks.toml`; empty = gitleaks defaults |

## Proven false positives

Per ADR-6, a secret finding fails the gate unless proven a false positive via a
signed exception. Use a committed `.gitleaksignore` (fingerprints) or a repo
`config-path` allowlist for that case — never disable the gate.
