# Native bridge contract

The `native-bridge-check` action compares Android native name-dispatch call
sites with the exact published SDK asset selected by the title's pin. Run it
in a required source job and in the Android build before compilation. It does
not build the SDK, contact a store, or execute purchases.

```yaml
- uses: SylphxAI/.github/.github/actions/native-bridge-check@<full-commit> # main
  with:
    reader-app-id: ${{ secrets.CUBEAGE_CI_READER_APP_ID }}
    reader-app-key: ${{ secrets.CUBEAGE_CI_READER_APP_KEY }}
```

An Android build that already minted a read-only SDK token can pass `token`
instead. The job must run on a self-hosted runner with Python 3. The read-only
App token is restricted to `Cubeage/cubeage-native-sdk` contents. A missing
release, missing asset, unverifiable SHA256, or unsupported registration shape
fails the check. There are no maintained export lists or source-tree fallbacks.

For Solar2D, the pin comes from `client/**/build.settings`. The checker reads
`plugin-release.aar/classes.jar` from the released `android.tgz`, follows the
`NamedJavaFunction` classes constructed in `LuaLoader.invoke`, and resolves
their compiled `getName` methods. Only functions in that registration array
count. Orphan classes and convenient Lua wrappers in the SDK source do not.

Lua calls, member references and capability checks on `sdk`, `plugin`, and
`cubeageNative` are checked across the client. Direct `plugin.cubeage` imports
and local aliases are discovered. Nonstandard injected receiver names can be
supplied to the CLI with `--lua-receivers`; this declares object identities,
never exported methods. Literal indexing is supported; computed bridge names
fail. A contiguous sequence of `if type(sdk.name) == "function" then return
sdk.name end` clauses is an explicit first-available selector: at least one
alternative must exist. Ordinary capability conjunctions require every name.

For Unity, the pin comes from `client/**/Packages/manifest.json`. The checker
downloads `cubeage-sdk-vX.Y.Z.aar`, reads its public JVM methods by class, and
checks the exact pinned `AndroidBridge.cs` plus title C# call sites. The pinned
adapter is treated as a caller, never as evidence that a native method exists.
Literal `AndroidJavaClass`/`AndroidJavaObject` receivers, class-name constants,
and singleton instances are resolved. Computed method names fail for these
receivers. This checks names, not JNI argument signatures, return types, or
callback behavior. Calls on objects returned through opaque callbacks are
outside this name check; their protocol still needs adapter/runtime tests.

This action checks the Android artifact. It does not claim iOS symbol, Lua
registration, store delivery, or installed-production-build acceptance.

For an offline census, download the pinned release asset and run:

```sh
python3 bridge_contract.py --root /path/to/title \
  --artifact /path/to/published/android.tgz --report census.json
```

The normal action always downloads the published asset and verifies its GitHub
release digest. `--artifact` is only for offline analysis of previously verified
bytes. Offline Unity analysis also accepts `--sdk-source` pointing to the exact
pinned SDK checkout. JSON reports include the artifact hash, registered names,
source paths, line numbers, alternatives and missing names.

The regression acceptance uses the actual published artifact:

```sh
NATIVE_BRIDGE_RELEASE_ARTIFACT=/path/to/published/android.tgz \
  python3 -m unittest discover -s tests -p test_native_bridge_contract.py -v
```

The test rejects capability checks for `restorePurchases` and
`querySubscriptions`, then accepts their native `*Json` names against the same
release bytes. Additional unit tests compile a registration table and verify
that an unregistered class cannot make the check pass.
