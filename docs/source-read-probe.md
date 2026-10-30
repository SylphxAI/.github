# Signed source-read probe

`source-read-probe` verifies a workflow's ordinary Access → Broker → GitHub
source-read route without compiling, signing, checking out source, or publishing
anything. Callers pin the action by its published commit SHA and grant
`id-token: write` to the probe job only.

1. Use `mode: bootstrap` once after the exact CI declaration is serving. Its
   receipt reports the stable `key.status.principal`; the policy owner can then
   add that principal to a read-only TrustPolicy. It returns no credential.
2. Use `mode: read` after the policy is ready, with at most four exact sources:

   ```yaml
   with:
     ci-name: source-read
     mode: read
     targets: >-
       [{"repository":"example/source","commit":"0123456789012345678901234567890123456789",
         "trust_policy":"orgs/org_example/trust_policies/source-read"}]
   ```

The probe honors Access's `active_after_ms`, first verifies that requesting
`contents: write` is refused with `400 INVALID_FIELD`, then requests only
`contents: read` for one repository. It checks the exact commit through GitHub's
API and `git init` + `fetch --depth=1` + `rev-parse FETCH_HEAD`. Git has no saved
remote, token URL, credential file, checkout, inherited tracing or global
credential helper. Its temporary object directory is removed on exit.

Optional `denied-policy` names an **existing same-org policy which does not grant
this subject**; that exchange must return `403`. The policy owner supplies it.
To check a disallowed ref or event, invoke the auth-only caller on that ref/event
without extending its declaration: Access must refuse bootstrap. A denied
workflow run is negative evidence, not a positive read receipt. Never add a
pull-request/fork grant just to make a probe pass.

The output `receipt` is a local JSON file with request UTC, endpoint, status,
request/CF identifiers, principal, token-exchange resource names and exact source
pins. It contains no OIDC, Access or provider token, and is written before each
request and updated on its first response. There are no automatic HTTP retries;
an unknown receipt requires investigation before a new dispatch. Non-JSON edge
denials fail closed. The HTTP client uses a truthful explicit User-Agent so edge
bot admission is not confused with application authorization.

Bootstrap proves identity issuance only. Positive read acceptance requires both
exact commit GET and depth-one `FETCH_HEAD` equality for every source. Neither
proves a build, native execution, device authorization or store access. Negative
responses prove no token/exchange **was returned**; only the Broker owner can
independently verify that no exchange row was created.
