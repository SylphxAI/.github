# Secret scanning in the shared verdict

`needs-pass` and `ci-ok` scan the commits added by every `pull_request` and `merge_group`
before returning a green verdict. A repository calling both gates scans twice
by design; a scan takes seconds and needs no shared receipt or deduplication
state. Updating the existing action pin enables the
check; callers need no new workflow step or checkout. The job needs
`contents: read` and a self-hosted Linux runner (`sylphx-linux-control` is enough).
No license or paid service is used.

The scanner fetches immutable Git objects into a temporary bare repository.
It runs the checksum-pinned MIT gitleaks 8.30.1 binary with its default rules.
It never checks out or executes the target repository's code. Authentication
uses the existing job token, is scoped to the GitHub server and stays in the
process environment rather than the repository configuration.

For pull requests the range is `pull_request.base.sha..pull_request.head.sha`;
for merge groups it is `merge_group.base_sha..merge_group.head_sha`. Every commit
in that range is checked, including a secret removed in a later commit. Only
added lines produce findings; unchanged historical secrets need no baseline.
Merge resolutions are checked, while lines inherited from a merge parent are
excluded. An unreadable range, invalid allowlist or failed detector fails closed.
Other events retain the aggregate verdict without fetching or installing a scanner.

A finding fails the action and names its commit, path, new-file line and SHA-256
of the complete added line, excluding its newline. Values and raw subprocess
output are never printed. A filename containing the detected value is withheld.
The raw detector report is kept only inside the private temporary directory,
removed when the scan exits, and never uploaded as an artifact.

## Reviewed exceptions

The aggregate gate deliberately ignores `.gitleaks.toml`, `.gitleaksignore` and
`gitleaks:allow` comments from the scanned repository. Use the same exact,
reasoned format as the publication guard: `.desk-secret-allowlist.json` at the
scanned head. Each entry must identify one specific finding:

```json
{
  "version": 1,
  "findings": [
    {
      "commit": "<full commit SHA that introduced the line>",
      "path": "tests/example.txt",
      "line": 12,
      "sha256": "<64 lowercase hex characters from the finding>",
      "reason": "Nonfunctional provider example used by this detector test."
    }
  ]
}
```

The reason must be nonempty. There are no wildcards, path-wide exceptions or
value-wide exceptions: a different commit, path, line or digest is a new finding.
For a multiline finding each reported added line needs its own entry. Commit
the exception in a later commit so it can name the immutable introducing commit.
Never copy the suspected credential into a reason, PR discussion or log. A real
credential must be revoked and removed; an exception is for a reviewed false
positive. Review changes to this file as security policy changes.

The standalone `secret-scan` action uses this same detector and exact exceptions.
Its existing explicit `config-path` input remains available for callers with a
reviewed gitleaks policy; it never changes the default policy of the aggregate gates.
The standalone action also retains push-range scanning and needs a checkout with
full history. Both Linux x64 and arm64 release archives have pinned checksums.

Repositories with their own scanner keep it. The shared scan runs alongside
those checks without importing a second scanner's broader suppression rules.
Secret detection is pattern based; a pass does not prove arbitrary private data
or an unknown credential format is absent.
