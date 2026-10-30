# Require actual test execution

The [`setup-ci-test` action](../.github/actions/setup-ci-test/action.yml) installs
`ci-test` on `GITHUB_PATH`. The guard runs one command, streams its combined
output, and requires fresh evidence that at least one test passed or failed.
It uses Python 3.10+ and the standard library, with no runtime downloads.
Pin the action to the commit you adopt, as with every shared action.

```yaml
- uses: SylphxAI/.github/.github/actions/setup-ci-test@<40-character-commit>
  id: test-guard
- run: ci-test --format libtest --shard rust/unit -- cargo test --locked --workspace
- run: |
    ci-test --format junit --report result.xml --shard python/unit -- \
      python -m pytest --junitxml=result.xml tests
- run: |
    ci-test --format vitest --report result.json --shard package/unit -- \
      npx vitest run --reporter=default --reporter=json --outputFile=result.json
```

The `script` action output is the absolute path of the installed standalone
guard. For example, pass it to a later step through
`env: {CI_TEST_SCRIPT: '${{ steps.test-guard.outputs.script }}'}` and invoke
`python3 "$CI_TEST_SCRIPT" ...`. The installed copy is independent of the
action checkout. The action needs no token or additional permissions.

The CLI is:

```text
ci-test --format libtest|junit|vitest --shard exact-id
        [--report PATH] [--exceptions JSONFILE] -- COMMAND ARGS...
```

`--report` is required for JUnit and Vitest and unavailable for libtest.
The guard deletes that path before starting the runner. The runner must write
a new report to the same path, resolved from the current working directory.
Use a separate report path for concurrent invocations. Commands run as an
argument vector, without a shell. Give each test invocation or matrix shard
its own identifier using letters, numbers, `.`, `_`, `:`, `/`, and `-`.

## Evidence and exit statuses

| Format | Accepted execution evidence | Excluded from execution |
| --- | --- | --- |
| `libtest` | Stable pretty/terse `running N tests` paired with a complete, anchored terminal summary; counts `passed + failed` | Ignored, measured, filtered, discovery listings |
| `junit` | Actual named `testcase` nodes under a `testsuite`/`testsuites` root, including nested and namespaced suites | Cases with a `skipped` child |
| `vitest` | Actual `testResults[].assertionResults[]` with status `passed` or `failed` | `skipped`, `pending`, `todo`, `disabled` |

Every announced libtest target must finish and its summary must match its
announced count. Several targets in one Cargo invocation may include empty
targets, provided their combined executed count is positive. Custom harnesses,
JSON output, and `--list` are unsupported by the libtest adapter; use a
structured reporter when the harness does not emit libtest summaries.

JUnit summary attributes never establish execution. When present, `tests`,
`skipped`, `failures`, and `errors` must agree with the actual testcase nodes.
Vitest's `numTotalTests` is deliberately ignored because it includes tests
that did not run. A complete Vitest report includes its boolean `success`,
suite verdicts and assertion arrays.

Every nonzero runner exit passes through unchanged, including pytest's
no-tests exit **5**. Signals use the shell convention `128 + signal`.
After a zero exit, the guard returns **1** for missing, malformed,
contradictory or incomplete evidence, any reported failures, or zero executed
tests. Invalid CLI arguments return **2**. A command that cannot be found
returns **127**; one that is not executable returns **126**.

Reports and runner output are evidence supplied by the runner, not a security
boundary against a malicious test command that forges its own results.

## Intentional empty shards

An intentional empty invocation needs a reviewed JSON file containing an
exact format, shard and argument vector, plus a nonblank reason:

```json
[
  {
    "format": "libtest",
    "shard": "rust/windows-only",
    "argv": ["cargo", "test", "--locked", "windows_only"],
    "reason": "This target has no Linux tests; Windows coverage runs in its own job."
  }
]
```

Pass it as `--exceptions .github/empty-test-shards.json`. Matching uses literal
equality, with no globs, regexes, command prefixes or blanket bypass flags.
All four fields are required and unknown fields are rejected. The guard logs
the reason when it accepts a valid empty report. An exception never accepts
missing or malformed evidence, report failures, or a nonzero runner exit.
Keep skipped/quarantined tests covered by a separate lane; declaring one
invocation empty does not execute those tests.

The package starter supplies `npm run test:ci` for this action; `npm test` runs
Vitest once locally and `npm run test:watch` retains interactive watching.

## Verification and primary sources

`tests/test_ci_test.py` covers adversarial reports, stale files, exit status
preservation and an installed-action consumer. `tests/test_ci_test_runners.py`
runs real Rust libtest, pytest JUnit and Vitest reporters. Project control
installs all three runners and requires these tests to run, using the action's
installed `script` output.

Format decisions were checked against upstream primary sources on 2026-10-30:

- [Rust test output documentation](https://doc.rust-lang.org/book/ch11-02-running-tests.html)
  and [libtest pretty formatter](https://github.com/rust-lang/rust/blob/master/library/test/src/formatters/pretty.rs),
  `write_run_start` and `write_run_finish`.
- [pytest JUnit reporter](https://github.com/pytest-dev/pytest/blob/main/src/_pytest/junitxml.py),
  testcase children and session summary counts.
- [Vitest reporter documentation](https://vitest.dev/guide/reporters.html#json-reporter)
  and [JSON reporter](https://github.com/vitest-dev/vitest/blob/main/packages/vitest/src/node/reporters/json.ts),
  assertion statuses and summary totals.
