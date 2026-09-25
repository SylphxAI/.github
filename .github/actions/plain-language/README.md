# Plain language

Fails a pull request or merge group that adds a coined term the company has
replaced with standard vocabulary. The rule and its reasons are in owner
`standards/docs.md`, "Plain language". [`terms.tsv`](terms.tsv) is the list the
check enforces.

```yaml
jobs:
  plain-language:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@<sha>
        with:
          fetch-depth: 0
      - uses: SylphxAI/.github/.github/actions/plain-language@<sha>
```

Only added lines are checked, so a repository adopts it without first cleaning
its history. Mark a line that must keep a term, such as a quotation or a
standard sense, with `plain-language: allow`.
