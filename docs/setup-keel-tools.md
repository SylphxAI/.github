# setup-keel-tools

`.github/actions/setup-keel-tools` puts the two tools a Keel title's web pack
needs on `PATH`:

- `keel`, built from the Keel commit in the title's `KEEL_PIN`. `keel pack` is
  the only packer, and the shell it writes must be the one the title was
  migrated against, so the CLI is never taken from another revision.
- `wasm-bindgen`, at the version the title's `Cargo.lock` pins for the
  `wasm-bindgen` crate; the CLI and the crate must match exactly.

Both are installed into one root under `$RUNNER_TEMP/keel-tools`, cached per
runner OS and arch, Keel commit, wasm-bindgen version, `features` and the
installer's hash. A warm key restores the root and logs `keel tools from
cache` in seconds; a new pin builds both from source once (about five
minutes) and saves the cache straight after the build, so a later failing step
still leaves it for the next run. The root carries a stamp written last, so a
half-built root never counts as warm.

## Use

After checkout and the Rust toolchain (and `rust-sccache`, which speeds the
cold build):

```yaml
- uses: SylphxAI/.github/.github/actions/setup-keel-tools@<sha>
```

Inputs, all optional:

| Input | Default | Meaning |
| --- | --- | --- |
| `pin` | empty | The Keel commit; empty reads `pin-file`. |
| `pin-file` | `KEEL_PIN` | File holding the Keel commit. |
| `wasm-bindgen` | `auto` | `auto` reads `cargo-lock`; `none` skips it; or an exact `x.y.z`. |
| `cargo-lock` | `Cargo.lock` | Lockfile read when `wasm-bindgen` is `auto`. |
| `keel-src` | empty | An existing Keel checkout at the pin to build from; empty clones SylphxAI/keel. |
| `features` | empty | Extra cargo features for the keel CLI; a title whose `keel.toml` encodes GPU textures needs `texture-encoder`. |
| `working-directory` | `.` | Directory holding `pin-file` and `cargo-lock`. |

Outputs: `bin` (the directory on `PATH`) and `cache-hit`.

A title that packs GPU textures sets `features: texture-encoder`, because
`cargo install` without it builds the keel CLI without that feature (a weight
one, `opt-level = 1` in Keel's workspace profile). Two shapes of the same pin
are two cache entries and two builds, so a title asks for the features all of
its pack jobs need rather than one shape per job.

This replaces each title's own copy: an inline `cargo install --path
.../crates/keel-cli` step, a local `.github/actions/keel-cli`, a separate
wasm-bindgen install step, or a `scripts/install-keel-tools.sh`. A title moves
by deleting its copy and calling this action at a pinned SHA.
