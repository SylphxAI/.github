# Pinned Unity packages

Embed the complete Unity registry package closure from Build Cache before the
first editor launch. Unity uses embedded packages in preference to registry
packages in both [2020.3](https://docs.unity3d.com/2020.3/Documentation/Manual/upm-embed.html)
and [2022.3](https://docs.unity3d.com/2022.3/Documentation/Manual/upm-embed.html)
(documentation read 2026-11-06).

The action downloads only
`https://build-cache.sylphx.net/upm/<sha256>/<name>/<version>`. Build Cache verifies
and stores the archive in its existing CAS. CI verifies the SHA256 again before
extracting it under `Packages/<name>`. It neither requests registry metadata nor
falls back to a Unity host. A cold mirror miss may fetch the fixed Unity download
URL and its HTTPS tarball-CDN redirect; a warm mirror hit needs neither host.

## Consumer contract

Commit `Packages/packages-lock.json` from the actual editor and
`Packages/unity-packages.json` generated from that lock. The pins cover **all**
Unity registry entries, including transitive dependencies. Built-in packages
remain supplied by the selected editor. Other package registries, local archives
and Git dependencies keep their existing preparation steps.
Every pin records an exact positive `archiveBytes` count as well as its version
and SHA256. Graph admission refuses missing or oversized counts before network
access, so a newly captured editor graph cannot silently exceed the archive cap.

```yaml
- name: Hydrate pinned Unity packages
  uses: SylphxAI/.github/.github/actions/unity-packages-cache@<full-commit-sha> # main
  with:
    project-path: client/UnityProject

- name: Compile player C# without Unity registry access
  uses: SylphxAI/.github/.github/actions/unity-compile-scripts@<full-commit-sha> # main
  with:
    project-path: client/UnityProject
    unity-image: ${{ env.UNITY_IMAGE }}
    license-home: ${{ runner.temp }}/_github_home
    target: Android
    block-package-registry: 'true'
```

The project path may be workspace-relative or an absolute job-owned temporary project.

Run hydration after dependency preparation and before every project's first
editor invocation. Use a clean checkout; existing embedded directories are never
overwritten. Hydration stages and verifies the whole closure before installing
any package. Executable tools retain their executable bit. Unsafe paths, links,
special files, oversized archives and archive identity/dependency mismatches fail
closed. An existing Library is not required.
Downloads and extraction stream in 1 MiB chunks. The archive cap is 754,624,107
bytes: the largest of 71 measured consumer archives on 2026-11-06
(603,699,285 bytes), plus 25%. Extraction retains separate 2 GiB/100,000-entry
bounds. A download must match both the pinned SHA256 and byte count.

The action returns a `receipt` path containing every package version and SHA256.
A changed Unity manifest request, changed registry lock entry, missing transitive
pin, invalid digest, failed download or hash mismatch stops before Unity starts.
Non-Unity manifest changes made by existing Firebase/Git preparation do not
invalidate the Unity pins.

`block-package-registry: 'true'` maps `packages.unity.com`,
`download.packages.unity.com` and `cdn.packages.unity.com` to loopback in the
compilation container and selects fresh container-local `UPM_CACHE_ROOT`,
`UPM_CACHE_PATH` and `UPM_NPM_CACHE_PATH` locations so a licence probe or image
cache override cannot prewarm the proof. The subsidiary variables override the root
([Unity 2022.3 documentation](https://docs.unity3d.com/2022.3/Documentation/Manual/upm-config-cache.html),
read 2026-11-06). For other editor entrypoints, pass the same three
`--add-host <host>:127.0.0.1` Docker arguments. Do not configure an HTTP proxy
that could bypass these mappings. Test initial adoption with no `Library` or
user-level Unity package cache and record successful resolution for each editor
version. Keep existing licence activation separate from this project check.

## Updating packages

1. Change the manifest and resolve it using the selected editor with normal
   registry access in an explicit maintenance environment. Retain its real lock;
   do not reconstruct dependency selection by hand.
2. From a checkout of this shared action, run:

   ```sh
   python3 .github/actions/unity-packages-cache/packages.py pin --project /path/to/UnityProject
   ```

   Only this explicit maintenance command reads Unity registry metadata. It checks
   each archive against the registry's SHA1, validates its package identity and
   dependency map, and records its SHA256 and actual byte count. The runtime action
   never calls `pin`.
3. Hydrate a clean project copy through Build Cache to warm and verify every pin.
   Commit the manifest, editor lock and pins together. Prove cold-Library
   resolution with the Unity package hosts blocked before adopting the update.

The optional `mirror` input accepts an HTTPS origin only; redirects from it are
rejected. The default is the existing Build Cache service, not a new registry,
service or storage allocation.

## Tests

```sh
python3 -m unittest discover -s tests -p test_unity_packages_cache.py
python3 -m unittest discover -s tests -p test_unity_compile_scripts.py
```
