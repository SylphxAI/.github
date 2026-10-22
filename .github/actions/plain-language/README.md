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
          filter: blob:none
      - uses: SylphxAI/.github/.github/actions/plain-language@<sha>
```

Only added lines are checked, so a repository adopts it without first cleaning
its history. Mark a line that must keep a term, such as a quotation or a
standard sense, with `plain-language: allow(<reason>; until=YYYY-MM-DD)`. A bare `allow`, a marker
without a reason, or a past date fails; a bare `allow` is tolerated with a warning until 2026-10-15.

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

## Ratchet baseline (product-names)

The action keeps no state, so each platform repository commits its own baseline,
`.plain-language-baseline.tsv` (`path<TAB>name<TAB>count`), and passes it:

```yaml
      - uses: SylphxAI/.github/.github/actions/plain-language@<sha>
        with:
          list: product-names
          baseline: .plain-language-baseline.tsv
          exclude: |
            docs/incidents/**
```

With `baseline` set the check scans every tracked file, not only added lines. A
hit above its baseline count (including any new file) fails; a baselined hit
that is gone also fails until it leaves the baseline, so the file only shrinks.
Generate it once with `PLAIN_LANGUAGE_WRITE_BASELINE=init`, later shrink it with
`=shrink` (refuses if anything grew):

```sh
PLAIN_LANGUAGE_BASELINE=.plain-language-baseline.tsv PLAIN_LANGUAGE_WRITE_BASELINE=shrink \
  .github/actions/plain-language/check.sh HEAD .github/actions/plain-language/product-names.tsv <exclude pathspecs> \
  > /tmp/b && mv /tmp/b .plain-language-baseline.tsv
```

Names match as tokens: `_`, `-`, `.` and spaces end a name, letters and digits do not
(`KALKAS_NAMESPACE` hits, `spironic` does not).
