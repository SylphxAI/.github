# Static publication transaction

Publish a complete, already-proven static release from an authenticated deployment
job. The caller supplies the exact package SHA256 and required proof kind→SHA256
mapping from its successful CI receipt. Those identities must come from a trusted
deployment declaration. This filesystem primitive creates no identity, endpoint,
registry permission or GitHub token.

`release.json` binds source repository/SHA/run/attempt, a complete file population,
each file's bytes/SHA256, a subject hash, proof files, the immutable release
directory, the replacement pointer bytes, and the expected prior pointer SHA256.
Proofs must name that exact subject/source, have their configured immutable hash,
and report a passing verdict. A product defines the actual build and engine-load
proof; this neutral mechanism contains no game-data, announcement or pricing rule.

`publish.py:publish` stages exact files, checks the immutable directory on a retry,
calls the public candidate verifier, replaces only the expected current pointer,
and calls the installed serving verifier. The latter must read the public pointer
and selected artifact bytes. A failed serving check restores the captured parent
pointer only while this release still owns it, then verifies rollback readback.
A newer successful publication always wins over a late failure from its predecessor.

Keep durable journal/previous-pointer files outside the served root. The journal
owns an accepted incomplete attempt across process crashes, including a crash
between pointer replacement and receipt commit. A short transaction coordinates
only the selected publication pointer. Source authoring and unrelated publication
targets can proceed independently. A terminal refused or rolled-back package is
not republished by retrying it. A rollback whose public readback has not completed
remains `ROLLBACK_SWITCHED`; a continuation reconciles that rollback.

New serving files/directories use `0644`/`0755`, even when the executor has a
private umask. Pointer activation and rollback preserve the captured pointer's
owner, group and permission bits; refusal to preserve that metadata stops before
replacement. Existing directories keep their permissions. Journal and backup
files remain private (`0600`) outside the serving root.

The deployment adapter must ensure that all pointer writers use this transaction,
the output filesystem supports durable rename, the source package cannot change
while being read, and the public verifier reads the configured serving route.
Do not mount database or backup storage. Do not infer game-session continuity
from these static-file tests; the mechanism never restarts a runtime process.

The implementation is an on-demand job with no resident service. Work is linear
in the package population. A complete release copy currently uses memory up to
the largest single file and disk for each retained release; at 10x/100x larger
artifacts those are the first resource bounds. Retention and deletion belong to
the deployment owner's explicit data lifecycle; this primitive never deletes a
previous accepted release. An incremental/deduplicated artifact representation
requires a verified immutable source contract before it can replace full copies.

Validation:

```sh
python3 -m unittest discover -s tests -p 'test_static_publication.py' -v
```

Fifteen cases exercise exact installation, corrupt/refused inputs, stale parents,
candidate failure, installed failure and rollback, out-of-order completion,
unchanged unrelated state, immutable-directory refusal, path/symlink refusal, and
process crashes before/after pointer switching and during rollback, public read
permissions under a private umask, and pointer permissions through rollback. They are
filesystem acceptance cases; a consuming deployment must supply actual public
readback and runtime load evidence before claiming installed delivery.
