# zh-Hant characters

Fails a pull request or merge group that adds a Simplified-only character to
Traditional Chinese content. The rule is in owner `standards/experience.md`: a
Traditional locale has no Simplified characters. [`simplified.tsv`](simplified.tsv)
is the character list the check enforces, with the Traditional characters it
suggests; it is generated from OpenCC (Apache-2.0) and lists only characters
that are not themselves Traditional, so shared characters such as 台, 里, 后 and
干 never match.

```yaml
jobs:
  zh-hant:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@<sha>
        with:
          fetch-depth: 0
      - uses: SylphxAI/.github/.github/actions/zh-hant@<sha>
```

What counts as zh-Hant content, on added lines only:

- every line of a file whose path names a Traditional locale: `zh_TW`, `zh-HK`,
  `zh-Hant`, `zhTW`, `values-zh-rTW`, `zh-Hant.lproj`;
- in any other file, the value after a `zh_tw`, `zh-HK`, `zh_hant` or similar
  key, up to a Simplified-locale key (`zh_cn`, `zh-Hans`) on the same line.

Mark a line that must keep a Simplified character, such as a quotation, with
`zh-hant: allow`. Mainland vocabulary in Traditional characters (the other half
of the rule) is left to review.
