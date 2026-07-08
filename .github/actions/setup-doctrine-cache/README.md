# setup-doctrine-cache

Sparse checkout of `SylphxAI/doctrine` for CI audit gates. Uses a
least-privilege GitHub App installation token — not `GITHUB_TOKEN` cross-repo
tarballs and not vendored doctrine copies in consumer repos.

## Required org configuration

1. GitHub App with `contents: read` on `SylphxAI/doctrine`.
2. Org or repo secrets passed into the action:
   - `app-id` + `app-private-key`, or
   - `app-client-id` + `app-private-key` (create-github-app-token v3).

Recommended org secret names: `SYLPHX_CI_GITHUB_APP_ID` (or
`SYLPHX_CI_GITHUB_APP_CLIENT_ID`) and `SYLPHX_CI_GITHUB_APP_PRIVATE_KEY`.

## Usage

```yaml
- uses: SylphxAI/.github/.github/actions/setup-doctrine-cache@main
  with:
    ref: dc59cbbaa15fb3c9d5d4e411e44c3168a913c758
    app-client-id: ${{ vars.SYLPHX_CI_GITHUB_APP_CLIENT_ID }}
    app-private-key: ${{ secrets.SYLPHX_CI_GITHUB_APP_PRIVATE_KEY }}

- name: Doctrine gates
  env:
    DOCTRINE_ROOT: ${{ github.workspace }}/.doctrine-cache
  run: bun run check:doctrine-project-control
```

Pin `ref` to a doctrine SHA when audit semantics change; bump via PR.