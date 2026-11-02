# Literal address guard

Reject newly added IPv4 and IPv6 literals belonging to an infrastructure
inventory. Consumers use DNS/routing names rather than pinning a node or an
entry address. The inventory owner supplies policy; this action owns matching
and Git-diff semantics, not a company's network allocations.

## Use

Run on **every** pull request and merge group, without a path filter, inside a
required job. Checkout the checked head and its base; pass that exact base SHA.
Generate the inventory immediately before this step from committed source.

```yaml
- uses: SylphxAI/.github/.github/actions/literal-address-guard@<full-commit-sha> # main
  with:
    base: ${{ github.event.pull_request.base.sha || github.event.merge_group.base_sha }}
    inventory: ${{ runner.temp }}/address-inventory.json
```

For an external inventory, check out its owner repository's current default
branch with an authorized read-only credential, generate from that checkout,
and set `inventory-root` to its path. Do not commit a copied address list or
silently fall back to an old snapshot when source access fails. Only explicitly
adopted repositories run this action; it does not dispatch to other repositories.

## Inventory contract (version 1)

The generated JSON has:

- `sourceHead`: full commit SHA matching `inventory-root`'s HEAD.
- `sources`: nonempty `{revision, path, sha256}` records for every source blob;
  at least one names `sourceHead`. Digests are verified against Git objects.
- `networks`: nonempty `{cidr, ...ownerMetadata}` records with canonical host
  `/32` or `/128` networks, or authoritative delegated prefixes. Match individual
  private node addresses, not entire private pod/service networks.
- `permits`: `{path, line, text, start, end, reason}` records; positions are
  one-based lines and zero-based character offsets `[start,end)`. The inventory
  owner's parser derives each exact declaration scalar, not an entire file or
  directory. Neighboring consumer values remain checked. Permits apply only
  when checking the inventory repository itself, never to consumer repositories.

The owner generator validates its schemas and coverage and fails on new
populated inventory types it cannot understand. `SylphxAI/infra` supplies the
Cell-specific generator and retirement-address policy. Source HEAD/digest
checks detect stale generated output; acquiring the current authoritative
checkout remains the caller's workflow responsibility.

## Checked boundary

All added text lines in tracked files are checked, including docs, scripts,
tests and workflows. Unchanged inherited lines do not block unrelated work.
Renames are treated as delete/add, so moving or duplicating a literal cannot
spend a historical count allowance. IPv4 URLs/ports, canonical IPv6 variants,
zone identifiers, IPv4-mapped IPv6 and subnet membership are recognized.
A host or subnet contained in an owned prefix also fails. Broader aggregate
networks and default routes do not pin an individual node and are not matches.

Invalid/missing revisions, empty or stale inventory, source digest mismatches,
changed submodules and unreadable text fail closed. Binary blobs are not
inspected. Dynamically assembled, encoded or generated runtime addresses are
outside this static check; a pass is not a runtime traffic census.

Tests: `python3 -m unittest discover -s tests -p test_literal_address_guard.py`.
