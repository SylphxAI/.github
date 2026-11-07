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
entries with `source: registry` and `url: https://packages.unity.com`, including
transitive dependencies and publishers such as `com.autodesk.fbx`. Selection
depends on the registry, not a `com.unity` name prefix. Package names use two or
more nonempty dot-separated segments containing lowercase ASCII letters, digits,
hyphens or underscores, up to Unity's 214-character limit
([Unity 2022.3 naming rules](https://docs.unity3d.com/2022.3/Documentation/Manual/cus-naming.html),
read 2026-11-06). Built-in packages
remain supplied by the selected editor. Other package registries, local archives
and Git dependencies keep their existing preparation steps. Every direct version
request must have a resolved `registry` or `builtin` lock entry, or a genuine
`embedded` override. Embedded overrides use the lock's relative `file:` path
inside `Packages` and require a package directory with a `package.json` whose
name matches the dependency. Admission anchors an opened project directory and
walks `Packages`, all embedded directory components and every input file with
fd-relative, no-follow opens. Manifest, lock, pins and embedded metadata are read
from those same opened file descriptors; symlinks and pathname replacement cannot
redirect these reads, even to another location inside `Packages`. The Linux action
retains these directory descriptors through receipt cleanup, staging, package
installation and pins output, so replacing `Packages` after admission cannot
redirect writes either. Private staging records the opened directory's device
and inode, rejects changed or linked staging paths immediately before extraction
and each installation move, and moves relative to the anchored staging descriptor.
Cleanup preserves uncertain replacements instead of following them. These checks
refuse static/committed links for input reads and writes; they are not an isolation
boundary against a concurrently running, fully privileged process in the same job
replacing private staging contents between checks. Missing entries,
stale Git/local/tarball sources and non-string manifest requests stop before
network access or mutation.
Unselected explicit local/Git requests remain unaffected.
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
Changes to direct requests for any selected official-registry package invalidate
the pins, including requests outside `com.unity`. Unrelated manifest changes made
by existing local/Git preparation do not invalidate them. Existing `com.unity`-only
manifest hashes remain compatible.

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
   never calls `pin`. Maintenance redirects allow only the exact HTTPS Unity CDN
   origin and `/tarballs/<name>/<version>/<sha1>.tgz` or
   `/tarballs/<name>/<name>-<version>/<sha1>.tgz`, bound to the requested package
   and version. Query strings, fragments, credentials, ports and unsafe paths
   are rejected. Both layouts were observed on 2026-11-06; for example,
   `com.autodesk.fbx@5.1.3` uses the version-only directory.
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
