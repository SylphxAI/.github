# SylphxAI/.github

Organization-level GitHub configuration: community health files, the profile,
templates, brand references, reusable workflows, and shared actions used by
more than one repository. [PROJECT.md](PROJECT.md) states the boundary.
Company standards are in `SylphxAI/owner` `standards/`.

- A change to a shared action or workflow reaches every caller that pins it, so
  callers pin by commit SHA and move deliberately.
- Keep secrets, tokens, and `.env` files out of the repository.
- Test with `python -m pytest`, narrowest target first.
