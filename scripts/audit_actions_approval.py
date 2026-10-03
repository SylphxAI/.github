#!/usr/bin/env python3
"""Fail when a ruleset or branch protection could ever count a GitHub Actions approval.

Policy: docs/actions-approval-guard.md. Read-only: every call is a GET.

Why it exists. With the organization setting "Allow GitHub Actions to create and
approve pull requests" on, a workflow's `GITHUB_TOKEN` can approve a pull
request, and GitHub counts that approval toward a required-review rule. The
company merges through the merge queue with zero required reviews, so no rule
may start to rely on (or be bypassed by) the Actions identity unnoticed.

For each organization it reads the Actions workflow-permission setting, every
organization ruleset, every non-archived repository's own rulesets, the
effective rules on its default branch (this is the only place an enterprise
ruleset is visible), and classic branch protection on the default branch.

FAIL (exit 1), for any active rule:
  - a pull_request rule requires approving reviews (> 0) while Actions can
    approve in that organization (and repository);
  - a bypass actor is the GitHub Actions integration (id 15368) or the
    repository role the Actions token holds (write);
  - classic protection lists `github-actions` as allowed to bypass pull request
    reviews;
  - anything it could not read (a missing token, a refused call): a guard that
    cannot see is not a pass.
WARN (exit 0 on its own): a pull_request rule requires approving reviews while
Actions cannot approve today; the next time the setting turns on it becomes a
FAIL, and bot approvals would count there.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

API = "https://api.github.com"
ACTIONS_INTEGRATION_ID = 15368  # the GitHub Actions app: the identity behind GITHUB_TOKEN
# Repository role ids in ruleset bypass lists: maintain 2, write 4, admin 5.
# GITHUB_TOKEN holds write-level repository permissions, so the write role is
# the one Actions can stand in for; maintain and admin it cannot hold.
ACTIONS_ROLES = {4: "write"}
ACTIONS_APP_SLUG = "github-actions"
# Client projects handed over to a customer: hands off, no audit (company rule).
DEFAULT_EXCLUDE = ("SylphxAI/bgca",)


@dataclass(frozen=True)
class Finding:
    severity: str  # "FAIL" | "WARN"
    org: str
    repo: str  # "-" for an organization-level or enterprise fact
    source: str
    message: str


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


class Api:
    """Minimal read-only GitHub REST client (GET only)."""

    def __init__(self, token: str):
        self.token = token

    def get(self, path: str, params: dict | None = None):
        url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "sylphx-actions-approval-guard",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as err:
            try:
                message = json.load(err).get("message", "")
            except Exception:  # noqa: BLE001 - the status is what matters
                message = ""
            raise ApiError(err.code, message) from None
        except urllib.error.URLError as err:
            raise ApiError(0, str(err.reason)) from None

    def pages(self, path: str, params: dict | None = None, key: str | None = None) -> list:
        out: list = []
        for page in range(1, 101):
            data = self.get(path, {**(params or {}), "per_page": 100, "page": page})
            items = data[key] if key else data
            out.extend(items)
            if len(items) < 100:
                break
        return out


# --- collect: one snapshot per organization ----------------------------------------


def collect(api: Api, org: str, exclude: tuple[str, ...] = DEFAULT_EXCLUDE) -> dict:
    """Read everything the evaluation needs. Never raises: a failed read is recorded."""
    snap: dict = {"org": org, "actions_can_approve": None, "org_rulesets": [], "repos": [], "errors": []}

    try:
        setting = api.get(f"/orgs/{org}/actions/permissions/workflow")
        snap["actions_can_approve"] = bool(setting.get("can_approve_pull_request_reviews"))
    except ApiError as err:
        snap["errors"].append(f"org Actions workflow permissions unreadable ({err})")

    try:
        for item in api.pages(f"/orgs/{org}/rulesets"):
            snap["org_rulesets"].append(api.get(f"/orgs/{org}/rulesets/{item['id']}"))
    except ApiError as err:
        snap["errors"].append(f"org rulesets unreadable ({err})")

    try:
        repos = api.pages(f"/orgs/{org}/repos", {"type": "all"})
    except ApiError as err:
        snap["errors"].append(f"repository list unreadable ({err})")
        return snap

    skip = {e.lower() for e in exclude}
    repos = [r for r in repos if not r.get("archived") and r["full_name"].lower() not in skip]
    org_on = snap["actions_can_approve"] is not False  # unreadable counts as on

    def one(repo: dict) -> dict:
        name, branch = repo["name"], repo.get("default_branch")
        base = f"/repos/{org}/{name}"
        entry: dict = {"name": name, "default_branch": branch, "actions_can_approve": org_on,
                       "rulesets": [], "effective_rules": [], "protection": None, "errors": []}
        if org_on:
            try:
                entry["actions_can_approve"] = bool(
                    api.get(f"{base}/actions/permissions/workflow").get("can_approve_pull_request_reviews"))
            except ApiError as err:
                entry["errors"].append(f"repo Actions workflow permissions unreadable ({err})")
        try:
            for item in api.pages(f"{base}/rulesets", {"includes_parents": "false"}):
                entry["rulesets"].append(api.get(f"{base}/rulesets/{item['id']}"))
        except ApiError as err:
            entry["errors"].append(f"rulesets unreadable ({err})")
        if branch and not repo.get("size") == 0:
            quoted = urllib.parse.quote(branch, safe="")
            try:
                entry["effective_rules"] = api.pages(f"{base}/rules/branches/{quoted}")
            except ApiError as err:
                entry["errors"].append(f"effective rules unreadable ({err})")
            try:
                entry["protection"] = api.get(f"{base}/branches/{quoted}/protection")
            except ApiError as err:
                if not (err.status == 404 and "not protected" in err.message.lower()):
                    entry["errors"].append(f"branch protection unreadable ({err})")
        return entry

    with ThreadPoolExecutor(max_workers=8) as pool:
        snap["repos"] = list(pool.map(one, repos))
    return snap


# --- evaluate: pure, tested on fixtures ----------------------------------------------


def _review_count(rule: dict) -> int:
    params = rule.get("parameters") or {}
    return int(params.get("required_approving_review_count") or 0)


def _ruleset_findings(rs: dict, org: str, repo: str, can_approve: bool) -> list[Finding]:
    if rs.get("enforcement") != "active":
        return []
    source = f"ruleset {rs.get('name', '?')} (#{rs.get('id', '?')})"
    out: list[Finding] = []
    for rule in rs.get("rules") or []:
        count = _review_count(rule) if rule.get("type") == "pull_request" else 0
        if count > 0:
            out.append(_review_finding(count, org, repo, source, can_approve))
    if "bypass_actors" not in rs:
        out.append(Finding("FAIL", org, repo, source, "bypass actors not readable with this token"))
    for actor in rs.get("bypass_actors") or []:
        kind, actor_id = actor.get("actor_type"), actor.get("actor_id")
        if kind == "Integration" and actor_id == ACTIONS_INTEGRATION_ID:
            out.append(Finding("FAIL", org, repo, source, "bypass actor is the GitHub Actions integration (15368)"))
        elif kind == "RepositoryRole" and actor_id in ACTIONS_ROLES:
            out.append(Finding("FAIL", org, repo, source,
                               f"bypass actor is the {ACTIONS_ROLES[actor_id]} role, which GitHub Actions holds"))
    return out


def _review_finding(count: int, org: str, repo: str, source: str, can_approve: bool) -> Finding:
    if can_approve:
        return Finding("FAIL", org, repo, source,
                       f"requires {count} approving review(s) and GitHub Actions can approve pull requests")
    return Finding("WARN", org, repo, source,
                   f"requires {count} approving review(s); a bot approval would count if Actions approval turns on")


def evaluate(snapshot: dict) -> list[Finding]:
    org = snapshot["org"]
    org_flag = snapshot.get("actions_can_approve")
    out: list[Finding] = [Finding("FAIL", org, "-", "audit", e) for e in snapshot.get("errors", [])]
    org_on = org_flag is not False  # unreadable counts as on, so findings stay at their strict level

    for rs in snapshot.get("org_rulesets", []):
        out += _ruleset_findings(rs, org, "-", org_on)

    enterprise_seen: set = set()
    for repo in snapshot.get("repos", []):
        name = repo["name"]
        on = org_on and bool(repo.get("actions_can_approve", True))
        out += [Finding("FAIL", org, name, "audit", e) for e in repo.get("errors", [])]
        for rs in repo.get("rulesets", []):
            out += _ruleset_findings(rs, org, name, on)
        for rule in repo.get("effective_rules", []):
            # An enterprise ruleset is visible only here, and it applies to many
            # repositories: report each one once per organization, never per repo.
            if rule.get("ruleset_source_type") != "Enterprise" or rule.get("type") != "pull_request":
                continue
            key = (rule.get("ruleset_source"), rule.get("ruleset_id"))
            count = _review_count(rule)
            if count > 0 and key not in enterprise_seen:
                enterprise_seen.add(key)
                out.append(_review_finding(count, org, "-", f"enterprise ruleset {key[0]} (#{key[1]})", org_on))
        prot = repo.get("protection") or {}
        reviews = prot.get("required_pull_request_reviews") or {}
        count = int(reviews.get("required_approving_review_count") or 0)
        if count > 0:
            out.append(_review_finding(count, org, name, "classic branch protection", on))
        allowed = (reviews.get("bypass_pull_request_allowances") or {}).get("apps") or []
        if any(app.get("slug") == ACTIONS_APP_SLUG for app in allowed):
            out.append(Finding("FAIL", org, name, "classic branch protection",
                               "github-actions may bypass pull request reviews"))
    if enterprise_seen:
        out.append(Finding("WARN", org, "-", "enterprise rulesets",
                           "enterprise ruleset bypass actors are not readable with an organization token; "
                           "check them in enterprise settings"))
    return sorted(out, key=lambda f: (f.severity != "FAIL", f.org, f.repo, f.source, f.message))


# --- report ------------------------------------------------------------------------


def render(snapshots: list[dict], findings: list[Finding]) -> str:
    fails = [f for f in findings if f.severity == "FAIL"]
    warns = [f for f in findings if f.severity == "WARN"]
    lines = ["## Actions approval guard", ""]
    lines.append("| Organization | Repositories read | Actions can approve PRs | FAIL | WARN |")
    lines.append("| --- | ---: | --- | ---: | ---: |")
    for s in snapshots:
        flag = {True: "**on**", False: "off", None: "unreadable"}[s.get("actions_can_approve")]
        lines.append(f"| {s['org']} | {len(s.get('repos', []))} | {flag} | "
                     f"{sum(1 for f in fails if f.org == s['org'])} | {sum(1 for f in warns if f.org == s['org'])} |")
    lines.append("")
    if findings:
        lines += ["| Severity | Organization | Repository | Source | Finding |", "| --- | --- | --- | --- | --- |"]
        lines += [f"| {f.severity} | {f.org} | {f.repo} | {f.source} | {f.message} |" for f in findings]
    else:
        lines.append("No ruleset or branch protection counts, or is bypassed by, a GitHub Actions approval.")
    lines.append("")
    lines.append(f"Result: {'FAIL' if fails else 'pass'} ({len(fails)} fail, {len(warns)} warn).")
    return "\n".join(lines) + "\n"


def run(orgs: list[str], token_for, exclude: tuple[str, ...], api_factory=Api) -> tuple[int, str]:
    snapshots, findings = [], []
    for org in orgs:
        token = token_for(org)
        if not token:
            snap = {"org": org, "actions_can_approve": None, "org_rulesets": [], "repos": [],
                    "errors": [f"no audit token for {org}"]}
        else:
            snap = collect(api_factory(token), org, exclude)
        snapshots.append(snap)
        findings += evaluate(snap)
    findings.sort(key=lambda f: (f.severity != "FAIL", f.org, f.repo, f.source, f.message))
    return (1 if any(f.severity == "FAIL" for f in findings) else 0), render(snapshots, findings)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--org", action="append", required=True, help="organization to audit (repeatable)")
    p.add_argument("--exclude", action="append", default=list(DEFAULT_EXCLUDE), help="owner/repo to skip")
    p.add_argument("--report", help="also write the markdown report to this file")
    args = p.parse_args(argv)
    code, report = run(args.org, lambda org: os.environ.get(f"AUDIT_TOKEN_{org.upper().replace('-', '_')}", ""),
                       tuple(args.exclude))
    sys.stdout.write(report)
    if args.report:
        with open(args.report, "w") as fh:
            fh.write(report)
    return code


if __name__ == "__main__":
    sys.exit(main())
