# Templates

## Delivered customer projects

A finished, handed-over customer project carries the organization custom
property `sylphx_delivery` = `delivered` and gets no automated change: no
adoption or rollout pull request, ruleset or settings sweep, or dependency bot.
Every script that writes to many repositories runs
`scripts/is-delivered.sh <owner/repo>` first and skips the repository on exit 0
(or on exit 2, an unreadable property).
