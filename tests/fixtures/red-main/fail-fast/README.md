# Force-cancel receipt

Provider metadata from SylphxAI/cloud isolated self-hosted push probe
42287638550, first read 2026-11-06 20:22–20:30 UTC. Linked check annotations were
read again at 20:32:23 UTC. Files are the run, counted jobs envelope, required
job's linked check and its annotations; they contain no repository source or
credentials. Job/check 128132487926 failed `Synthetic required failure`, then
successfully ran `Cancel failed current run`. The other job was cancelled while
running; no aggregate job was created.

Positive unit tests adapt only `head_branch` to `main` and `path` to
`.github/workflows/verify.yml`. Probe origin is tested as ineligible unchanged.
Run/job/check/attempt/SHA/suite linkage and the provider's rewritten conclusions,
step order and annotation are retained. The temporary probe branch is deleted;
these fixtures preserve the measured cancellation contract.
