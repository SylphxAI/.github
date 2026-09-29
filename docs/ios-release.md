# iOS signing and TestFlight upload

`.github/workflows/ios-release.yml` is the one reusable job that signs,
archives, exports and uploads an iOS app to TestFlight. It runs on
`[self-hosted, macos, sylphx, standard]` (Xcode 26.3, iOS 26.2 SDK, macOS
26.3.2 x86_64, no signing identities preinstalled).

**Scope.** The macOS fleet is internal only: it is not sold to tenants. It is
macOS virtualised on non-Apple hardware under a licence risk the owner accepted
(RUL-69). The workflow uploads to TestFlight only; nothing in it, or in any
script it runs, submits for App Store review or releases a build
(`tests/test_ios_release.py` fails if such a call appears).

## Trust model

The caller repository's own build code (build-phase scripts, SPM plugins) runs
in the job that holds the unlocked distribution identity. Only repositories
that own that identity may call this workflow; never one that builds
third-party code. The API key is written only in the upload step, after the
build.

## Why a reusable workflow

The temporary keychain, installed profiles and API key must be removed by an
`if: always()` step of the same job. A composite action has no post step, so
each caller would have to remember its own cleanup. One workflow keeps setup,
build, upload and cleanup in one job.

## Call it

Copy [`workflow-templates/ios-release.yml`](../workflow-templates/ios-release.yml).
Pin the workflow by commit. Pass each secret by name; never `secrets: inherit`,
which hands the workflow every secret of the repository.

```yaml
jobs:
  testflight:
    uses: SylphxAI/.github/.github/workflows/ios-release.yml@<commit>
    with:
      scheme: MyApp
      bundle-id: com.example.myapp
      project: MyApp.xcodeproj
    secrets:
      APP_STORE_CONNECT_API_KEY_ID: ${{ secrets.APP_STORE_CONNECT_API_KEY_ID }}
      APP_STORE_CONNECT_ISSUER_ID: ${{ secrets.APP_STORE_CONNECT_ISSUER_ID }}
      APP_STORE_CONNECT_API_PRIVATE_KEY_BASE64: ${{ secrets.APP_STORE_CONNECT_API_PRIVATE_KEY_BASE64 }}
      IOS_DIST_CERTIFICATE_BASE64: ${{ secrets.IOS_DIST_CERTIFICATE_BASE64 }}
      IOS_DIST_CERTIFICATE_PASSWORD: ${{ secrets.IOS_DIST_CERTIFICATE_PASSWORD }}
      APPLE_TEAM_ID: ${{ secrets.APPLE_TEAM_ID }}
      IOS_PROVISIONING_PROFILE_BASE64: ${{ secrets.IOS_PROVISIONING_PROFILE_BASE64 }}
```

### Inputs

| Name | Type | Required | Default | Meaning |
| --- | --- | --- | --- | --- |
| `scheme` | string | yes | | Xcode scheme to archive |
| `bundle-id` | string | yes | | App bundle id; selects the profile used for the archive |
| `workspace` | string | one of the two | `''` | `.xcworkspace` path |
| `project` | string | one of the two | `''` | `.xcodeproj` path, used when `workspace` is empty |
| `configuration` | string | no | `Release` | Build configuration |
| `xcconfig` | string | no | `''` | `.xcconfig` in the repository, for per-target settings (extensions) |
| `build-number` | string | no | `''` | `CURRENT_PROJECT_VERSION` override, digits only |
| `ref` | string | no | `''` | Ref to check out; empty is the caller's own |

### Secrets (all required)

| Name | Content |
| --- | --- |
| `APP_STORE_CONNECT_API_KEY_ID` | API key id (10 characters) |
| `APP_STORE_CONNECT_ISSUER_ID` | API issuer id (UUID) |
| `APP_STORE_CONNECT_API_PRIVATE_KEY_BASE64` | the `AuthKey_<id>.p8`, base64 |
| `IOS_DIST_CERTIFICATE_BASE64` | Apple Distribution certificate with private key (`.p12`), base64 |
| `IOS_DIST_CERTIFICATE_PASSWORD` | the `.p12` password |
| `APPLE_TEAM_ID` | Apple Developer team id (10 characters) |
| `IOS_PROVISIONING_PROFILE_BASE64` | App Store profile, base64; one profile per line when the app has extensions |

These names match the Cubeage organization secrets.

## What it does

1. Creates a temporary keychain with a random, masked password
   (`set-keychain-settings -lut 5400`, the job's 90-minute budget), puts it first in the user search list
   (the existing list is kept), imports the certificate (access granted to `codesign` only, so build code cannot export the private key with `security`) and runs
   `set-key-partition-list -S apple-tool:,apple:,codesign:`.
2. Decodes each profile (`security cms -D`) into
   `~/Library/MobileDevice/Provisioning Profiles/<UUID>.mobileprovision`. The
   bundle id of each profile comes from its `application-identifier`, so no
   mapping input is needed.
3. `xcodebuild archive` with `CODE_SIGN_STYLE=Manual`, the team id,
   `CODE_SIGN_IDENTITY=Apple Distribution` and the specifier of the profile
   matching `bundle-id`; then `xcodebuild -exportArchive` with a generated
   `ExportOptions.plist` (`app-store-connect`, manual signing, bundle id to
   profile name map).
4. Uploads the `.ipa` with `xcrun altool --upload-app` and the API key
   (`~/.appstoreconnect/private_keys/AuthKey_<id>.p8`, mode 0600).
5. `if: always()` cleanup: restores the search list, deletes the keychain,
   removes the installed profiles and the API key, and wipes the workspace
   temporary files.

Decoded files are written mode 0600 under `$RUNNER_TEMP`. Secrets reach a step
only through its `env:` and are never echoed. The `security` and `altool`
tools take the keychain and certificate passwords on their command line, so
they are visible to other processes on the same runner for that moment; the
runners are single-job JIT machines.

## Upload tool

Chosen: `xcrun altool --upload-app --apiKey --apiIssuer`. Apple's
[Upload builds](https://developer.apple.com/help/app-store-connect/manage-builds/upload-builds/)
page lists altool and Transporter as the CLI upload tools and still shows
`--upload-app`; its deprecation notice covers notarization only (replaced by
`notarytool`). `--upload-package` needs extra per-app metadata (numeric Apple
id, bundle version) and Xcode 26 reports of it are mixed. Transporter's
`iTMSTransporter` is not documented on that page. Xcode 26's altool has been
reported to exit 0 on some failed uploads
([fastlane#29739](https://github.com/fastlane/fastlane/issues/29739)), so the
step checks the exit status, error text in the output, and a success marker.
The first end-to-end run confirms the marker text on Xcode 26.3.

## Limits

- No App Store review submission or release, by design.
- With extensions, pass `xcconfig` setting per-target `PROVISIONING_PROFILE_SPECIFIER`;
  the command-line specifier is only the main app's profile.
