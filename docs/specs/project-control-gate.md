# Project-Control Gate Spec

## Goal

Give `SylphxAI/.github` a lightweight, non-mutating repository-local validation
gate for organization-control changes.

## Scope

The gate validates this repository only:

- vendor-neutral project identity and truth homes live in
  `project.manifest.json`;
- Sylphx-specific governance facts remain in `.doctrine/project.json`;
- generated `.groundatlas*` outputs are evidence/navigation only;
- shared workflow/action contracts remain owned by their ADRs and specs.

The gate does not own consumer repository manifests, branch protection,
deployments, releases, runtime behavior, or production proof.

## Workflow Contract

`.github/workflows/groundatlas.yml` runs on:

- `pull_request`;
- `push` to `main`;
- `merge_group`.

It must:

1. check out the repository;
2. set up Node.js 22.14.0;
3. run `node --check` against
   `.github/actions/setup-changesets-publisher/changesets-publish.mjs`;
4. run `node --test tests/groundatlas-boundary.test.mjs`;
5. run `SylphxAI/groundatlas@v0.1.2` with `package-spec:
   groundatlas@0.1.2`, `require-atlas: "true"`, and `strict: "true"`;
6. assert that GroundAtlas selects `project.manifest.json` and treats
   `.doctrine/project.json` only as an adapter;
7. upload the manifest and fleet reports as `groundatlas-package-dogfood`.

## Acceptance

- `ga audit` passes after `ga update`.
- `ga manifest --json` selects `project.manifest.json`.
- `ga fleet --require-atlas --strict --json` reports one adopted project with
  zero warnings and zero blockers.
- The Changesets publisher syntax check passes.
- The GroundAtlas boundary test passes.
- No reusable workflow/action public input, output, permission, or behavior is
  changed by the GroundAtlas gate.
