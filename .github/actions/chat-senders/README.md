# Chat senders

Fails a pull request, merge group or push that adds a direct chat-API host
outside [`allow-list.tsv`](allow-list.tsv): `api.telegram.org`,
`hooks.slack.com`, `slack.com/api/chat.postMessage`, `discord.com/api/webhooks`
or `discordapp.com/api/webhooks`. A product sends chat messages through Notify,
which holds the bot token and applies preferences, suppression and receipts
(cloud `docs/adr/ADR-01M495XFM4158579XRYVC92M6S-notify-chat-channel.md`).

```yaml
jobs:
  chat-senders:
    runs-on: sylphx-linux-standard
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@<sha>
        with:
          fetch-depth: 0
      - uses: SylphxAI/.github/.github/actions/chat-senders@<sha>
```

Only added lines are checked, so existing senders do not block a change while
they move to Notify. The allow-list is owned by this action alone: it names
Notify's chat sender, the ADR's decision 7 exceptions (chat administration with
a bot's own rights, conversational agents, platform paging), and tests,
fixtures and documentation in every repository. There is no inline waiver and
no caller input that widens it; a new exception is a reviewed change to
`allow-list.tsv` here. Set `mode: warn` to annotate without failing.
