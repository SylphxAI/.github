# Deal randomness

The shared `stack-conformance` action reports `C1/deal-randomness` when a
recognized shuffle, deal, card or tile draw, or dice roll uses an insecure
random source. Slot results and reward rolls are also outcomes. Cosmetic particles, animation and rendering remain outside
the rule.

The recognizer covers Rust, JavaScript, TypeScript, Java, Lua and C#. It
recognizes time or constant seeds for `StdRng`/ChaCha, non-cryptographic
generators such as `SmallRng`, `Math.random`, Java `Random`, default
`Collections.shuffle`, Lua `math.random`, and Unity `Random`. Same-file helper
calls, explicit receiver bindings and ordinary Rust RNG module imports are
traced. A secure seed does not make xorshift, xoshiro, PCG or an LCG secure.
Same-named helpers retain class ownership; uncalled nested declarations and
proven literal-false branches do not execute through their enclosing deal.

Use OS entropy and a cryptographically secure generator: `OsRng`, `getrandom`,
`rand::rng()`, securely seeded ChaCha/`StdRng`, Web Crypto,
`crypto.randomInt`, `SecureRandom`, or `RandomNumberGenerator`. A client may
display a server-provided deal. Fail explicitly when entropy is unavailable;
a time or constant fallback restores the defect.

Every unbaselined recognized deal violation fails, including one inherited
on unchanged lines. Existing debt is adopted through the ordinary
[rule-scoped adoption helper](stack-conformance.md#adopting-new-rules-with-a-pin-bump)
in the same shared-action pin update. Only findings independently present at
the immutable base can be seeded. Exact violation spans and occurrence counts
then only shrink: copying a violation fails, and removing it requires deleting
its stale baseline record. Architecture decisions cannot waive this rule.
Checked-in runtime engine copies are scanned even under `vendor`; tests,
fixtures, protocol-generated source and installed dependency directories are
excluded.

The report separates `deal_randomness.raw_emissions`, `warnings` and
`refusals`. A warning remains visible and does not fail the check. The pinned
checker owns [the scope policy](../policy/deal-randomness-scope.json); a caller
cannot supply its own exceptions. Each audited warning binds the exact source,
its caller population, and configuration/entrypoint evidence. A new caller or
changed selection input invalidates the old warning. Both detector and policy
bytes participate in the checker capability fingerprint.

The present audited warning scopes are:

- Korvana `finance.ts`: the local dice helper is called only in demo mode;
  real settlement follows the separate kernel/on-chain path.
- Tryit sliding puzzle: visible numbered tiles form a solvable board without
  a hidden deal or a chance-based score.
- Limited Fun Mahjong Keel's vendored FunRoom APIs: the server selects
  MahjongTw16/HK13; the client has a separately pinned engine dependency.
- Mahjong verifier/analytics scripts: recorded seeds reproduce historical
  outcomes; these scripts do not author a live deal.

Slot reel results remain outcome paths. A dice-named helper is not sufficient
evidence that its caller scores dice; unused APIs should be deleted through
the owning repository's ordinary change process. AI seed reads inside an
insecure deal remain part of that deal and require remediation or baseline.

This static check does not prove arbitrary cross-module seed provenance,
encoded sources, reflection, native binary behavior or installed client
distribution. The game inventory traces those paths separately. An unknown
seed parameter is not declared insecure solely because it is a parameter;
callers must supply secure entropy. Source tests exercise the actual deal
with the insecure source disabled, and verify entropy failure prevents a deal.
Macros, reflection, untyped cross-module receiver aliases, arithmetic dice
outside named operations, and unsupported Godot source require independent
inventory traces. The rule does not certify these unknown paths secure.
