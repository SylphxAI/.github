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

SHIPPED_POLICY = amq.load_policy(ROOT / "policy" / "merge-queue.json")
POLICY = {**SHIPPED_POLICY, "required_checks": []}
OLD = {"merge_method": "SQUASH", "grouping_strategy": "ALLGREEN", "max_entries_to_build": 10, "min_entries_to_merge": 1,
       "max_entries_to_merge": 10, "min_entries_to_merge_wait_minutes": 0, "check_response_timeout_minutes": 60}


def ruleset(rid: int, name: str, params: dict | None, checks=("ci-ok",), reviews: int | None = 1,
            extra: bool = False) -> dict:
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
            "required_review_thread_resolution": False,
            "require_extra_approval_for_unattributed_changes": extra}})
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
        self.detail_reads = []

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
            check_rule = next((r for r in rs["rules"] if r["type"] == "required_status_checks"), None)
            checks = check_rule["parameters"] if check_rule else None
            rules = []
            if amq.queue_params(rs) is not None:
                rules.append({"type": "MERGE_QUEUE", "parameters": {amq.PARAMS[k]: v for k, v in amq.queue_params(rs).items()}})
            if checks is not None:
                rules.append({"type": "REQUIRED_STATUS_CHECKS", "parameters": {
                    "strictRequiredStatusChecksPolicy": checks["strict_required_status_checks_policy"],
                    "requiredStatusChecks": [{"context": c["context"], "integrationId": c.get("integration_id")}
                                             for c in checks["required_status_checks"]]}})
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
        if method == "GET" and path.split("/")[-2] == "rulesets":
            self.detail_reads.append(path)
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

    def test_shipped_policy_turns_off_extra_approval_for_unattributed_changes(self):
        # The gate that actually held every bot pull request was this flag, not the review count.
        self.assertIs(POLICY["review"]["require_extra_approval_for_unattributed_changes"], False)

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
                     lambda p: p["review"].update(required_approving_review_count=True),
                     lambda p: p["review"].update(require_extra_approval_for_unattributed_changes="no"),
                     lambda p: p.update(required_checks="bad"),
                     lambda p: p["required_checks"][0].update(checks=["bad"]),
                     lambda p: p["required_checks"][0].update(ruleset_id=True),
                     lambda p: p["required_checks"][0]["checks"][0].update(integration_id=True),
                     lambda p: p["required_checks"][0]["checks"][0].update(context=" ci-ok"),
                     lambda p: p["required_checks"].append(copy.deepcopy(p["required_checks"][0]))):
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

    def test_archived_retired_repository_is_excluded_and_writes_nothing(self):
        gh = FakeGh({"SylphxAI/work": ruleset(11, "sylphx-merge-queue", OLD),
                     "SylphxAI/cloud": ruleset(12, "sylphx-merge-queue", OLD)})
        rows = {r["repo"]: r for r in amq.plan(amq.read_org(gh, "SylphxAI"), POLICY,
                                               amq.excluded(POLICY, amq.hands_off_repos()))}
        self.assertEqual(rows["SylphxAI/work"]["status"], "EXCLUDED")
        self.assertEqual(rows["SylphxAI/work"]["changes"], {})
        with tempfile.TemporaryDirectory() as directory:
            amq.apply(gh, list(rows.values()), POLICY, pathlib.Path(directory), pause=0)
        self.assertEqual([repo for repo, _ in gh.puts], ["SylphxAI/cloud"])

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
        with tempfile.NamedTemporaryFile("w", suffix=".json") as policy_file:
            json.dump(POLICY, policy_file)
            policy_file.flush()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                rc = amq.main(["--policy", policy_file.name, *argv], gh=gh)
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
            self.assertEqual(len(gh.puts), 2)
            repo, body = next(p for p in gh.puts if p[0] == "SylphxAI/desk-tools")
            self.assertEqual({r["type"] for r in body["rules"]}, {"merge_queue", "required_status_checks", "pull_request"})
            review = body["rules"][2]["parameters"]
            self.assertEqual(review["required_approving_review_count"], 0)
            self.assertFalse(review["require_extra_approval_for_unattributed_changes"])  # the true review gate is off
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
            reviews = {repo: amq.review_count(rs) for repo, rs in gh.rulesets.items()}
            rc, _ = self.run_main(gh, "--rollback", d)
            self.assertEqual(rc, 0)
            self.assertEqual(amq.queue_params(gh.rulesets["SylphxAI/desk-tools"]), OLD)
            for repo in gh.rulesets:
                self.assertEqual(amq.review_count(gh.rulesets[repo]), reviews[repo])

    def test_apply_refuses_an_existing_backup_and_keeps_it(self):
        gh = self.fake()
        with tempfile.TemporaryDirectory() as d:
            amq.WRITE_PAUSE_SECONDS = 0
            self.run_main(gh, "--apply", "--backup-dir", d)
            first = (pathlib.Path(d) / "SylphxAI__desk-tools__10.json").read_text()
            # The ruleset drifts again (a hand edit); a second apply into the same directory must not run.
            gh.rulesets["SylphxAI/desk-tools"] = ruleset(10, "sylphx-merge-queue", OLD)
            gh.puts.clear()
            rc, _ = self.run_main(gh, "--apply", "--backup-dir", d)
            self.assertEqual((rc, gh.puts), (2, []))
            self.assertEqual((pathlib.Path(d) / "SylphxAI__desk-tools__10.json").read_text(), first)
            self.assertEqual(amq.queue_params(gh.rulesets["SylphxAI/desk-tools"]), OLD)

    def test_rollback_restores_only_the_queue_parameters(self):
        gh = self.fake()
        with tempfile.TemporaryDirectory() as d:
            amq.WRITE_PAUSE_SECONDS = 0
            self.run_main(gh, "--apply", "--backup-dir", d)
            # An unrelated edit made after the apply: a second approval and a new bypass actor.
            rs = gh.rulesets["SylphxAI/desk-tools"]
            rs["rules"][2]["parameters"] = {"required_approving_review_count": 2}
            rs["bypass_actors"] = [{"actor_id": 5, "actor_type": "Team", "bypass_mode": "always"}]
            gh.puts.clear()
            rc, _ = self.run_main(gh, "--rollback", d)
            self.assertEqual(rc, 0)
            now = gh.rulesets["SylphxAI/desk-tools"]
            self.assertEqual(amq.queue_params(now), OLD)
            self.assertEqual(now["rules"][2]["parameters"], {"required_approving_review_count": 2})
            self.assertEqual(now["bypass_actors"][0]["actor_id"], 5)

    def test_rollback_fails_when_the_queue_rule_is_gone(self):
        gh = self.fake()
        with tempfile.TemporaryDirectory() as d:
            amq.WRITE_PAUSE_SECONDS = 0
            self.run_main(gh, "--apply", "--backup-dir", d)
            gh.rulesets["SylphxAI/desk-tools"]["rules"] = [r for r in gh.rulesets["SylphxAI/desk-tools"]["rules"] if r["type"] != "merge_queue"]
            gh.puts.clear()
            rc, out = self.run_main(gh, "--rollback", d)
            self.assertEqual(rc, 1)
            self.assertNotIn("SylphxAI/desk-tools", [r for r, _ in gh.puts])

    def test_all_four_organizations_and_the_platform_repo_are_covered(self):
        self.assertEqual(POLICY["orgs"], ["SylphxAI", "Cubeage", "EpiowAI", "OzyrixLtd"])
        skip = amq.excluded(POLICY, amq.hands_off_repos())
        for repo in ("Cubeage/cubeage-platform", "Cubeage/hk-mahjong-tycoon", "SylphxAI/bgca"):
            self.assertIn(repo, skip)

    def test_first_403_stops_the_run(self):
        # A 403 on the write stops the run: no ruleset after it is written.
        gh = self.fake()
        original = gh.rest
        def forbid_write(path, method="GET", body=None):
            if method == "PUT":
                raise amq.Forbidden("HTTP 403")
            return original(path, method, body)
        gh.rest = forbid_write
        with tempfile.TemporaryDirectory() as d:
            amq.WRITE_PAUSE_SECONDS = 0
            rc, _ = self.run_main(gh, "--apply", "--backup-dir", d)
        self.assertEqual(rc, 1)
        self.assertEqual(gh.puts, [])

    def test_detail_read_403_stops_before_any_write(self):
        # The extra-approval detail read is a read like the property read: a 403 there fails closed.
        gh = FakeGh({"SylphxAI/desk-tools": ruleset(10, "sylphx-merge-queue", OLD)})
        original = gh.rest
        def forbid_detail(path, method="GET", body=None):
            if method == "GET" and path.split("/")[-2] == "rulesets":
                raise amq.Forbidden("HTTP 403")
            return original(path, method, body)
        gh.rest = forbid_detail
        with tempfile.TemporaryDirectory() as d:
            rc, _ = self.run_main(gh, "--apply", "--backup-dir", d)
        self.assertEqual((rc, gh.puts), (2, []))

    def test_one_failure_does_not_hide_the_next(self):
        gh = self.fake()
        original = gh.rest
        def boom_write(path, method="GET", body=None):
            if method == "PUT" and "/SylphxAI/desk-tools/" in path:
                raise RuntimeError("boom")
            return original(path, method, body)
        gh.rest = boom_write
        with tempfile.TemporaryDirectory() as d:
            amq.WRITE_PAUSE_SECONDS = 0
            rc, _ = self.run_main(gh, "--apply", "--backup-dir", d)
        self.assertEqual(rc, 1)
        self.assertEqual([r for r, _ in gh.puts], ["SylphxAI/janus"])

    def test_unreadable_detail_excludes_the_repository_before_planning(self):
        gh = self.fake(fail={"SylphxAI/desk-tools": RuntimeError("boom")})
        with tempfile.TemporaryDirectory() as d:
            amq.WRITE_PAUSE_SECONDS = 0
            rc, out = self.run_main(gh, "--apply", "--backup-dir", d, "--repo", "SylphxAI/desk-tools", "--json")
        self.assertEqual((rc, gh.puts), (0, []))
        self.assertTrue(all(row["status"] == "EXCLUDED" and row["changes"] == {}
                            for row in json.loads(out)["plan"]))
        self.assertEqual(gh.detail_reads, ["repos/SylphxAI/desk-tools/rulesets/10"])


class RequiredCheckTests(unittest.TestCase):
    def fake(self):
        return FakeGh({
            entry["repo"]: ruleset(entry["ruleset_id"], entry["ruleset"], POLICY["settings"], reviews=0)
            for entry in SHIPPED_POLICY["required_checks"]})

    def rows(self, gh):
        return amq.plan(amq.read_org(gh, "SylphxAI"), SHIPPED_POLICY, {})

    def test_declared_review_gate_covers_the_gated_repositories(self):
        self.assertEqual([entry["repo"] for entry in SHIPPED_POLICY["required_checks"]],
                         ["SylphxAI/cloud", "SylphxAI/infra", "SylphxAI/desk-tools", "SylphxAI/.github",
                          "SylphxAI/owner"])
        for entry in SHIPPED_POLICY["required_checks"]:
            expected = [
                {"context": "ci-ok", "integration_id": 15368},
                {"context": "review-ok", "integration_id": 5155317}]
            if entry["repo"] == "SylphxAI/owner":
                # owner's plain-language workflow produces always-loaded on its pull request heads; no other repository does.
                expected = [
                    {"context": "ci-ok", "integration_id": 15368},
                    {"context": "always-loaded", "integration_id": 15368}]
            self.assertEqual(entry["checks"], expected)

    def test_scoped_missing_review_check_is_reported_with_exact_issuer(self):
        rows = [row for row in self.rows(self.fake()) if row["status"] == "DRIFT"]
        self.assertEqual(len(rows), len(SHIPPED_POLICY["required_checks"]))
        for row in rows:
            before, after = row["changes"]["required_status_checks"]
            self.assertEqual(before, [{"context": "ci-ok", "integration_id": 15368}])
            entry = next(e for e in SHIPPED_POLICY["required_checks"] if e["repo"] == row["repo"])
            for check in entry["checks"][1:]:
                self.assertIn(check, after)

    def test_apply_adds_gate_preserves_other_checks_and_rollback_restores_bindings(self):
        gh = self.fake()
        cloud = gh.rulesets["SylphxAI/cloud"]
        cloud["rules"][1]["parameters"]["required_status_checks"].append(
            {"context": "security-review", "integration_id": 77})
        before = copy.deepcopy(gh.rulesets)
        with tempfile.TemporaryDirectory() as directory:
            result = amq.apply(gh, self.rows(gh), SHIPPED_POLICY, pathlib.Path(directory), pause=0)
            self.assertTrue(all(row["result"] == "APPLIED" for row in result))
            checks = gh.rulesets["SylphxAI/cloud"]["rules"][1]["parameters"]["required_status_checks"]
            self.assertIn({"context": "security-review", "integration_id": 77}, checks)
            self.assertIn({"context": "review-ok", "integration_id": 5155317}, checks)
            self.assertEqual(gh.rulesets["SylphxAI/cloud"]["conditions"], before["SylphxAI/cloud"]["conditions"])
            gh.puts.clear()
            self.assertEqual(amq.apply(gh, self.rows(gh), SHIPPED_POLICY, pathlib.Path(directory), pause=0), [])
            self.assertEqual(gh.puts, [])
            restored = amq.rollback(gh, pathlib.Path(directory), pause=0)
            self.assertTrue(all(row["result"] == "RESTORED" for row in restored))
            for repo, saved in before.items():
                self.assertEqual(amq.queue_params(gh.rulesets[repo]), amq.queue_params(saved))
            self.assertIn({"context": "review-ok", "integration_id": 5155317},
                          gh.rulesets["SylphxAI/cloud"]["rules"][1]["parameters"]["required_status_checks"])

    def test_wrong_review_issuer_is_corrected_and_unscoped_ruleset_unchanged(self):
        gh = self.fake()
        gh.rulesets["SylphxAI/cloud"]["rules"][1]["parameters"]["required_status_checks"].append(
            {"context": "review-ok", "integration_id": 15368})
        gh.rulesets["SylphxAI/janus"] = ruleset(24403813, "janus-main", POLICY["settings"], reviews=0)
        with tempfile.TemporaryDirectory() as directory:
            amq.apply(gh, self.rows(gh), SHIPPED_POLICY, pathlib.Path(directory), pause=0)
        self.assertEqual({repo for repo, _ in gh.puts}, {entry["repo"] for entry in SHIPPED_POLICY["required_checks"]})
        self.assertEqual(gh.rulesets["SylphxAI/janus"]["rules"][1], ruleset(1, "x", POLICY["settings"])["rules"][1])
        checks = gh.rulesets["SylphxAI/cloud"]["rules"][1]["parameters"]["required_status_checks"]
        self.assertNotIn({"context": "review-ok", "integration_id": 15368}, checks)
        self.assertIn({"context": "review-ok", "integration_id": 5155317}, checks)

    def test_missing_status_rule_adds_ci_and_review_without_weakening_other_rules(self):
        gh = self.fake()
        gh.rulesets["SylphxAI/cloud"]["rules"].pop(1)
        with tempfile.TemporaryDirectory() as directory:
            amq.apply(gh, self.rows(gh), SHIPPED_POLICY, pathlib.Path(directory), pause=0)
        added = next(rule for rule in gh.rulesets["SylphxAI/cloud"]["rules"]
                     if rule["type"] == "required_status_checks")
        self.assertEqual(added["parameters"]["required_status_checks"],
                         SHIPPED_POLICY["required_checks"][0]["checks"])

    def test_missing_target_refuses_every_write_and_name_drift_fails_closed(self):
        gh = self.fake()
        del gh.rulesets["SylphxAI/infra"]
        with tempfile.TemporaryDirectory() as directory:
            result = amq.apply(gh, self.rows(gh), SHIPPED_POLICY, pathlib.Path(directory), pause=0)
        self.assertEqual(gh.puts, [])
        self.assertTrue(all(row["result"].startswith("FAILED") for row in result))
        gh.rulesets["SylphxAI/cloud"]["name"] = "renamed"
        with self.assertRaisesRegex(ValueError, "name differs"):
            self.rows(gh)

    def test_required_checks_only_ruleset_is_read_and_converged(self):
        gh = self.fake()
        gh.rulesets["SylphxAI/cloud"] = ruleset(20321070, "sylphx-merge-queue", None, reviews=None)
        with tempfile.TemporaryDirectory() as directory:
            result = amq.apply(gh, self.rows(gh), SHIPPED_POLICY, pathlib.Path(directory), pause=0)
        self.assertTrue(all(row["result"] == "APPLIED" for row in result))
        self.assertIsNone(amq.queue_params(gh.rulesets["SylphxAI/cloud"]))
        self.assertIn({"context": "review-ok", "integration_id": 5155317},
                      gh.rulesets["SylphxAI/cloud"]["rules"][0]["parameters"]["required_status_checks"])

    def test_multiple_old_publishers_converge_to_one_exact_binding(self):
        old = [{"context": "review-ok", "integration_id": None},
               {"context": "review-ok", "integration_id": 15368},
               {"context": "ci-ok", "integration_id": 15368}]
        self.assertEqual(amq.merge_checks(old, SHIPPED_POLICY["required_checks"][0]["checks"]),
                         [{"context": "review-ok", "integration_id": 5155317},
                          {"context": "ci-ok", "integration_id": 15368}])

    def test_wrong_issuer_in_readback_and_rollback_is_not_reported_as_success(self):
        gh = self.fake()
        original = gh.rest
        written = set()
        def wrong_binding(path, method="GET", body=None):
            result = original(path, method, body)
            if method == "PUT":
                written.add(path)
            if path in written:
                for rule in result["rules"]:
                    if rule["type"] == "required_status_checks":
                        for check in rule["parameters"]["required_status_checks"]:
                            if check["context"] == "ci-ok":
                                check["integration_id"] = 999
            return result
        gh.rest = wrong_binding
        with tempfile.TemporaryDirectory() as directory:
            applied = amq.apply(gh, self.rows(gh), SHIPPED_POLICY, pathlib.Path(directory), pause=0)
            self.assertTrue(all(row["result"].startswith("FAILED") for row in applied))
            restored = amq.rollback(gh, pathlib.Path(directory), pause=0)
            # Queue-only rollback preserves the live check binding, even if
            # a prior apply's binding write failed its readback.
            self.assertTrue(all(row["result"] == "RESTORED" for row in restored))


class ExtraApprovalTests(unittest.TestCase):
    """review.require_extra_approval_for_unattributed_changes reaches live rulesets.

    This is the gate that actually held every bot pull request on 2026-10-07 (one approving review
    for commits GitHub cannot attribute to a user, which is every desk App's commit), not the
    approving review count. The live write removed it; the tool now manages it so drift is caught."""

    def fleet(self, gh, skip=None, only=None):
        items = amq.read_org(gh, "SylphxAI")
        amq.enrich_extra_approval(gh, items, skip if skip is not None else {}, only or set())
        return items

    def test_extra_approval_on_drifts_to_false(self):
        gh = FakeGh({"SylphxAI/desk-tools": ruleset(10, "sylphx-merge-queue", POLICY["settings"],
                                                    reviews=0, extra=True)})
        rows = {r["repo"]: r for r in amq.plan(self.fleet(gh), POLICY, {})}
        self.assertEqual(rows["SylphxAI/desk-tools"]["status"], "DRIFT")
        self.assertEqual(rows["SylphxAI/desk-tools"]["changes"],
                         {"require_extra_approval_for_unattributed_changes": [True, False]})
        self.assertEqual(gh.detail_reads, ["repos/SylphxAI/desk-tools/rulesets/10"])

    def test_extra_approval_off_is_ok(self):
        gh = FakeGh({"SylphxAI/desk-tools": ruleset(10, "sylphx-merge-queue", POLICY["settings"],
                                                    reviews=0, extra=False)})
        rows = {r["repo"]: r for r in amq.plan(self.fleet(gh), POLICY, {})}
        self.assertEqual((rows["SylphxAI/desk-tools"]["status"], rows["SylphxAI/desk-tools"]["changes"]),
                         ("OK", {}))

    def test_a_ruleset_without_a_pull_request_rule_costs_no_detail_read(self):
        gh = FakeGh({"SylphxAI/janus": ruleset(12, "ci-must-pass", None, reviews=None)})
        self.fleet(gh)
        self.assertEqual(gh.detail_reads, [])

    def test_an_inherited_ruleset_costs_no_detail_read(self):
        gh = FakeGh({"SylphxAI/desk-tools": ruleset(10, "sylphx-merge-queue", POLICY["settings"], reviews=0)})
        items = self.fleet(gh)
        self.assertTrue(any(i["id"] is None and i["review"] is not None for i in items))
        self.assertEqual(gh.detail_reads, ["repos/SylphxAI/desk-tools/rulesets/10"])

    def test_an_excluded_repository_costs_no_detail_read_or_write(self):
        gh = FakeGh({"SylphxAI/bgca": ruleset(12, "sylphx-merge-queue", OLD, extra=True)})
        skip = amq.excluded(POLICY, amq.hands_off_repos())
        items = self.fleet(gh, skip=skip)
        self.assertEqual(gh.detail_reads, [])
        rows = {r["repo"]: r for r in amq.plan(items, POLICY, skip)}
        self.assertEqual((rows["SylphxAI/bgca"]["status"], rows["SylphxAI/bgca"]["changes"]), ("EXCLUDED", {}))

    def test_apply_converges_the_flag_and_reads_it_back(self):
        gh = FakeGh({"SylphxAI/desk-tools": ruleset(10, "sylphx-merge-queue", POLICY["settings"],
                                                    reviews=0, extra=True)})
        with tempfile.TemporaryDirectory() as d:
            result = amq.apply(gh, amq.plan(self.fleet(gh), POLICY, {}), POLICY, pathlib.Path(d), pause=0)
            self.assertEqual([r["result"] for r in result], ["APPLIED"])
            self.assertFalse(amq.extra_approval(gh.rulesets["SylphxAI/desk-tools"]))
            # A second run converges nothing.
            self.assertEqual(amq.apply(gh, amq.plan(self.fleet(gh), POLICY, {}), POLICY, pathlib.Path(d), pause=0), [])


if __name__ == "__main__":
    unittest.main()
