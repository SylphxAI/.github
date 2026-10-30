# Registry pull credentials for CI

A job with `permissions: id-token: write` can exchange its own GitHub OIDC token
for an exact repository pull grant through the ordinary registry token service.
Its numeric repository identity, workflow, event/ref and reusable workflow pin
must first be declared in Foundation's `github-oidc-readers.json`. The helper
does not create authority. An undeclared job fails at the exchange.

Check out this shared repository at a full commit SHA containing the helper,
then run:

```sh
python3 "$SHARED_CHECKOUT/scripts/mint-registry-auth.py" \
  --identity github-oidc --actions pull \
  --image registry.sylphx.com/namespace/image \
  --run-id "$GITHUB_RUN_ID" --output-dir "$RUNNER_TEMP/registry-read"
skopeo inspect --authfile "$RUNNER_TEMP/registry-read/skopeo-auth.json" \
  docker://registry.sylphx.com/namespace/image@sha256:FULL_DIGEST
rm -rf "$RUNNER_TEMP/registry-read"
```

Use a fresh private directory and arrange its removal on failure as well. The
helper sets directory mode 0700 and both files to 0600. `config.json` contains
the Docker identity token. `skopeo-auth.json` contains the registry client's
Basic encoding of `oauth2accesstoken:<registry-token>`. Pass the latter file to
Skopeo; never print or export its contents. The helper prints paths and timing
only. It refuses a returned token with extra repositories, duplicate actions,
push authority, a foreign audience or lifetime over 900 seconds.

The default `--actions pull,push` retains the publisher contract. A pull grant
does not authorize publication and does not attest a digest; callers pin and
verify the exact digest and required bytes. Delete read credentials before
running compilers or minting output publication credentials. Fork admission is
controlled by the caller's GitHub execution policy and head-repository check;
the base repository's OIDC identity does not independently prove the PR head.
