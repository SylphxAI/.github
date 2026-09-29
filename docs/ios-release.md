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
Pin the workflow by commit. Pass each secret by name; never `secrets: inherit`,
which hands the workflow every secret of the repository.

Build mode, Xcode repository:

```yaml
jobs:
  testflight:
    uses: SylphxAI/.github/.github/workflows/ios-release.yml@<commit>
    with:
      scheme: MyApp
      bundle-id: com.example.myapp
      project: MyApp.xcodeproj
      environment: ios-release
    secrets: # each NAME: ${{ secrets.NAME }}, all seven
```

Build mode, Unity (export Xcode on Linux in an earlier job that uploads the
artifact `ios-build`, then build on macOS):

```yaml
jobs:
  export:  # Linux: Unity export, then actions/upload-artifact name: ios-build
  testflight:
    needs: export
    uses: SylphxAI/.github/.github/workflows/ios-release.yml@<commit>
    with:
      xcode-artifact: ios-build
      pre-build-script: client/ci/ios-prebuild.sh
      workspace: Unity-iPhone.xcworkspace
      scheme: Unity-iPhone
      bundle-id: com.example.game
      environment: ios-release
    secrets: # all seven
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
      environment: ios-release
    secrets: # only the three APP_STORE_CONNECT_* secrets
```

### Inputs

| Name | Type | Default | Meaning |
| --- | --- | --- | --- |
| `bundle-id` | string, required | | App bundle id; selects the archive profile (build) or must equal the `.ipa`'s `CFBundleIdentifier` (prebuilt) |
| `environment` | string | `''` | GitHub environment of the job (see Trust model) |
| `ipa-artifact` | string | `''` | Prebuilt mode: artifact name holding exactly one signed `.ipa`, from an earlier job of the same run |
| `xcode-artifact` | string | `''` | Build mode: artifact holding an exported Xcode project, downloaded before the build |
| `artifact-path` | string | `.` | Repo-relative directory the `xcode-artifact` is downloaded into |
| `pre-build-script` | string | `''` | Build mode: script run with `bash` before any secret is materialised (relative to the artifact directory if `xcode-artifact` is set, else the repository root) |
| `scheme` | string | `''` | Build mode: Xcode scheme (required in build mode) |
| `workspace` / `project` | string | `''` | Build mode: `.xcworkspace` or `.xcodeproj` path; set one |
| `configuration` | string | `Release` | Build configuration |
| `xcconfig` | string | `''` | `.xcconfig` in the repository, for per-target settings (extensions) |
| `build-number` | string | `''` | `CURRENT_PROJECT_VERSION` override, digits only |

There is no `ref` input: the job builds the caller's own triggering ref only.
Every path input must be repository-relative, with no `..` and no leading `/`.

### Secrets

| Name | Content | Needed |
| --- | --- | --- |
| `APP_STORE_CONNECT_API_KEY_ID` | API key id (10 characters) | always |
| `APP_STORE_CONNECT_ISSUER_ID` | API issuer id (UUID) | always |
| `APP_STORE_CONNECT_API_PRIVATE_KEY_BASE64` | the `AuthKey_<id>.p8`, base64 | always |
| `IOS_DIST_CERTIFICATE_BASE64` | Apple Distribution certificate with private key (`.p12`), base64 | build mode |
| `IOS_DIST_CERTIFICATE_PASSWORD` | the `.p12` password | build mode |
| `APPLE_TEAM_ID` | Apple Developer team id (10 characters) | build mode |
| `IOS_PROVISIONING_PROFILE_BASE64` | App Store profile, base64; one profile per line when the app has extensions | build mode |

The four build-mode secrets are optional in the interface; build mode fails
with a clear error if one is missing. These names match the Cubeage
organization secrets.

## Build mode

1. Downloads `xcode-artifact` (if set), then runs `pre-build-script` (if set),
   in a step whose environment holds no signing secret.
2. Creates a temporary keychain with a random, masked password
   (`set-keychain-settings -lut 5400`, the job's 90-minute budget), puts it
   first in the user search list (the existing list is kept), imports the
   certificate with access for `codesign` only, and runs
   `set-key-partition-list -S codesign:`. If the first real run fails on a key
   prompt, `apple:` is added under security review.
3. Decodes each profile (`security cms -D`) into
   `~/Library/MobileDevice/Provisioning Profiles/<UUID>.mobileprovision`. Each
   profile's bundle id comes from its `application-identifier`.
4. `xcodebuild archive` (`CODE_SIGN_STYLE=Manual`, team id,
   `CODE_SIGN_IDENTITY=Apple Distribution`, the specifier of the profile
   matching `bundle-id`), then `xcodebuild -exportArchive` with a generated
   `ExportOptions.plist` (`app-store-connect`, manual signing).

## Prebuilt mode

Downloads `ipa-artifact` and fails closed unless all of these hold:

- the artifact has exactly one `.ipa` with exactly one `Payload/*.app`;
- `CFBundleIdentifier` equals `bundle-id`;
- `codesign -dv --verbose=4` shows `Authority=Apple Distribution:` and
  `codesign --verify --deep --strict` passes;
- `embedded.mobileprovision` has `beta-reports-active` true, no
  `ProvisionedDevices` (an App Store profile), and an `application-identifier`
  ending in the bundle id.

The version and build are printed in the job summary. The signing secrets are
not read in this mode.

## Upload and cleanup

`xcrun altool --upload-app --apiKey --apiIssuer`, with the `.p8` written mode
0600 under `$RUNNER_TEMP` (found through `API_PRIVATE_KEYS_DIR`) only in this
step, after the build, so build scripts never see it. Then the `if: always()`
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

- **Environment.** The caller repository creates an `ios-release` environment
  with deployment branches limited to `main` and release tags, stores ALL the
  signing and App Store Connect secrets in that environment (not the
  repository or organization), and passes `environment: ios-release`. Other
  refs then cannot read them.
- **Build mode.** `xcodebuild archive` runs the project's Run Script phases
  while the keychain is unlocked, so title code can execute then. The
  `codesign:`-only partition list and the missing `-T /usr/bin/security` stop
  that code exporting the private key, but it could ask `codesign` to sign.
  Hence the trusted-ref and environment gating. The pre-build script and any
  artifact download run before the keychain exists and see no signing secret.
- **Prebuilt mode.** No signing secret reaches the job. The earlier job that
  builds the `.ipa` holds whatever identity the engine needs; its trust is that
  job's own. The validation above stops uploading the wrong app or a
  non-App-Store build.
- The `security` tool takes the keychain and certificate passwords on its
  command line, visible to other processes on the same runner for that moment;
  the runners are single-job JIT machines.
