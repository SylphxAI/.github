# Unsigned Unity player C# compilation

`unity-compile-scripts` compiles the consumer project's actual player assemblies
for Android or iOS with its existing Unity image, dependencies and licensed HOME.
It does not produce a native player, invoke Gradle/Xcode, sign, install or upload
an app. A network-writer stub compile or static source scan is not this check.

Run the caller's normal dependency materialization and Unity activation first,
then invoke the shared action pinned to a commit:

```yaml
- uses: SylphxAI/.github/.github/actions/unity-compile-scripts@<sha>
  with:
    project-path: UnityProject
    unity-image: ${{ env.UNITY_IMAGE }}
    license-home: ${{ runner.temp }}/_github_home
    target: Android
```

The caller returns its license through its existing always-run cleanup. The
action does not acquire credentials or retry authentication. Fork PRs cannot
be granted private license/dependency secrets; apply the repository's existing
trusted-PR policy rather than borrowing another identity.

The helper installs one generated editor entry into the checked-out workspace,
refusing to overwrite consumer-owned code. Unity's `-buildTarget` and
`PlayerBuildInterface.CompilePlayerScripts` compile non-development player C#;
the receipt identifies the target, Unity version, UTC time and assembly hashes.
Compiler errors, empty output, target mismatch and a stale receipt fail closed.
Output defaults to `Builds/sylphx-player-scripts`, outside Unity's transient
`Temp` directory; `Temp` is refused because editor shutdown removes it.

The editor container receives a soft and hard open-file limit of 65,536 and
prints both values before starting Unity. An 8,192 limit has produced a Mono
`ToFileDescriptor` assertion during Bee named-pipe startup, ending the compiler
with signal 6/exit 134. The larger limit follows the existing Unity container
mitigation; a failed editor still fails the action, without an automatic retry.

No title names or source pins belong in the shared action. The consumer owns
its project path, editor version, dependency pins, store-target selection and
PR/merge-group trigger. Keep the native APK/IPA lane separate from this compile
gate so an unrelated native linker or artifact-storage failure does not hold a
source-level compiler correction.

Primary API documentation, read2026-10-29:
- Unity2020.3 and2022.3 `UnityEditor.Build.Player.PlayerBuildInterface.CompilePlayerScripts`.
- `BuildScriptsOnly` is not used: Unity documents that it requires a prior full
  build and may ignore pending player-data changes.

Local tests use an isolated temporary project and fake Docker failure to test
the handoff/negative invariants. Actual Unity consumer CI is still required
before calling the store-target compile gate verified.
