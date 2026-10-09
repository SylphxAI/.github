#!/usr/bin/env python3
"""Tests for scripts/apply_merge_queue.py. GitHub is a fake that serves a fleet and records writes."""
from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("apply_merge_queue", ROOT / "scripts" / "apply_merge_queue.py")
amq = importlib.util.module_from_spec(SPEC)
sys.modules["apply_merge_queue"] = amq
SPEC.loader.exec_module(amq)

POLICY = amq.load_policy(ROOT / "policy" / "merge-queue.json")
OLD = {"merge_method": "SQUASH", "grouping_strategy": "ALLGREEN", "max_entries_to_build": 10, "min_entries_to_merge": 1,
       "max_entries_to_merge": 10, "min_entries_to_merge_wait_minutes": 0, "check_response_timeout_minutes": 60}


def ruleset(rid: int, name: str, params: dict | None, checks=("ci-ok",), reviews: int | None = 1) -> dict:
    """A repository ruleset; params=None has no merge_queue rule, reviews=None no pull_request rule."""
    rules = []
    if params is not None:
        rules.append({"type": "merge_queue", "parameters": dict(params)})
    rules.append({"type": "required_status_checks", "parameters": {
        "strict_required_status_checks_policy": False, "do_not_enforce_on_create": False,
        "required_status_checks": [{"context": c, "integration_id": 15368} for c in checks]}})
    if reviews is not None:
        rules.append({"type": "pull_request", "parameters": {
            "required_approving_review_count": reviews, "dismiss_stale_reviews_on_push": False,
            "require_code_owner_review": False, "require_last_push_approval": False,
            "required_review_thread_resolution": False}})
    return {
        "id": rid, "name": name, "target": "branch", "enforcement": "active", "source_type": "Repository",
        "bypass_actors": [], "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "rules": rules,
    }


class FakeGh:
    def __init__(self, repos: dict[str, dict], fail: dict[str, Exception] | None = None,
                 properties: dict[str, list | Exception] | None = None):
        self.rulesets = repos  # "org/name" -> ruleset body
        self.puts: list[tuple[str, dict]] = []
        self.queries = 0
        self.fail = fail or {}
        self.orgs = POLICY["orgs"]
        self.properties = properties or {}
        self.property_reads = []

    def graphql(self, query: str) -> dict:
        self.queries += 1
        nodes = []
        self.last_query = query
        # Serve only the organization requested, including its inherited ruleset.
        org = next((o for o in self.orgs if f'login:"{o}"' in query), "")
        inherited = None  # one enterprise ruleset, listed under every repository, as GitHub does
        for repo, rs in self.rulesets.items():
            if repo.split("/")[0] != org:
                continue
            checks = next(r for r in rs["rules"] if r["type"] == "required_status_checks")["parameters"]
            rules = []
            if amq.queue_params(rs) is not None:
                rules.append({"type": "MERGE_QUEUE", "parameters": {amq.PARAMS[k]: v for k, v in amq.queue_params(rs).items()}})
            rules.append({"type": "REQUIRED_STATUS_CHECKS", "parameters": {
                "strictRequiredStatusChecksPolicy": checks["strict_required_status_checks_policy"],
                "requiredStatusChecks": [{"context": c["context"], "integrationId": 15368} for c in checks["required_status_checks"]]}})
            if amq.review_count(rs) is not None:
                rules.append({"type": "PULL_REQUEST", "parameters": {"requiredApprovingReviewCount": amq.review_count(rs)}})
            if inherited is None:
                inherited = {"databaseId": 1, "name": "agent-native-queued-trunk-base", "enforcement": "ACTIVE",
                             "target": "BRANCH", "source": {"__typename": "Enterprise"}, "conditions": None,
                             "rules": {"nodes": [{"type": "PULL_REQUEST", "parameters": {"requiredApprovingReviewCount": 0}},
                                                 {"type": "DELETION", "parameters": None}]}}
            nodes.append({"name": repo.split("/")[1], "rulesets": {"nodes": [
                {"databaseId": rs["id"], "name": rs["name"], "enforcement": "ACTIVE", "target": "BRANCH",
                 "source": {"__typename": "Repository"},
                 "conditions": {"refName": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
                 "rules": {"nodes": rules}},
                inherited,
            ]}})
        return {"organization": {"repositories": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}}}

    def rest(self, path: str, method: str = "GET", body: dict | None = None) -> dict:
        repo = "/".join(path.split("/")[1:3])
        if path.endswith("/properties/values"):
            self.property_reads.append(repo)
            value = self.properties.get(repo, [])
            if isinstance(value, Exception):
                raise value
            return copy.deepcopy(value)
        if repo in self.fail:
            raise self.fail[repo]
        rs = self.rulesets[repo]
        if method == "PUT":
            self.puts.append((repo, copy.deepcopy(body)))
            self.rulesets[repo] = {**rs, **body}
        return copy.deepcopy(self.rulesets[repo])


class PolicyTests(unittest.TestCase):
    def test_shipped_policy_is_headgreen_with_the_fast_gate(self):
        self.assertEqual(POLICY["settings"]["grouping_strategy"], "HEADGREEN")
        self.assertEqual(POLICY["fast_gate"]["context"], "ci-ok")

    def test_shipped_policy_requires_no_approving_review(self):
        self.assertEqual(POLICY["review"]["required_approving_review_count"], 0)

    def test_shipped_policy_covers_every_organization_that_carries_a_ruleset(self):
        # Every organization of the fleet carries a repository ruleset with a
        # pull_request rule, so the review count must reach all four, not only the
        # one whose policy the first tool run listed.
        self.assertEqual(POLICY["orgs"], ["SylphxAI", "Cubeage", "EpiowAI", "OzyrixLtd"])

    def test_the_fleet_organizations_match_the_audited_optimistic_merge_policy(self):
        # policy/optimistic-merge.json is the one list of the fleet's organizations
        # (its audit test pins the four); the merge queue policy covers the same set.
        audited = json.loads((ROOT / "policy" / "optimistic-merge.json").read_text())
        self.assertEqual(POLICY["orgs"], audited["orgs"])

    def test_rejects_unknown_and_bad_values(self):
        for edit in (lambda p: p["settings"].pop("merge_method"),
                     lambda p: p["settings"].update(grouping_strategy="SOMEGREEN"),
                     lambda p: p["settings"].update(max_entries_to_build=-1),
                     lambda p: p["overrides"].append({"repo": "a/b", "ruleset": "r", "settings": {"max_entries_to_build": 1}}),
                     lambda p: p["exclude"].append({"repo": "a/b"}),
                     lambda p: p.pop("review"),
                     lambda p: p["review"].update(required_approving_review_count=-1),
                     lambda p: p["review"].update(required_approving_review_count=True)):
            p = copy.deepcopy(json.loads((ROOT / "policy" / "merge-queue.json").read_text()))
            edit(p)
            with tempfile.NamedTemporaryFile("w", suffix=".json") as f:
                json.dump(p, f)
                f.flush()
                with self.assertRaises(ValueError):
                    amq.load_policy(pathlib.Path(f.name))

    def test_override_wins_for_its_ruleset_only(self):
        p = copy.deepcopy(POLICY)
        p["overrides"] = [{"repo": "o/r", "ruleset": "q", "settings": {"max_entries_to_build": 2}, "reason": "x"}]
        self.assertEqual(amq.desired_settings(p, "o/r", "q")["max_entries_to_build"], 2)
        self.assertEqual(amq.desired_settings(p, "o/r", "other")["max_entries_to_build"], 5)

    def test_hands_off_repos_come_from_the_optimistic_merge_policy(self):
        self.assertIn("SylphxAI/bgca", amq.hands_off_repos())


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.fleet = amq.read_org(FakeGh({
            "SylphxAI/desk-tools": ruleset(10, "sylphx-merge-queue", OLD),
            "SylphxAI/cloud": ruleset(11, "sylphx-merge-queue", {**OLD, **POLICY["settings"]}),
            "SylphxAI/bgca": ruleset(12, "sylphx-merge-queue", OLD),
            "SylphxAI/other": ruleset(13, "sylphx-merge-queue", OLD, checks=("lint",)),
            "SylphxAI/janus": ruleset(14, "ci-must-pass", None),
        }), "SylphxAI")

    def rows(self):
        return {r["repo"]: r for r in amq.plan(self.fleet, POLICY, amq.excluded(POLICY, amq.hands_off_repos()))}

    def test_rulesets_with_a_queue_or_a_review_rule_are_read(self):
        # The one enterprise ruleset is inherited by every repository, so it is listed once per repository in the
        # read; the repository's own ruleset is the other entry.
        self.assertEqual(sorted({(i["repo"], i["ruleset"]) for i in self.fleet}),
                         [("SylphxAI/bgca", "agent-native-queued-trunk-base"), ("SylphxAI/bgca", "sylphx-merge-queue"),
                          ("SylphxAI/cloud", "agent-native-queued-trunk-base"), ("SylphxAI/cloud", "sylphx-merge-queue"),
                          ("SylphxAI/desk-tools", "agent-native-queued-trunk-base"), ("SylphxAI/desk-tools", "sylphx-merge-queue"),
                          ("SylphxAI/janus", "agent-native-queued-trunk-base"), ("SylphxAI/janus", "ci-must-pass"),
                          ("SylphxAI/other", "agent-native-queued-trunk-base"), ("SylphxAI/other", "sylphx-merge-queue")])
        self.assertTrue(all(i["ruleset"] != "agent-native-queued-trunk-base" or i["id"] is None for i in self.fleet))
    def test_only_the_repositorys_own_rulesets_are_read(self):
        gh = FakeGh({})
        amq.read_org(gh, "SylphxAI")
        self.assertIn("includeParents:false", gh.last_query)

    def test_required_review_drifts_to_zero(self):
        rows = self.rows()
        self.assertEqual(rows["SylphxAI/desk-tools"]["changes"]["required_approving_review_count"], [1, 0])
        self.assertEqual(rows["SylphxAI/cloud"]["changes"], {"required_approving_review_count": [1, 0]})

    def test_a_ruleset_without_a_queue_but_with_a_review_rule_is_converged(self):
        # The bug this guards: the plan loop skipped the review check for a ruleset with no queue, so a new
        # repository's require-review ruleset kept required_approving_review_count=1 forever.
        row = self.rows()["SylphxAI/janus"]
        self.assertEqual((row["status"], row["changes"], row["notes"]),
                         ("DRIFT", {"required_approving_review_count": [1, 0]}, []))

    def test_an_inherited_ruleset_is_read_but_never_written(self):
        # An enterprise ruleset has no repository path; the read appends it per repository and it must not appear
        # as drift or take part in a write.
        inherited = [i for i in self.fleet if i["id"] is None]
        self.assertTrue(inherited)
        self.assertTrue(all(i["ruleset"] == "agent-native-queued-trunk-base" for i in inherited))
        self.assertTrue(all(i["review"] == 0 for i in inherited))

    def test_diff_matches_headgreen(self):
        row = self.rows()["SylphxAI/desk-tools"]
        self.assertEqual(row["status"], "DRIFT")
        self.assertEqual(row["changes"]["grouping_strategy"], ["ALLGREEN", "HEADGREEN"])
        self.assertEqual(row["changes"]["max_entries_to_build"], [10, 5])

    def test_converged_ruleset_is_ok_and_hands_off_is_excluded(self):
        self.fleet = amq.read_org(FakeGh({
            "SylphxAI/cloud": ruleset(11, "sylphx-merge-queue", {**OLD, **POLICY["settings"]}, reviews=0),
            "SylphxAI/bgca": ruleset(12, "sylphx-merge-queue", OLD),
        }), "SylphxAI")
        rows = self.rows()
        self.assertEqual(rows["SylphxAI/cloud"]["status"], "OK")
        self.assertEqual(rows["SylphxAI/bgca"]["status"], "EXCLUDED")
        self.assertEqual(rows["SylphxAI/bgca"]["changes"], {})

    def test_missing_fast_gate_is_reported_not_written(self):
        row = self.rows()["SylphxAI/other"]
        self.assertTrue(any("ci-ok is not a required check" in n for n in row["notes"]))

    def test_gate_override(self):
        p = copy.deepcopy(POLICY)
        p["fast_gate_overrides"] = [{"repo": "SylphxAI/other", "ruleset": "sylphx-merge-queue", "context": "lint", "reason": "x"}]
        row = {r["repo"]: r for r in amq.plan(self.fleet, p, {})}["SylphxAI/other"]
        self.assertEqual(row["notes"], [])


class ApplyTests(unittest.TestCase):
    def fake(self, **kw):
        return FakeGh({"SylphxAI/desk-tools": ruleset(10, "sylphx-merge-queue", OLD),
                       "SylphxAI/work": ruleset(11, "sylphx-merge-queue", OLD),
                       "SylphxAI/janus": ruleset(12, "ci-must-pass", None)}, **kw)

    def run_main(self, gh, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = amq.main(list(argv), gh=gh)
        return rc, out.getvalue()

    def test_dry_run_is_default_and_writes_nothing(self):
        gh = self.fake()
        rc, out = self.run_main(gh, "--repo", "SylphxAI/desk-tools")
        self.assertEqual((rc, gh.puts), (0, []))
        self.assertIn("grouping_strategy: ALLGREEN -> HEADGREEN", out)
        self.assertNotIn("SylphxAI/work", out)

    def test_delivered_and_unreadable_properties_exclude_before_planning(self):
        for properties in ([{"property_name": "sylphx_delivery", "value": "delivered"}],
                           RuntimeError("HTTP 404"), RuntimeError("timeout")):
            with self.subTest(properties=properties), tempfile.TemporaryDirectory() as d:
                gh = self.fake(properties={"SylphxAI/desk-tools": properties})
                rc, out = self.run_main(gh, "--apply", "--backup-dir", d,
                                        "--repo", "SylphxAI/desk-tools", "--json")
                self.assertEqual((rc, gh.puts), (0, []))
                self.assertTrue(all(row["status"] == "EXCLUDED" and row["changes"] == {}
                                    for row in json.loads(out)["plan"]))
                self.assertEqual(gh.property_reads, ["SylphxAI/desk-tools"])
                self.assertEqual(list(pathlib.Path(d).glob("*.json")), [])

    def test_undelivered_property_allows_apply(self):
        gh = self.fake(properties={"SylphxAI/desk-tools": [
            {"property_name": "sylphx_delivery", "value": "in_progress"}]})
        with tempfile.TemporaryDirectory() as d:
            amq.WRITE_PAUSE_SECONDS = 0
            rc, _ = self.run_main(gh, "--apply", "--backup-dir", d, "--repo", "SylphxAI/desk-tools")
        self.assertEqual(rc, 0)
        self.assertEqual([repo for repo, _ in gh.puts], ["SylphxAI/desk-tools"])

    def test_property_403_stops_before_any_put(self):
        gh = self.fake(properties={"SylphxAI/desk-tools": amq.Forbidden("HTTP 403")})
        with tempfile.TemporaryDirectory() as d:
            rc, _ = self.run_main(gh, "--apply", "--backup-dir", d)
        self.assertEqual((rc, gh.puts), (2, []))

    def test_check_exits_one_on_drift(self):
        self.assertEqual(self.run_main(self.fake(), "--check")[0], 1)

    def test_apply_needs_a_backup_dir(self):
        gh = self.fake()
        with self.assertRaises(SystemExit):
            self.run_main(gh, "--apply")
        self.assertEqual(gh.puts, [])

    def test_apply_carries_the_rest_of_the_ruleset_through_and_is_idempotent(self):
        gh = self.fake()
        with tempfile.TemporaryDirectory() as d:
            amq.WRITE_PAUSE_SECONDS = 0
            rc, _ = self.run_main(gh, "--apply", "--backup-dir", d)
            self.assertEqual(rc, 0)
            self.assertEqual(len(gh.puts), 3)
            repo, body = next(p for p in gh.puts if p[0] == "SylphxAI/desk-tools")
            self.assertEqual({r["type"] for r in body["rules"]}, {"merge_queue", "required_status_checks", "pull_request"})
            review = body["rules"][2]["parameters"]
            self.assertEqual(review["required_approving_review_count"], 0)
            self.assertFalse(review["require_code_owner_review"])  # the rest of the rule is carried through
            self.assertEqual(body["rules"][1], ruleset(10, "x", OLD)["rules"][1])
            self.assertEqual(body["conditions"], gh.rulesets[repo]["conditions"])
            self.assertEqual(amq.queue_params(gh.rulesets[repo]), POLICY["settings"])
            janus = gh.rulesets["SylphxAI/janus"]
            self.assertEqual((amq.queue_params(janus), amq.review_count(janus)), (None, 0))
            self.assertNotIn("merge_queue", {r["type"] for r in janus["rules"]})
            saved = json.loads(next(pathlib.Path(d).glob("SylphxAI__desk-tools__10.json")).read_text())
            self.assertEqual(amq.queue_params(saved), OLD)
            # Second run: nothing drifts, nothing is written.
            gh.puts.clear()
            rc, _ = self.run_main(gh, "--apply", "--backup-dir", d)
            self.assertEqual((rc, gh.puts), (0, []))

    def test_rollback_restores_the_saved_ruleset(self):
        gh = self.fake()
        with tempfile.TemporaryDirectory() as d:
            amq.WRITE_PAUSE_SECONDS = 0
            self.run_main(gh, "--apply", "--backup-dir", d)
            rc, _ = self.run_main(gh, "--rollback", d)
            self.assertEqual(rc, 0)
            self.assertEqual(amq.queue_params(gh.rulesets["SylphxAI/desk-tools"]), OLD)
            for repo in gh.rulesets:
                self.assertEqual(amq.review_count(gh.rulesets[repo]), 1)

    def test_first_403_stops_the_run(self):
        gh = self.fake(fail={"SylphxAI/desk-tools": amq.Forbidden("HTTP 403")})
        with tempfile.TemporaryDirectory() as d:
            amq.WRITE_PAUSE_SECONDS = 0
            rc, _ = self.run_main(gh, "--apply", "--backup-dir", d)
        self.assertEqual(rc, 1)
        self.assertEqual(gh.puts, [])

    def test_one_failure_does_not_hide_the_next(self):
        gh = self.fake(fail={"SylphxAI/desk-tools": RuntimeError("boom")})
        with tempfile.TemporaryDirectory() as d:
            amq.WRITE_PAUSE_SECONDS = 0
            rc, _ = self.run_main(gh, "--apply", "--backup-dir", d)
        self.assertEqual(rc, 1)
        self.assertEqual([r for r, _ in gh.puts], ["SylphxAI/janus", "SylphxAI/work"])


if __name__ == "__main__":
    unittest.main()
