# Standards lint

Fails a pull request, merge group or push that adds a sleep or a polling loop
as the normal way work progresses. The primary path is an event: a watch or
informer, a transactional outbox to an event bus, a webhook, or Postgres
`LISTEN/NOTIFY`. A slow periodic resync triggers the same reconciliation and
only repairs missed events (owner `standards/architecture.md`, "Real time:
events first, resync as the safety net"). [`rules.tsv`](rules.tsv) is the list
this action enforces; a repository does not copy it.

```yaml
jobs:
  standards-lint:
    runs-on: sylphx-linux-standard
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@<sha>
        with:
          fetch-depth: 0
      - uses: SylphxAI/.github/.github/actions/standards-lint@<sha>
```

Only added lines are checked, so a repository adopts it without first
rewriting the loops it already ships. Pin the action by commit SHA and move
the pin deliberately; the rules travel with the pin, so no repository keeps
its own copy of the law.

## What fails

| Added line | Finding |
| --- | --- |
| `tokio::time::sleep(`, `time.sleep(`, `sleep 5`, `setTimeout(`, `setInterval(` | `primary-path.sleep` or `primary-path.timer` |
| a `while` or `for` loop whose body re-reads state (`fetch`, `get`, `query`, `poll`, `probe`, `status`) | `primary-path.poll` |
| a Kubernetes CronJob `schedule:` of five cron fields | `primary-path.cron` |

A finding names the rule (`standards-lint/<code>` in the annotation title) and
the file and line, and points at the standard.

## What passes

An event path has nothing to flag: a `watch`, an informer, `LISTEN`/`NOTIFY`,
a webhook handler, or a subscription.

A line that is genuinely a bounded resync, a recovery, or time-based work
carries the exception and says why:

```rust
// standards-lint: allow(bounded resync: repairs a missed notify, 300s)
tokio::time::sleep(RESYNC_INTERVAL).await;
```

```python
time.sleep(300)  # standards-lint: allow(recovery: reconnect backoff after a dropped watch)
```

The reason is not free text the check ignores blindly: it has to be there, in
parentheses, on the same line. A bare `standards-lint: allow` still fails, so
an exception always names what it is. A schedule that is the work itself (a
daily report, an expiry) uses the same marker.

Set `mode: warn` to annotate without failing, and `exclude` to skip pathspecs
such as recorded evidence. Tests are checked like any other file: a fixture
that adds a polling loop is a new primary path and fails.
