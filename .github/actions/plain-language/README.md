# Plain language

Annotates a pull request or merge group that adds a coined term the company has
replaced with standard vocabulary. The check is advisory, because no list of
coined words is ever complete; the rule and review do the rest. The rule and its reasons are in owner
`standards/docs.md`, "Plain language". [`terms.tsv`](terms.tsv) is the list the
check enforces.

```yaml
jobs:
  plain-language:
    runs-on: sylphx-linux-standard
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

A platform repository (cloud, hands, infra) also runs the product-name list,
because platform code never names a product (owner
`standards/architecture.md`):

```yaml
      - uses: SylphxAI/.github/.github/actions/plain-language@<sha>
        with:
          list: product-names
          exclude: |
            **/tests/**
            docs/incidents/**
```
