#!/usr/bin/env python3
"""Audit or converge repository defaults and existing default-branch approval gates."""

from __future__ import annotations

import argparse
import base64
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from fnmatch import fnmatchcase
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
OWNER = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,38})")
REPO = re.compile(r"[a-zA-Z0-9_.-]{1,100}")
DEFAULTS = {"allow_auto_merge": True, "delete_branch_on_merge": True}
RULESET_REVIEW = {
    "required_approving_review_count": 0,
    "require_code_owner_review": False,
    "require_last_push_approval": False,
    "require_extra_approval_for_unattributed_changes": False,
}
CLASSIC_REVIEW = {
    "required_approving_review_count": 0,
    "require_code_owner_reviews": False,
    "require_last_push_approval": False,
}
PUT_FIELDS = {"name", "target", "enforcement", "bypass_actors", "conditions", "rules"}
RESPONSE_FIELDS = {"id", "node_id", "source", "source_type", "created_at", "updated_at",
                   "current_user_can_bypass", "_links"}


class Unknown(Exception):
    """Safe diagnostic: never contains response bodies, credentials or API paths."""


@dataclass
class Response:
    status: int
    data: object
    etag: str | None


API_ORIGIN = "https://api.github.com/"
INSTALLATION_REPOSITORIES = "installation/repositories?per_page=100"
APP_PERMISSIONS = {"administration": "write", "metadata": "read", "contents": "read"}


class NoRedirect(HTTPRedirectHandler):
    # Never carry an authorization header to a different org or origin.
    def redirect_request(self, *args, **kwargs):
        return None


def github_request(token, path, method="GET", body=None, etag=None, *, paginate=False):
    opener = build_opener(ProxyHandler({}), NoRedirect())
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    if etag:
        headers["If-Match"] = etag
    encoded = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        encoded = json.dumps(body).encode()
    pages = []
    page = 1
    while True:
        # Use only the presence of rel=next; never follow a server-provided URL.
        query = ("&" if "?" in path else "?") + f"page={page}" if paginate else ""
        try:
            request = Request(API_ORIGIN + path + query, data=encoded, headers=headers, method=method)
            try:
                response = opener.open(request, timeout=30)
            except HTTPError as exc:
                response = exc
            with response:
                status = response.code
                if (status == 404 and not paginate) or 200 <= status < 300:
                    raw = response.read()
                    data = json.loads(raw) if raw.strip() else None
                else:
                    data = None
                etag = response.headers.get("ETag")
                link = response.headers.get("Link", "")
        except (OSError, ValueError, HTTPException):
            # No provider body, request metadata, or exception chain in diagnostics.
            raise Unknown("api_response_unreadable") from None
        if not paginate or not 200 <= status < 300:
            return Response(status, data, etag)
        if path == INSTALLATION_REPOSITORIES:
            data = data.get("repositories") if isinstance(data, dict) else None
        if not isinstance(data, list):
            raise Unknown("pagination_response_invalid")
        pages.extend(data)
        if not re.search(r';\s*rel="next"', link):
            return Response(status, pages, None)
        page += 1


def app_jwt(app_id, private_key):
    def b64(raw):
        return base64.urlsafe_b64encode(raw).rstrip(b"=")

    now = int(time.time())
    claims = {"iat": now - 60, "exp": now + 480, "iss": app_id}
    message = b64(b'{"alg":"RS256","typ":"JWT"}') + b"." + b64(json.dumps(claims).encode())
    try:
        # Only the public JWT input goes in the anonymous file; the key goes via stdin,
        # never a command argument, child environment, or named file.
        with tempfile.TemporaryFile() as payload:
            payload.write(message)
            payload.seek(0)
            fd = payload.fileno()
            result = subprocess.run(
                ["openssl", "dgst", "-sha256", "-sign", "/dev/stdin", "-sigopt",
                 "rsa_padding_mode:pkcs1", f"/dev/fd/{fd}"], input=private_key.encode(),
                capture_output=True, env={"PATH": os.defpath}, pass_fds=(fd,), timeout=10,
            )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise Unknown("app_jwt_signing_failed") from None
    if result.returncode or not result.stdout:
        raise Unknown("app_jwt_signing_failed")
    return (message + b"." + b64(result.stdout)).decode("ascii")


class GitHub:
    def __init__(self, org, token):
        self.org = org.lower()
        self._token = token

    def request(self, path, method="GET", body=None, etag=None, paginate=False):
        if path != INSTALLATION_REPOSITORIES or method != "GET":
            resource, _, _ = path.partition("?")
            parts = resource.split("/")
            if (len(parts) < 3 or parts[0] != "repos" or parts[1].lower() != self.org
                    or not REPO.fullmatch(parts[2]) or any(part in (".", "..") for part in parts)):
                raise Unknown("installation_request_out_of_scope")
        return github_request(self._token, path, method, body, etag, paginate=paginate)


def app_installations(orgs, app_id, private_key):
    jwt = app_jwt(app_id, private_key)
    clients = {}
    for org in orgs:
        install = require(github_request(jwt, f"orgs/{org}/installation"), dict, "app_installation")
        account = install.get("account")
        install_id = install.get("id")
        if (not isinstance(account, dict) or not isinstance(account.get("login"), str)
                or account["login"].lower() != org.lower() or install.get("target_type") != "Organization"
                or type(install_id) is not int or install_id <= 0):
            raise Unknown("app_installation_identity_invalid")
        if install.get("repository_selection") != "all":
            raise Unknown("app_installation_requires_all_repositories")
        exchange = require(github_request(jwt, f"app/installations/{install_id}/access_tokens", "POST",
                                          {"permissions": APP_PERMISSIONS}), dict, "app_token_exchange")
        token = exchange.get("token")
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", token):
            raise Unknown("app_token_exchange_invalid")
        if exchange.get("permissions") != APP_PERMISSIONS:
            raise Unknown("app_token_permissions_invalid")
        clients[org] = GitHub(org, token)
    return clients

def require(response, expected, label):
    if not 200 <= response.status < 300:
        raise Unknown(f"{label}_http_{response.status}")
    if not isinstance(response.data, expected):
        raise Unknown(f"{label}_invalid")
    return response.data


def repo_state(api, path, repo):
    response = api.request(path)
    meta = require(response, dict, "repository")
    if str(meta.get("full_name", "")).lower() != repo.lower():
        raise Unknown("repository_identity_changed")
    flags = ("archived", "fork", "disabled")
    if any(meta.get(flag) is True for flag in flags):
        return response, next(flag for flag in flags if meta.get(flag) is True)
    if any(meta.get(flag) is not False for flag in flags):
        raise Unknown("repository_eligibility_unknown")
    if not isinstance(meta.get("permissions"), dict) or meta["permissions"].get("admin") is not True:
        raise Unknown("repository_admin_not_confirmed")
    properties = require(api.request(f"{path}/properties/values"), list, "delivery")
    if any(not isinstance(p, dict) or not isinstance(p.get("property_name"), str) or "value" not in p
           for p in properties):
        raise Unknown("delivery_response_invalid")
    delivery = [p["value"] for p in properties if p["property_name"] == "sylphx_delivery"]
    if any(value == "delivered" for value in delivery):
        return response, "delivered"
    if len(delivery) > 1 or any(value not in (None, "managed") for value in delivery):
        raise Unknown("delivery_value_unknown")
    return response, None


def diff(values, desired, *, optional_defaults=False):
    changes = {}
    for key, want in desired.items():
        got = values.get(key, want if optional_defaults else None)
        if type(got) is not type(want) or (type(got) is int and got < 0):
            raise Unknown("managed_field_unreadable")
        if got != want:
            changes[key] = {"before": got, "after": want}
    return changes


def update_fields(changes):
    return {key: entry["after"] for key, entry in changes.items()}


def classic(api, path):
    response = api.request(path)
    if (response.status == 404 and isinstance(response.data, dict)
            and response.data.get("message") == "Branch not protected"):
        return None
    data = require(response, dict, "classic_protection")
    review = data.get("required_pull_request_reviews")
    if review is not None and not isinstance(review, dict):
        raise Unknown("classic_reviews_invalid")
    return response


def scope_is_default(conditions, branch):
    if not isinstance(conditions, dict) or set(conditions) != {"ref_name"}:
        return False
    refs = conditions["ref_name"]
    if not isinstance(refs, dict) or set(refs) != {"include", "exclude"}:
        return False
    include = refs["include"]
    return (refs["exclude"] == [] and isinstance(include, list) and bool(include)
            and all(ref in ("~DEFAULT_BRANCH", f"refs/heads/{branch}") for ref in include))


def rule_body(data, repo, rid):
    if (data.get("source_type") != "Repository" or str(data.get("source", "")).lower() != repo.lower()
            or data.get("id") != rid):
        raise Unknown("ruleset_authority_unknown")
    if not PUT_FIELDS <= data.keys() or data.keys() - PUT_FIELDS - RESPONSE_FIELDS:
        raise Unknown("ruleset_writable_state_incomplete")
    body = {key: data[key] for key in PUT_FIELDS}
    if (not isinstance(body["bypass_actors"], list) or not isinstance(body["conditions"], dict)
            or not isinstance(body["rules"], list)
            or any(not isinstance(rule, dict) or not isinstance(rule.get("type"), str)
                   for rule in body["rules"])):
        raise Unknown("ruleset_writable_state_incomplete")
    return body


def review_rule(body):
    reviews = [rule for rule in body["rules"] if rule["type"] == "pull_request"]
    if len(reviews) != 1 or not isinstance(reviews[0].get("parameters"), dict):
        raise Unknown("ruleset_pull_request_rule_changed")
    return reviews[0]


def verify_write(api, path, method, body, etag):
    response = api.request(path, method, body, etag)
    if not 200 <= response.status < 300:
        raise Unknown(f"{method.lower()}_http_{response.status}")
    # GitHub recommends spacing mutative REST calls at least one second apart.
    time.sleep(1)


def reconcile(api, repo, apply=False):
    row = {"repo": repo, "findings": []}
    findings = row["findings"]
    path = f"repos/{repo}"

    def record(kind, changes, after=None, **identity):
        findings.append({"kind": kind, "status": after or ("drift" if changes else "ok"),
                         "changes": changes, **identity})
        return findings[-1]

    def guard(branch):
        fresh, skip = repo_state(api, path, repo)
        if skip or fresh.data.get("default_branch") != branch:
            raise Unknown("repository_eligibility_changed")
        return fresh

    try:
        meta, skip = repo_state(api, path, repo)
        if skip:
            return {**row, "status": "skipped", "reason": skip}
        branch = meta.data.get("default_branch")
        if not isinstance(branch, str) or not branch:
            raise Unknown("default_branch_unknown")
        row["default_branch"] = branch
        defaults = record("repository_settings", diff(meta.data, DEFAULTS))
        encoded = quote(branch, safe="")
        branch_path = f"{path}/branches/{encoded}"
        protection_path = f"{branch_path}/protection"
        branch_response = api.request(branch_path)
        if (branch_response.status == 404 and meta.data.get("size") == 0
                and isinstance(branch_response.data, dict)
                and branch_response.data.get("message") == "Branch not found"):
            record("branch_protection", {}, "absent", reason="no_default_branch_ref")
            base, effective = None, []
        else:
            if require(branch_response, dict, "branch").get("name") != branch:
                raise Unknown("default_branch_identity_changed")
            base = classic(api, protection_path)
            effective = require(api.request(f"{path}/rules/branches/{encoded}?per_page=100", paginate=True),
                                list, "effective_rules")
            if not all(isinstance(rule, dict) and isinstance(rule.get("type"), str) for rule in effective):
                raise Unknown("effective_rules_invalid")
            if base is None and not effective:
                record("branch_protection", {}, "absent", reason="no_existing_protection")

        work = []
        if base:
            review = base.data.get("required_pull_request_reviews")
            if review is None:
                record("classic_reviews", {}, "absent")
            else:
                checks = diff(review, CLASSIC_REVIEW, optional_defaults=True)
                result = record("classic_reviews", checks)
                if checks:
                    work.append(("classic", result, None))

        seen = set()
        for rule in effective:
            if rule["type"] != "pull_request":
                continue
            rid = rule.get("ruleset_id")
            source = rule.get("ruleset_source_type")
            parameters = rule.get("parameters")
            if (type(rid) is not int or rid <= 0 or rid in seen or not isinstance(parameters, dict)
                    or source not in ("Repository", "Organization", "Enterprise")):
                raise Unknown("effective_review_scope_unknown")
            seen.add(rid)
            changes = diff(parameters, RULESET_REVIEW, optional_defaults=True)
            if source != "Repository":
                record("ruleset_reviews", changes, "blocked" if changes else "inherited_ok",
                       ruleset_id=rid, source_type=source, reason="inherited_authority")
            elif str(rule.get("ruleset_source", "")).lower() != repo.lower():
                raise Unknown("ruleset_authority_unknown")
            elif not changes:
                record("ruleset_reviews", {})
            else:
                rule_path = f"{path}/rulesets/{rid}"
                current = require(api.request(rule_path), dict, "ruleset")
                # A read-only scope determination does not require unredacted bypass actors.
                if (current.get("source_type") != "Repository"
                        or str(current.get("source", "")).lower() != repo.lower() or current.get("id") != rid):
                    raise Unknown("ruleset_authority_unknown")
                if (current.get("target") != "branch" or current.get("enforcement") != "active"
                        or not scope_is_default(current.get("conditions"), branch)):
                    record("ruleset_reviews", changes, "blocked", ruleset_id=rid,
                           reason="not_proven_default_branch_only")
                    continue
                current_body = rule_body(current, repo, rid)
                if changes != diff(review_rule(current_body)["parameters"], RULESET_REVIEW, optional_defaults=True):
                    raise Unknown("ruleset_changed_since_discovery")
                work.append(("ruleset", record("ruleset_reviews", changes, ruleset_id=rid), current_body))

        if apply:
            for kind, finding, original in work:
                guard(branch)
                if kind == "classic":
                    current = classic(api, protection_path)
                    if current is None or current.data != base.data:
                        raise Unknown("classic_protection_changed_since_discovery")
                    patch = update_fields(finding["changes"])
                    verify_write(api, f"{protection_path}/required_pull_request_reviews", "PATCH", patch, None)
                    after = classic(api, protection_path)
                    want = {**current.data, "required_pull_request_reviews":
                            {**current.data["required_pull_request_reviews"], **patch}}
                    if after is None or after.data != want:
                        raise Unknown("classic_protection_readback_differs")
                else:
                    rid = finding["ruleset_id"]
                    rule_path = f"{path}/rulesets/{rid}"
                    current = api.request(rule_path)
                    body = rule_body(require(current, dict, "ruleset"), repo, rid)
                    if body != original:
                        raise Unknown("ruleset_changed_since_discovery")
                    rule = review_rule(body)
                    rule["parameters"] = {**rule["parameters"], **update_fields(finding["changes"])}
                    verify_write(api, rule_path, "PUT", body, current.etag)
                    after = rule_body(require(api.request(rule_path), dict, "ruleset_readback"), repo, rid)
                    if after != body:
                        raise Unknown("ruleset_readback_differs")
                finding["status"] = "applied"
            if defaults["changes"]:
                fresh = guard(branch)
                changes = diff(fresh.data, DEFAULTS)
                if changes != defaults["changes"]:
                    raise Unknown("repository_settings_changed_since_discovery")
                verify_write(api, path, "PATCH", update_fields(changes), fresh.etag)
                readback = require(api.request(path), dict, "repository_readback")
                if str(readback.get("full_name", "")).lower() != repo.lower() or diff(readback, DEFAULTS):
                    raise Unknown("repository_settings_readback_differs")
                defaults["status"] = "applied"
        statuses = {result["status"] for result in findings}
        row["status"] = next((s for s in ("blocked", "applied", "drift") if s in statuses), "ok")
    except Unknown as exc:
        row.update(status="error", reason=str(exc))
    return row


def inventory(api, org):
    repos = require(api.request(INSTALLATION_REPOSITORIES, paginate=True), list, "inventory")
    names = {}
    for item in repos:
        full = item.get("full_name") if isinstance(item, dict) else None
        parts = full.split("/") if isinstance(full, str) else []
        if (len(parts) != 2 or parts[0].lower() != org.lower() or not REPO.fullmatch(parts[1])
                or parts[1] in (".", "..")):
            raise Unknown("inventory_repository_identity_invalid")
        names[full.lower()] = full
    return sorted(names.values(), key=str.lower)


def skip_repos():
    policy = json.loads((ROOT / "policy/optimistic-merge.json").read_text())
    return {repo.lower() for entry in policy["exemptions"] if entry["class"] in {"hands-off", "advisory-fork"}
            for repo in entry["repos"]}


def run(clients, *, apply=False, selected=(), skips=()):
    report = {"mode": "apply" if apply else "dry-run", "organizations": []}
    selected = {name.lower() for name in selected}

    def one(api, repo):
        if any(fnmatchcase(repo.lower(), pattern) for pattern in skips):
            return {"repo": repo, "status": "skipped", "reason": "explicit_hands_off", "findings": []}
        return reconcile(api, repo, apply)

    for org, api in clients.items():
        group = {"org": org, "repositories": [], "status": "ok"}
        report["organizations"].append(group)
        try:
            repos = inventory(api, org)
            if selected:
                absent = {name for name in selected if name.startswith(org.lower() + "/")} - {
                    name.lower() for name in repos}
                if absent:
                    raise Unknown("selected_repository_not_visible")
                repos = [name for name in repos if name.lower() in selected]
            if apply:
                group["repositories"] = [one(api, repo) for repo in repos]
            else:
                with ThreadPoolExecutor(max_workers=8) as pool:
                    group["repositories"] = list(pool.map(lambda repo: one(api, repo), repos))
            statuses = {row["status"] for row in group["repositories"]}
            group["status"] = next((s for s in ("error", "blocked", "applied", "drift") if s in statuses), "ok")
        except Unknown as exc:
            group.update(status="error", reason=str(exc))
    report["summary"] = {
        "organizations": dict(Counter(org["status"] for org in report["organizations"])),
        "repositories": dict(Counter(row["status"] for org in report["organizations"]
                                     for row in org["repositories"])),
    }
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", action="append", help="repeat for multiple explicit owners")
    parser.add_argument("--repo", action="append", default=[], help="restrict to a named org/repo (repeatable)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="write changes")
    mode.add_argument("--dry-run", dest="apply", action="store_false", help="report only (default)")
    parser.set_defaults(apply=False)
    parser.add_argument("--summary-only", action="store_true", help="omit all owner and repository identifiers")
    parser.add_argument("--require-fleet", action="store_true", help="require the configured scope to match fleet policy")
    args = parser.parse_args(argv)
    configured = args.org or os.environ.get("REPO_BASELINE_ORGS", "").split()
    if (args.apply or args.require_fleet) and not configured:
        parser.error("this mode requires explicit owner scope")
    fleet = json.loads((ROOT / "policy/merge-queue.json").read_text())["orgs"]
    orgs = configured or fleet
    if (not orgs or not all(OWNER.fullmatch(org) for org in orgs)
            or len({org.lower() for org in orgs}) != len(orgs)):
        parser.error("provide a nonempty, distinct organization list via --org or REPO_BASELINE_ORGS")
    if args.require_fleet and {org.lower() for org in orgs} != {org.lower() for org in fleet}:
        parser.error("configured owners do not match fleet policy")
    for repo in args.repo:
        parts = repo.split("/")
        if (len(parts) != 2 or parts[0].lower() not in {org.lower() for org in orgs}
                or not REPO.fullmatch(parts[1]) or parts[1] in (".", "..")):
            parser.error("--repo requires org/repo with an explicitly scoped owner")
    app_id = os.environ.get("REPO_BASELINE_APP_ID", "").strip()
    key = os.environ.get("REPO_BASELINE_APP_PRIVATE_KEY", "")
    if not re.fullmatch(r"[1-9][0-9]*", app_id) or not key.strip():
        parser.error("configure REPO_BASELINE_APP_ID (numeric) and REPO_BASELINE_APP_PRIVATE_KEY")
    try:
        # Preflight the entire selected scope before any repository work, even in apply mode.
        clients = app_installations(orgs, app_id, key)
    except Unknown as exc:
        print(f"Repository baseline authentication failed: {exc}", file=sys.stderr)
        return 1
    report = run(clients, apply=args.apply, selected=args.repo, skips=skip_repos())
    public = {"mode": report["mode"], "summary": report["summary"]}
    print(json.dumps(public if args.summary_only else report, indent=2, sort_keys=True))
    return int(bool({"error", "blocked"} & report["summary"]["organizations"].keys()))


if __name__ == "__main__":
    raise SystemExit(main())
