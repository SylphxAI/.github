import base64
from copy import deepcopy
from email.message import Message
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import reconcile_repo_baseline as baseline
import pytest
import yaml


REPO = "example/project"
ROOT = f"repos/{REPO}"
CLASSIC = f"{ROOT}/branches/main/protection"
EFFECTIVE = f"{ROOT}/rules/branches/main?per_page=100"
RULESET = f"{ROOT}/rulesets/7"


class Fake:
    def __init__(self):
        self.calls = []
        self.data = {
            ROOT: {"full_name": REPO, "default_branch": "main", "archived": False, "fork": False,
                   "disabled": False, "permissions": {"admin": True}, "allow_auto_merge": False,
                   "delete_branch_on_merge": False, "has_wiki": True},
            f"{ROOT}/properties/values": [{"property_name": "sylphx_delivery", "value": "managed"}],
            f"{ROOT}/branches/main": {"name": "main", "protected": True},
            CLASSIC: {"required_pull_request_reviews": {"required_approving_review_count": 2,
                       "require_code_owner_reviews": True, "require_last_push_approval": True,
                       "dismiss_stale_reviews": True, "dismissal_restrictions": {"users": ["example"]}},
                      "required_status_checks": {"strict": True, "checks": [{"context": "ci", "app_id": 42}]},
                      "allow_force_pushes": {"enabled": False}},
            RULESET: {"id": 7, "source": REPO, "source_type": "Repository", "name": "protect",
                      "target": "branch", "enforcement": "active", "bypass_actors": [{"actor_id": 1}],
                      "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
                      "rules": [{"type": "pull_request", "parameters": {"required_approving_review_count": 1,
                                 "require_extra_approval_for_unattributed_changes": True,
                                 "required_review_thread_resolution": True, "allowed_merge_methods": ["squash"]}},
                                {"type": "non_fast_forward"}, {"type": "required_status_checks", "parameters": {
                                    "required_status_checks": [{"context": "ci", "integration_id": 42}],
                                    "strict_required_status_checks_policy": True}}]},
        }
        self.inherited = []
        self.status = {}
        self.corrupt = False

    def request(self, path, method="GET", body=None, etag=None, paginate=False):
        self.calls.append((path, method, deepcopy(body), etag))
        if (path, method) in self.status:
            return baseline.Response(*self.status[path, method], None)
        if path == EFFECTIVE:
            assert paginate
        if path == EFFECTIVE and RULESET not in self.data:
            return baseline.Response(200, deepcopy(self.inherited), None)
        if path == EFFECTIVE:
            rule = {**self.data[RULESET]["rules"][0], "ruleset_id": 7,
                    "ruleset_source_type": "Repository", "ruleset_source": REPO}
            return baseline.Response(200, deepcopy(([rule] if RULESET in self.data else []) + self.inherited), None)
        if method == "PATCH" and path == f"{CLASSIC}/required_pull_request_reviews":
            self.data[CLASSIC]["required_pull_request_reviews"].update(body)
        elif method == "PATCH":
            self.data[path].update(body)
        elif method == "PUT":
            self.data[path].update(deepcopy(body))
            if self.corrupt:
                self.data[path]["rules"].pop()
        return baseline.Response(200, deepcopy(self.data.get(path, {})), '"etag"')

    @property
    def writes(self):
        return [call for call in self.calls if call[1] != "GET"]


def test_dry_run_and_apply_preserve_unrelated_settings_and_are_idempotent():
    api = Fake()
    before = deepcopy(api.data)
    assert baseline.reconcile(api, REPO)["status"] == "drift"
    assert api.writes == []
    with patch.object(baseline.time, "sleep"):
        assert baseline.reconcile(api, REPO, apply=True)["status"] == "applied"
        assert len(api.writes) == 3
        assert baseline.reconcile(api, REPO, apply=True)["status"] == "ok"
    classic, ruleset, settings = api.writes
    assert classic[0:3] == (f"{CLASSIC}/required_pull_request_reviews", "PATCH", baseline.CLASSIC_REVIEW)
    assert ruleset[0:2] == (RULESET, "PUT") and ruleset[3] == '"etag"'
    assert settings[0:3] == (ROOT, "PATCH", baseline.DEFAULTS)
    before[ROOT].update(baseline.DEFAULTS)
    before[CLASSIC]["required_pull_request_reviews"].update(baseline.CLASSIC_REVIEW)
    before[RULESET]["rules"][0]["parameters"].update(
        required_approving_review_count=0, require_extra_approval_for_unattributed_changes=False)
    assert api.data == before


def test_no_protection_is_not_created():
    api = Fake()
    del api.data[RULESET]
    api.status[CLASSIC, "GET"] = (404, {"message": "Branch not protected"})
    with patch.object(baseline.time, "sleep"):
        assert baseline.reconcile(api, REPO, apply=True)["status"] == "applied"
    assert [(path, method) for path, method, *_ in api.writes] == [(ROOT, "PATCH")]


def test_archived_delivered_and_unknown_authority_are_never_written():
    for reason in ("archived", "delivered", "unknown_admin", "unknown_delivery", "unknown_classic"):
        api = Fake()
        if reason == "archived":
            api.data[ROOT]["archived"] = True
        elif reason == "delivered":
            api.data[f"{ROOT}/properties/values"][0]["value"] = "delivered"
        elif reason == "unknown_admin":
            del api.data[ROOT]["permissions"]
        elif reason == "unknown_delivery":
            api.status[f"{ROOT}/properties/values", "GET"] = (403, None)
        elif reason == "unknown_classic":
            api.status[CLASSIC, "GET"] = (404, {"message": "Not Found"})
        result = baseline.reconcile(api, REPO, apply=True)
        assert result["status"] == ("error" if reason.startswith("unknown") else "skipped")
        assert api.writes == []


@pytest.mark.parametrize("missing", ["merge_default", "branch_access"])
def test_unreadable_merge_default_or_branch_never_writes(missing):
    api = Fake()
    if missing == "merge_default":
        del api.data[ROOT]["allow_auto_merge"]
    else:
        api.status[f"{ROOT}/branches/main", "GET"] = (403, None)
    result = baseline.reconcile(api, REPO, apply=True)
    assert result["status"] == "error"
    assert api.writes == []


def test_mixed_refs_and_inherited_drift_are_reported_without_wrong_authority_write():
    api = Fake()
    api.data[RULESET]["conditions"]["ref_name"]["include"].append("refs/heads/releases/*")
    api.inherited = [{"type": "pull_request", "ruleset_id": 123, "ruleset_source_type": "Enterprise",
                      "parameters": {"required_approving_review_count": 2}}]
    with patch.object(baseline.time, "sleep"):
        result = baseline.reconcile(api, REPO, apply=True)
    assert result["status"] == "blocked"
    assert {f.get("reason") for f in result["findings"] if f["status"] == "blocked"} == {
        "inherited_authority", "not_proven_default_branch_only"}
    assert [method for _, method, *_ in api.writes] == ["PATCH", "PATCH"]


def test_redacted_ruleset_and_changed_readback_fail_closed():
    api = Fake()
    del api.data[RULESET]["bypass_actors"]
    assert baseline.reconcile(api, REPO, apply=True)["status"] == "error"
    assert api.writes == []
    api = Fake()
    api.corrupt = True
    with patch.object(baseline.time, "sleep"):
        assert baseline.reconcile(api, REPO, apply=True)["reason"] == "ruleset_readback_differs"
    assert not any(path == ROOT for path, *_ in api.writes)


def test_fresh_read_prevents_overwrite_of_concurrent_ruleset_edit():
    api = Fake()
    request = api.request

    def changed(path, method="GET", body=None, etag=None, paginate=False):
        if path == RULESET and method == "GET" and any(p == RULESET for p, *_ in api.calls):
            api.data[RULESET]["name"] = "renamed by another tool"
        return request(path, method, body, etag, paginate)

    api.request = changed
    with patch.object(baseline.time, "sleep"):
        assert baseline.reconcile(api, REPO, apply=True)["reason"] == "ruleset_changed_since_discovery"
    assert not any(method == "PUT" for _, method, *_ in api.writes)


def test_public_summary_has_no_identifiers_and_cli_defaults_to_dry_run(capsys, app_env):
    api = Fake()
    api.data["installation/repositories?per_page=100"] = [{"full_name": REPO}]
    with patch.object(baseline, "app_installations", return_value={"example": api}):
        assert baseline.main(["--org", "example", "--summary-only"]) == 0
    output = capsys.readouterr().out
    assert '"mode": "dry-run"' in output
    assert "example" not in output and "project" not in output
    assert api.writes == []


@pytest.fixture
def app_env(monkeypatch):
    # Synthetic input; only the isolated signing test uses generated key material.
    values = {"REPO_BASELINE_ORGS": "example another", "REPO_BASELINE_APP_ID": "1234",
              "REPO_BASELINE_APP_PRIVATE_KEY": "private_key_sentinel"}
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return values


def test_jwt_header_claims_and_rs256_signature(tmp_path, monkeypatch):
    # No PEM fixture on disk or in source. Signature verification uses only the public key.
    pem = subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048"],
                         capture_output=True, check=True, timeout=10).stdout
    pub = tmp_path / "public.pem"
    pub.write_bytes(subprocess.run(["openssl", "pkey", "-pubout"], input=pem, capture_output=True,
                                   check=True, timeout=10).stdout)
    now = 1_795_000_000
    monkeypatch.setattr(baseline.time, "time", lambda: now)
    head, claims, sig = baseline.app_jwt("1234", pem.decode()).split(".")

    def decode(part):
        assert "=" not in part
        return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))

    assert json.loads(decode(head)) == {"alg": "RS256", "typ": "JWT"}
    assert json.loads(decode(claims)) == {"iat": now - 60, "exp": now + 480, "iss": "1234"}
    signature = tmp_path / "signature.bin"
    signature.write_bytes(decode(sig))
    result = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(pub), "-signature", str(signature)],
                            input=f"{head}.{claims}".encode(), capture_output=True, timeout=10)
    assert result.returncode == 0 and result.stdout == b"Verified OK\n"


class AppWire:
    def __init__(self):
        self.calls = []
        self.responses = {}
        for org, ident in (("example", 10), ("another", 20)):
            self.responses[f"orgs/{org}/installation"] = (200, {
                "id": ident, "account": {"login": org.upper()}, "target_type": "Organization",
                "repository_selection": "all",
            })
            self.responses[f"app/installations/{ident}/access_tokens"] = (201, {
                "token": f"installation_{org}", "permissions": {"administration": "write", "metadata": "read", "contents": "read"},
            })

    def request(self, token, path, method="GET", body=None, etag=None, *, paginate=False):
        self.calls.append((token, path, method, body, paginate))
        status, data = self.responses[path]
        return baseline.Response(status, deepcopy(data), None)


def test_multi_org_credentials_and_repository_isolation(app_env, monkeypatch, capsys):
    wire = AppWire()
    orgs = ("example", "another")
    monkeypatch.setattr(baseline, "app_jwt", lambda *args: "jwt_sentinel")
    real_request = wire.request

    def request(token, path, *args, **kwargs):
        if path == "installation/repositories?per_page=100":
            org = dict(zip(("installation_example", "installation_another"), orgs))[token]
            wire.responses[path] = (200, [{"full_name": f"{org}/project"}])
        elif path in ("repos/example/project", "repos/another/project"):
            wire.responses[path] = (200, {"full_name": path.removeprefix("repos/"), "archived": True})
        return real_request(token, path, *args, **kwargs)

    monkeypatch.setattr(baseline, "github_request", request)
    assert baseline.main(["--summary-only"]) == 0
    permissions = {"permissions": {"administration": "write", "metadata": "read", "contents": "read"}}
    assert wire.calls[:4] == [
        ("jwt_sentinel", "orgs/example/installation", "GET", None, False),
        ("jwt_sentinel", "app/installations/10/access_tokens", "POST", permissions, False),
        ("jwt_sentinel", "orgs/another/installation", "GET", None, False),
        ("jwt_sentinel", "app/installations/20/access_tokens", "POST", permissions, False),
    ]
    assert wire.calls[4:] == [
        ("installation_example", "installation/repositories?per_page=100", "GET", None, True),
        ("installation_example", "repos/example/project", "GET", None, False),
        ("installation_another", "installation/repositories?per_page=100", "GET", None, True),
        ("installation_another", "repos/another/project", "GET", None, False),
    ]
    assert json.loads(capsys.readouterr().out)["summary"]["repositories"] == {"skipped": 2}
    client = baseline.GitHub("example", "installation_example")
    for path in ("repos/another/project", "orgs/another/installation", "app/installations/20/access_tokens",
                 "repos/example/../another/project"):
        with pytest.raises(baseline.Unknown, match="installation_request_out_of_scope"):
            client.request(path)
    assert len(wire.calls) == 8


@pytest.mark.parametrize(("path", "response", "reason"), [
    ("orgs/another/installation", (404, {}), "app_installation_http_404"),
    ("orgs/another/installation", (200, {"id": 20}), "app_installation_identity_invalid"),
    ("orgs/another/installation", (200, {"id": 20, "account": {"login": "another"},
                                      "target_type": "Organization", "repository_selection": "selected"}),
     "app_installation_requires_all_repositories"),
    ("app/installations/20/access_tokens", (403, {}), "app_token_exchange_http_403"),
    ("app/installations/20/access_tokens", (201, {"token": "installation_another",
                                                "permissions": {"administration": "write", "metadata": "read",
                                                                "contents": "write"}}),
     "app_token_permissions_invalid"),
])
def test_bad_later_installation_refuses_fleet_before_repository_calls(app_env, monkeypatch, capsys,
                                                                      path, response, reason):
    wire = AppWire()
    wire.responses[path] = response
    monkeypatch.setattr(baseline, "app_jwt", lambda *args: "jwt_sentinel")
    monkeypatch.setattr(baseline, "github_request", wire.request)
    assert baseline.main(["--apply"]) == 1
    assert any(call[1] == "app/installations/10/access_tokens" for call in wire.calls)
    assert all(call[0] == "jwt_sentinel" for call in wire.calls)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"Repository baseline authentication failed: {reason}\n"


@pytest.mark.parametrize("missing", ["REPO_BASELINE_APP_ID", "REPO_BASELINE_APP_PRIVATE_KEY", "REPO_BASELINE_ORGS"])
def test_configuration_refuses_missing_secrets_in_cli_and_workflow(app_env, monkeypatch, capsys, missing):
    monkeypatch.setenv(missing, "")
    monkeypatch.setenv("GH_TOKEN", "ambient_token_must_not_be_used")
    with patch.object(baseline, "app_installations") as preflight:
        with pytest.raises(SystemExit) as exc:
            baseline.main(["--require-fleet"])
        assert exc.value.code == 2
        preflight.assert_not_called()
    assert "private_key_sentinel" not in capsys.readouterr().err

    repo_root = Path(__file__).resolve().parents[1]
    job = yaml.safe_load((repo_root / ".github/workflows/repository-baseline.yml").read_text())["jobs"]["reconcile"]
    assert job["environment"] == "repository-baseline"
    step = next(step for step in job["steps"] if step.get("name") == "Reconcile repository baseline")
    assert set(step["env"]) == set(app_env) | {"APPLY_BASELINE"}
    result = subprocess.run(["bash", "-c", step["run"]], cwd=repo_root,
                            env={**os.environ, "APPLY_BASELINE": "false"}, capture_output=True, text=True)
    assert result.returncode == 2
    assert result.stdout == "::error::Configure the dedicated repository baseline environment secrets first.\n"
    assert result.stderr == ""


def test_signer_failure_never_displays_private_key_or_signer_output(app_env, capsys):
    key = app_env["REPO_BASELINE_APP_PRIVATE_KEY"]
    with patch.object(baseline.subprocess, "run", return_value=subprocess.CompletedProcess(
            ["openssl"], 1, stdout=key.encode(), stderr=key.encode())) as run:
        assert baseline.main([]) == 1
        args, kwargs = run.call_args
        assert kwargs["input"] == key.encode()
        assert key not in repr(args) and key not in repr(kwargs["env"])
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "Repository baseline authentication failed: app_jwt_signing_failed\n"


class WireResponse(io.BytesIO):
    def __init__(self, body, code=200, **headers):
        super().__init__(body)
        self.code = code
        self.headers = Message()
        for key, val in headers.items():
            self.headers[key] = val


def test_http_pagination_stays_same_scope_and_keeps_bearer_out_of_url():
    pages = [WireResponse(b'{"repositories":[{"full_name":"example/a"}]}',
                          Link='<https://untrusted.invalid/repos/another/b>; rel="next"'),
             WireResponse(b'{"repositories":[{"full_name":"example/b"}]}')]
    with patch.object(baseline, "build_opener") as opener:
        opened = opener.return_value.open
        opened.side_effect = pages
        assert baseline.inventory(baseline.GitHub("example", "installation_token_sentinel"), "example") == [
            "example/a", "example/b"]
    assert [c.args[0].full_url for c in opened.call_args_list] == [
        f"https://api.github.com/installation/repositories?per_page=100&page={i}" for i in (1, 2)]
    assert all(c.args[0].get_header("Authorization") == "Bearer installation_token_sentinel"
               for c in opened.call_args_list)
    assert baseline.NoRedirect().redirect_request(None, None, None, None, None, "https://untrusted.invalid") is None


@pytest.mark.parametrize("status", [200, 404])
def test_http_auth_errors_never_display_provider_body_jwt_or_installation_token(app_env, monkeypatch, capsys,
                                                                                status):
    jwt, token = "jwt_sentinel", "installation_token_sentinel"
    monkeypatch.setattr(baseline, "app_jwt", lambda *args: jwt)
    body = f"malformed payload: {jwt}, {token}, {app_env['REPO_BASELINE_APP_PRIVATE_KEY']}".encode()
    with patch.object(baseline, "build_opener") as opener:
        opener.return_value.open.return_value = WireResponse(body, status)
        assert baseline.main([]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "Repository baseline authentication failed: api_response_unreadable\n"
