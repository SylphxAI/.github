#!/usr/bin/env bash
#
# is-delivered.sh -- exit 0 when a repository is a delivered customer project.
#
# A finished, handed-over customer project carries the organization custom
# property `sylphx_delivery` = `delivered`. Every sweep, rollout, adoption or
# bot script that writes to many repositories calls this first and skips the
# repository on exit 0. It changes only on a customer request.
#
# Usage: scripts/is-delivered.sh owner/repo
# Exit:  0 delivered (skip it), 1 not delivered, 2 unreadable (treat as skip).
set -euo pipefail

repo="${1:?usage: is-delivered.sh owner/repo}"
value="$(gh api "repos/${repo}/properties/values" \
  --jq '.[] | select(.property_name == "sylphx_delivery") | .value' 2>/dev/null)" || exit 2
[[ "${value}" == "delivered" ]]
