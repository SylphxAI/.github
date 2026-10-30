# Literal model selectors

Product requests inherit the organization's runtime model default. A request
may instead name a Sylphx AI catalog SKU of the form
`<provider>/<line>-latest`. The catalog controls which revision that SKU serves.

The stack-conformance model check rejects dated and undated upstream model IDs.
Selection fields and constants such as `model`, `AI_MODEL`, `modelId`,
`defaultModel`, and calls such as `languageModel(...)` also reject arbitrary
`provider/model` literals and bare model families. Provider recognition does
not depend on a fixed list of vendor names. Lists of model choices and literal
environment fallbacks are checked too.

For example, all of these fail:

```typescript
const AI_MODEL = "deepseek/deepseek-v4.1-flash";
const model = "gpt-5.1-2025-11-13";
const modelId = "claude-sonnet";
const defaultModel = "new-provider/new-line";
```

These pass:

```typescript
client({model: organisation.defaultModel});
client({prompt});
client({model: "deepseek/deepseek-flash-latest"});
```

Comments, documentation and prose mentions do not select a model. The existing
path exclusions cover tests, fixtures, documentation/examples, dependency
trees and build outputs. Inline Rust test bodies are excluded without excluding
their surrounding production source. Catalog and provider-locator ownership
exceptions remain restricted to the repository/path pairs in `model_pins.py`.
Ordinary repository identifiers and dependency paths do not become model IDs
because an earlier field on the same line mentions a model.

This is an offline structural check. Computed, encoded or indirectly selected
model IDs still need review; CI does not prove arbitrary runtime data flow or
the existence of a catalog SKU. A pinned alias declaration can tighten the
existing SKU admission check.

## Widened recognition and existing debt

Previously recognized findings retain the `model-pin` rule and their existing
baseline keys. Newly recognized selectors have rule `model-selector`, exposed
by `rule_id(finding)`. Their keys keep the same `model-pin <path> <id>
<occurrence>` format, so moving a source line preserves a record and copying
an ID creates new debt.

The widened capability is adopted separately using
`# stack-rule-adopted-v1 model-selector`. The existing
`# model-alias-ratchet-v1` marker remains required for model baseline records.
An immutable old-checker profile establishes whether this capability is newly
introduced. Only then may `decide(..., seed_rules={"model-selector"})` admit
new-coverage findings independently present at the base revision. Old model-pin
debt does not gain a new seeding opportunity. After adoption, all records can
only shrink, and removed findings require their stale records to be deleted.

The shared release integrates this module through the existing adoption path:

1. Include `model-selector` in current and source-profile capabilities only
   when the immutable module declares `SELECTOR_RULE` and the checker calls
   `model_pins.decide`.
2. Use `model_pins.rule_id` in the base/head adoption loops, and ensure the
   model baseline marker exists when either model capability is first adopted.
3. Pass the validated introduced rules as `seed_rules` to `decide`.
4. Register the actual predecessor release profile, then update callers and
   their rule-scoped baselines through the existing pin-bump/onboarding route.

The detector's local tests establish recognition and shrinking-baseline
behavior. Required caller CI must separately demonstrate a clean pass and a
failure after adding a forbidden model selector before rollout is complete.
