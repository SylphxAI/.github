# Rust source reachability

The shared `stack-conformance` action checks Rust wiring on every invocation.
For a local check, run its `rust_reachability.py` with the repository root as
the optional positional argument.
It takes seconds, does not invoke Cargo, and checks every tracked or unignored
`.rs` beneath a Cargo package, including packages excluded from the root
workspace. A source file with no target/module/include edge fails the action. The regression suite also demonstrates the failure and repair.

The check follows explicit and automatically discovered lib/bin/test/bench/
example/build-script roots, `mod`, literal `#[path]`, `cfg_attr` alternatives,
inline modules, raw identifiers, `include!`, and trybuild fixture globs. It takes
the union of configurations: a platform/feature-gated module is reachable.
`OUT_DIR` includes refer to generated build output, not checked-in files.
Unknown dynamic include expressions fail for manual inspection. Embedding a
source with `include_str!`/`include_bytes!` does **not** compile its tests.

Real templates, externally copied harness inputs, or preserved vendored tools
can use `.rust-reachability.json`: an object mapping **exact file paths** to
specific reasons and their consumer. No globs or unexplained baseline entries.
An exception that becomes reachable or disappears fails until removed.
Prefer deleting obsolete code or restoring its real tests over an exception.

This is a source-wiring check, not proof that every cfg branch runs, a macro
expands, or a test is selected by CI. Cargo/rustc dep-info from the relevant
platform/feature build remains authoritative for an actual compilation. Read
hits and consumers before deleting or reconnecting them. Standalone `.rs`
outside Cargo packages are reported in `--json` for census/manual inspection;
they are not assumed to be Cargo modules.

References read on 2026-11-02: [Rust module path rules](https://doc.rust-lang.org/reference/items/modules.html)
and [Cargo targets and auto-discovery](https://doc.rust-lang.org/cargo/reference/cargo-targets.html).
The witnessed failure was test files remaining on disk when their module
declarations disappeared: an all-target Cargo check silently missed them.

Caller pin updates adopt `.rust-reachability.json` only for inspected real
exceptions in the same change. Never seed a blanket baseline. Delivered
customer repositories remain exempt under the existing property check.

Python 3.11+ is required (`tomllib`); git and the checked-out source tree are
the only inputs. The check performs no network request, dependency fetch, or
compilation. Work is linear in the scanned source/module edges; a 4,273-file
monorepo scan measured 9.1s on 2026-11-02, with smaller repos under 4s. It runs
only during the existing conformance job and retains no service or cache.
