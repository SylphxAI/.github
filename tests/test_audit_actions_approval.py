#!/usr/bin/env python3
"""Tests for the Actions approval guard (scripts/audit_actions_approval.py)."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("audit_actions_approval", ROOT / "scripts" / "audit_actions_approval.py")
audit = importlib.util.module_from_spec(SPEC)
sys.modules["audit_actions_approval"] = audit  # dataclasses resolve annotations through sys.modules
SPEC.loader.exec_module(audit)


def fixture(name: str) -> dict:
    return json.loads((ROOT / "tests" / "fixtures" / "actions-approval" / f"{name}.json").read_text())


def summary(findings) -> set:
    return {(f.severity, f.repo, f.source.split(" (")[0]) for f in findings}


class EvaluateTest(unittest.TestCase):
    def test_clean_org_has_no_findings(self) -> None:
        self.assertEqual(audit.evaluate(fixture("clean-org")), [])

    def test_review_count_with_setting_on_fails_and_respects_repo_setting_and_enforcement(self) -> None:
        got = summary(audit.evaluate(fixture("review-count-setting-on")))
        self.assertIn(("FAIL", "one-review", "ruleset reviews"), got)
        # the repository turned Actions approval off: only a warning
        self.assertIn(("WARN", "repo-turned-off", "ruleset reviews"), got)
        # a disabled ruleset counts for nothing
        self.assertFalse([g for g in got if g[1] == "disabled-ruleset"])

    def test_review_count_with_setting_off_is_a_warning_not_a_failure(self) -> None:
        snap = fixture("review-count-setting-on")
        snap["actions_can_approve"] = False
        findings = audit.evaluate(snap)
        self.assertTrue(findings)
        self.assertEqual({f.severity for f in findings}, {"WARN"})

    def test_actions_bypass_actor_fails_even_with_setting_off(self) -> None:
        findings = audit.evaluate(fixture("actions-bypass-actor"))
        fails = {(f.repo, f.message) for f in findings if f.severity == "FAIL"}
        self.assertIn(("-", "bypass actor is the GitHub Actions integration (15368)"), fails)
        self.assertIn(("writer-role", "bypass actor is the write role, which GitHub Actions holds"), fails)
        # admin role and another app are not what Actions holds
        self.assertEqual(sum(1 for f in findings if f.repo == "writer-role"), 1)

    def test_classic_protection_actions_bypass_fails_and_count_warns(self) -> None:
        findings = [f for f in audit.evaluate(fixture("actions-bypass-actor")) if f.repo == "classic"]
        self.assertEqual({(f.severity, f.message.split(" ")[0]) for f in findings},
                         {("FAIL", "github-actions"), ("WARN", "requires")})

    def test_unreadable_state_fails_closed(self) -> None:
        snap = fixture("clean-org")
        snap["errors"] = ["org rulesets unreadable (HTTP 403: Resource not accessible)"]
        snap["repos"][0]["errors"] = ["branch protection unreadable (HTTP 403: x)"]
        self.assertEqual([f.severity for f in audit.evaluate(snap)], ["FAIL", "FAIL"])

    def test_unreadable_setting_counts_as_on(self) -> None:
        snap = fixture("review-count-setting-on")
        snap["actions_can_approve"] = None
        snap["repos"] = snap["repos"][:1]
        self.assertIn("FAIL", {f.severity for f in audit.evaluate(snap)})

    def test_ruleset_without_bypass_list_fails_closed(self) -> None:
        snap = fixture("clean-org")
        del snap["org_rulesets"][0]["bypass_actors"]
        self.assertEqual([f.severity for f in audit.evaluate(snap)], ["FAIL"])

    def test_enterprise_rule_reported_once_per_org(self) -> None:
        snap = fixture("clean-org")
        rule = {"type": "pull_request", "ruleset_source_type": "Enterprise", "ruleset_source": "ent",
                "ruleset_id": 5, "parameters": {"required_approving_review_count": 1}}
        snap["repos"] = [dict(snap["repos"][0], name=n, effective_rules=[rule]) for n in ("a", "b", "c")]
        findings = audit.evaluate(snap)
        self.assertEqual([(f.severity, f.repo) for f in findings], [("WARN", "-"), ("WARN", "-")])


class FakeApi:
    def __init__(self, routes: dict):
        self.routes = routes

    def get(self, path, params=None):
        value = self.routes[path]
        if isinstance(value, audit.ApiError):
            raise value
        return value

    def pages(self, path, params=None, key=None):
        return self.get(path)


class CollectAndRunTest(unittest.TestCase):
    def routes(self, **over) -> dict:
        r = {
            "/orgs/Org/actions/permissions/workflow": {"can_approve_pull_request_reviews": True},
            "/orgs/Org/rulesets": [{"id": 1}],
            "/orgs/Org/rulesets/1": {"id": 1, "name": "o", "enforcement": "active", "bypass_actors": [], "rules": []},
            "/orgs/Org/repos": [
                {"name": "app", "full_name": "Org/app", "default_branch": "main", "archived": False, "size": 5},
                {"name": "old", "full_name": "Org/old", "default_branch": "main", "archived": True, "size": 5},
                {"name": "skipme", "full_name": "SylphxAI/bgca", "default_branch": "main", "archived": False},
            ],
            "/repos/Org/app/actions/permissions/workflow": {"can_approve_pull_request_reviews": True},
            "/repos/Org/app/rulesets": [],
            "/repos/Org/app/rules/branches/main": [],
            "/repos/Org/app/branches/main/protection": audit.ApiError(404, "Branch not protected"),
        }
        r.update(over)
        return r

    def test_collect_reads_active_repos_only_and_tolerates_unprotected(self) -> None:
        snap = audit.collect(FakeApi(self.routes()), "Org")
        self.assertEqual([r["name"] for r in snap["repos"]], ["app"])
        self.assertEqual(snap["repos"][0]["errors"], [])
        self.assertTrue(snap["actions_can_approve"])
        self.assertEqual(audit.evaluate(snap), [])

    def test_collect_records_a_refused_read(self) -> None:
        routes = self.routes(**{"/repos/Org/app/branches/main/protection": audit.ApiError(403, "Forbidden")})
        snap = audit.collect(FakeApi(routes), "Org")
        self.assertIn("branch protection unreadable", snap["repos"][0]["errors"][0])

    def test_run_exit_codes_and_report(self) -> None:
        code, report = audit.run(["Org"], lambda org: "t", audit.DEFAULT_EXCLUDE, lambda token: FakeApi(self.routes()))
        self.assertEqual(code, 0)
        self.assertIn("Result: pass", report)
        self.assertIn("| Org | 1 | **on** | 0 | 0 |", report)

        bad = self.routes(**{"/orgs/Org/rulesets/1": {
            "id": 1, "name": "o", "enforcement": "active", "rules": [],
            "bypass_actors": [{"actor_id": 15368, "actor_type": "Integration", "bypass_mode": "always"}]}})
        code, report = audit.run(["Org"], lambda org: "t", audit.DEFAULT_EXCLUDE, lambda token: FakeApi(bad))
        self.assertEqual(code, 1)
        self.assertIn("| FAIL | Org | - | ruleset o (#1) |", report)

    def test_missing_token_fails(self) -> None:
        code, report = audit.run(["Org"], lambda org: "", audit.DEFAULT_EXCLUDE)
        self.assertEqual(code, 1)
        self.assertIn("no audit token for Org", report)


if __name__ == "__main__":
    unittest.main()
