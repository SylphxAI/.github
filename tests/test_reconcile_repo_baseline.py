from copy import deepcopy
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import reconcile_repo_baseline as baseline


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


def test_public_summary_has_no_identifiers_and_cli_defaults_to_dry_run(capsys):
    api = Fake()
    api.data["orgs/example/repos?type=all&per_page=100"] = [{"full_name": REPO}]
    with patch.object(baseline, "GitHub", return_value=api):
        assert baseline.main(["--org", "example", "--summary-only"]) == 0
    output = capsys.readouterr().out
    assert '"mode": "dry-run"' in output
    assert "example" not in output and "project" not in output
    assert api.writes == []
