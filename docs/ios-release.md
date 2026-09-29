# iOS signing and TestFlight upload

`.github/workflows/ios-release.yml` is the one reusable job that gets an iOS
app to TestFlight on `[self-hosted, macos, sylphx, standard]` (Xcode 26.3,
iOS 26.2 SDK, macOS 26.3.2 x86_64, no signing identities preinstalled).

**Scope.** The macOS fleet is internal only: it is not sold to tenants. It is
macOS virtualised on non-Apple hardware under a licence risk the owner accepted
(RUL-69). The workflow uploads to TestFlight only; nothing in it, or in any
script it runs, submits for App Store review or releases a build
(`tests/test_ios_release.py` fails if such a call appears).

## Two modes, one upload

- **Build mode** (default): the workflow signs, archives and exports. For an
  Xcode repository, or for an engine that exports an Xcode project (Unity).
- **Prebuilt mode** (`ipa-artifact`): an engine already built and signed the
  `.ipa` (Solar2D's CoronaBuilder). The workflow validates it, then uploads.

Both end in the same `altool` upload and the same `if: always()` cleanup.

## Why a reusable workflow

The temporary keychain, installed profiles and API key must be removed by an
`if: always()` step of the same job. A composite action has no post step, so
each caller would have to remember its own cleanup.

## Call it

Copy [`workflow-templates/ios-release.yml`](../workflow-templates/ios-release.yml).
Pin the workflow by commit. There is no `secrets:` block and no
`secrets: inherit`: the signing job names a GitHub environment and reads the
secrets from it (see Secrets).

Build mode, Xcode repository:

```yaml
jobs:
  testflight:
    uses: SylphxAI/.github/.github/workflows/ios-release.yml@<commit>
    with:
      scheme: MyApp
      bundle-id: com.example.myapp
      project: MyApp.xcodeproj
```

Build mode, Unity (an earlier job exports Xcode on Linux and uploads the
artifact `ios-build`; this job `needs:` it):

```yaml
jobs:
  export:  # Linux: Unity export, then actions/upload-artifact name: ios-build
  testflight:
    needs: export
    uses: SylphxAI/.github/.github/workflows/ios-release.yml@<commit>
    with:
      xcode-artifact: ios-build
      pre-build-script: client/ci/ios-prebuild.sh
      post-archive-script: client/ci/ios-postarchive.sh
      workspace: Unity-iPhone.xcworkspace
      scheme: Unity-iPhone
      bundle-id: com.example.game
      xcode-version: '26.3'
```

Prebuilt mode, Solar2D (an earlier job builds and signs the `.ipa` and uploads
it as the artifact `ipa`; this job `needs:` it):

```yaml
jobs:
  testflight:
    needs: build
    uses: SylphxAI/.github/.github/workflows/ios-release.yml@<commit>
    with:
      ipa-artifact: ipa
      bundle-id: com.example.game
```

### Inputs

| Name | Type | Default | Meaning |
| --- | --- | --- | --- |
| `bundle-id` | string, required | | App bundle id; selects the archive profile (build) or must equal the `.ipa`'s `CFBundleIdentifier` (prebuilt) |
| `environment` | string | `ios-release` | Environment of the signing job; must not be empty |
| `ipa-artifact` | string | `''` | Prebuilt mode: artifact holding exactly one signed `.ipa`, from an earlier job of the same run |
| `xcode-artifact` | string | `''` | Build mode: artifact holding an exported Xcode project, downloaded before the build |
| `artifact-path` | string | `.` | Repo-relative directory the `xcode-artifact` is downloaded into |
| `pre-build-script` | string | `''` | Build mode: script run with `bash` in the `prepare` job, before any secret exists (relative to the artifact directory if `xcode-artifact` is set, else the repository root) |
| `post-archive-script` | string | `''` | Build mode: same rules; runs after archive and before export, in a step with no signing secrets. The temporary keychain exists at that point (partition list `codesign:` only) |
| `xcode-version` | string | `''` | Build mode: select the installed `/Applications/Xcode*.app` whose version equals it or starts with it plus a dot (`26.3`); digits and dots only; fails listing the installed versions |
| `override-team` | boolean | `false` | Build mode: also pass `DEVELOPMENT_TEAM=APPLE_TEAM_ID` to the archive (applies to every target; safe only when they share one team) |
| `scheme` | string | `''` | Build mode: Xcode scheme (required in build mode) |
| `workspace` / `project` | string | `''` | Build mode: `.xcworkspace` or `.xcodeproj` path; set one |
| `configuration` | string | `Release` | Build configuration |
| `xcconfig` | string | `''` | `.xcconfig` in the repository, for per-target settings (extensions) |
| `build-number` | string | `''` | `CURRENT_PROJECT_VERSION` override, digits only |

There is no `ref` input: the job builds the caller's own triggering ref only.
Every path input must be repository-relative, with no `..` and no leading `/`.

### Secrets

The workflow declares no `secrets:`. Its signing job names the environment
(`environment` input) and reads these names from that environment of the
caller's repository, never from repository or organization secrets. A caller
job cannot declare an environment itself, so the environment lives in the
called workflow. A missing or empty secret fails the job, naming the secret and
the environment.

| Name | Content | Needed |
| --- | --- | --- |
| `APP_STORE_CONNECT_API_KEY_ID` | API key id (10 characters) | always |
| `APP_STORE_CONNECT_ISSUER_ID` | API issuer id (UUID) | always |
| `APP_STORE_CONNECT_API_PRIVATE_KEY_BASE64` | the `AuthKey_<id>.p8`, base64 | always |
| `IOS_DIST_CERTIFICATE_BASE64` | Apple Distribution certificate with private key (`.p12`), base64 | build mode |
| `IOS_DIST_CERTIFICATE_PASSWORD` | the `.p12` password | build mode |
| `APPLE_TEAM_ID` | Apple Developer team id (10 characters) | build mode |
| `IOS_PROVISIONING_PROFILE_BASE64` | App Store profile, base64; one profile per line when the app has extensions | build mode |

Set up the `ios-release` environment in the caller repository: deployment
branches limited to `main` and release tags, and ALL the secrets above stored
in it (not in the repository or organization). Other refs then cannot read
them. These names match the Cubeage organization secrets.

## Three jobs

1. `prepare` (no environment, no secret; the caller's own pre-build code runs
   here): checks out the ref, selects `xcode-version`, downloads
   `xcode-artifact`, runs `pre-build-script`, and uploads the prepared
   workspace as an artifact (one day retention). In prebuilt mode it only
   validates the inputs.
2. `sign` (build mode only; environment; holds ONLY the four signing secrets):
   the build steps below, then uploads the exported `.ipa` as an artifact (one
   day retention). Caller code runs in this job (Run Script phases, package
   plugins, the post-archive script), so it never references an App Store
   Connect secret.
3. `upload` (environment; the ONLY job with the `APP_STORE_CONNECT_*`
   secrets): on a fresh machine it downloads the `.ipa` (the `sign` artifact,
   or `ipa-artifact` in prebuilt mode), validates it, runs a process check,
   then writes the key and uploads.

## Build mode

1. Downloads the prepared workspace, then:
2. Creates a temporary keychain with a random, masked password
   (`set-keychain-settings -lut 5400`, the job's 90-minute budget), puts it
   first in the user search list (the existing list is kept), imports the
   certificate with access for `codesign` only, and runs
   `set-key-partition-list -S codesign:`. If the first real run fails on a key
   prompt, `apple:` is added under security review.
3. Decodes each profile (`security cms -D`) into
   `~/Library/MobileDevice/Provisioning Profiles/<UUID>.mobileprovision`. Each
   profile's bundle id comes from its `application-identifier`.
4. `xcodebuild archive`, then `post-archive-script` (if set), then
   `xcodebuild -exportArchive` with a generated `ExportOptions.plist`
   (`app-store-connect`, `signingStyle` manual, `signingCertificate` Apple
   Distribution, team id, bundle id to profile name map).

### Signing

The archive passes no `CODE_SIGN_IDENTITY`, `PROVISIONING_PROFILE_SPECIFIER`
or `CODE_SIGN_STYLE` on the command line. Settings given to `xcodebuild` on the
command line override every target in the build, including Pods, frameworks
and extensions, so one profile specifier breaks any project with more than
one signed target (Unity's `UnityFramework`, Pods, extensions); see the
CocoaPods discussion
[#6964](https://github.com/CocoaPods/CocoaPods/pull/6964) and Apple's
[developer forum thread](https://developer.apple.com/forums/thread/52810).
Chosen instead: the project's own signing settings sign the archive (the app
target is set to manual signing with the Apple Distribution identity and its
profile, scoped to that target, for example by the caller's `pre-build-script`
or Unity's player settings), and `-exportArchive` with a manual
`ExportOptions.plist` re-signs for distribution, the common CI practice. Not
chosen: archiving unsigned (`CODE_SIGNING_ALLOWED=NO`), which drops
entitlements from the archive. `override-team` adds `DEVELOPMENT_TEAM` for
projects whose targets share one team.

## Validation before upload

The `upload` job validates every `.ipa`, whether `sign` built it or the caller
did (prebuilt mode), and fails closed unless all of these hold:

- the artifact has exactly one `.ipa` with exactly one `Payload/*.app`;
- `CFBundleIdentifier` equals `bundle-id`;
- `codesign -dv --verbose=4` shows `Authority=Apple Distribution:` and
  `codesign --verify --deep --strict` passes;
- `embedded.mobileprovision` has `beta-reports-active` true, no
  `ProvisionedDevices` (an App Store profile), and an `application-identifier`
  ending in the bundle id.

The version and build are printed in the job summary. In prebuilt mode `sign`
is skipped and no signing secret reaches any job.

## Upload and cleanup

`xcrun altool --upload-app --apiKey --apiIssuer`, with the `.p8` written mode
0600 under `$RUNNER_TEMP` (found through `API_PRIVATE_KEYS_DIR`) only in this
step of the `upload` job, after validation and a process check, so build
scripts never see it. Then the `if: always()`
cleanup: each of restore search list, delete keychain, remove profiles, remove
the key and wipe temporary files runs even when another fails; failures are
collected and fail the step at the end.

TestFlight processing is asynchronous: a successful upload means Apple
accepted the file, and the build appears in TestFlight some minutes later (it
can still be rejected during processing, with an email to the account). The
workflow does not read the build back through the App Store Connect API; that
gap is stated, not hidden. Check the build in App Store Connect.

### Upload tool

Chosen: `xcrun altool --upload-app`. Apple's
[Upload builds](https://developer.apple.com/help/app-store-connect/manage-builds/upload-builds/)
page lists altool and Transporter as the CLI upload tools and still shows
`--upload-app`; its deprecation notice covers notarization only. `--upload-package`
needs extra per-app metadata, and `iTMSTransporter` is not documented on that
page. Xcode 26's altool has been reported to exit 0 on some failed uploads
([fastlane#29739](https://github.com/fastlane/fastlane/issues/29739)), so the
step checks the exit status, error text and a success marker. The first
end-to-end run confirms the marker text on Xcode 26.3.

## Trust model

The caller repository's own code runs in the job that ends up holding the
distribution identity. Only repositories that own that identity may call this
workflow; never one that builds third-party code.

- **Environment.** `sign` and `upload` have the environment, and the secrets
  live only there, so a run from another ref cannot read them. `prepare`, which
  runs the caller's pre-build code, has no environment and no secret.
- **Three jobs, three trust levels.** Caller code runs in `prepare` (no
  secret) and in `sign` (signing secrets only). It can append to that
  machine's runner command files (`BASH_ENV`, `PATH`, `DEVELOPER_DIR`, the
  process-guard hash) and leave processes behind. The App Store Connect key is
  therefore only ever in `upload`, a separate job on a fresh single-use
  machine that carries neither command-file state nor processes from `sign`,
  and which validates the `.ipa` again before the key is written. The `sign`
  job cannot upload a build or read the ASC key.
- **Build mode.** `xcodebuild archive` runs the project's Run Script phases
  while the keychain is unlocked, so title code can execute then. The
  `codesign:`-only partition list and the missing `-T /usr/bin/security` stop
  that code exporting the private key, but it could ask `codesign` to sign.
  Hence the trusted-ref and environment gating. The post-archive script also
  runs with the keychain present and no signing secret in its environment.
- **Command files.** Caller scripts run without `GITHUB_ENV`, `GITHUB_PATH`,
  `GITHUB_OUTPUT`, `GITHUB_STATE` or `GITHUB_STEP_SUMMARY` in their
  environment. A script that guesses the runner's command-file paths can still
  poison later steps of its own job; the job split contains that to `sign`,
  which holds no upload credential.
- **Lingering processes.** At the start of the signing job a baseline of the
  runner user's processes is recorded. After archive, after the post-archive
  script and after export, every new process of that user that is not an
  ancestor or child of the check itself and not on an explicit exclusion list
  (the runner's `Runner.*`, Apple system paths `/System`, `/usr/libexec`,
  `/usr/sbin`, `/Library/Apple`, and Xcode helpers such as `XCBBuildService`,
  `com.apple.dt.*`, `ibtoold`, `mdworker`) is sent TERM, then KILL after 5
  seconds, and the step fails if any survives. The `upload` job runs the same check without killing before the `.p8` is
  written; on a fresh machine it finds nothing. This closes a process that title code left running to
  read the `.p8` or use the keychain later. It does not close code that runs
  during archive itself. The exclusion list is conservative but was written
  without a real runner to observe: the first run may list an unexpected
  helper, and the list is widened under review. The helper is written to
  `$RUNNER_TEMP` before any caller code runs and its hash is checked before
  each use. Run it only on a dedicated runner: on a shared machine it would
  kill other jobs' processes.
- **Prebuilt mode.** No signing secret reaches any job. The earlier job that
  builds the `.ipa` holds whatever identity the engine needs; its trust is that
  job's own. The validation above stops uploading the wrong app or a
  non-App-Store build.
- The `security` tool takes the keychain and certificate passwords on its
  command line, visible to other processes on the same runner for that moment;
  the runners are single-job JIT machines.
