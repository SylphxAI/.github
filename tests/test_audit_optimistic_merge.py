#!/usr/bin/env python3
"""Tests for the optimistic-merge conformance check (scripts/audit_optimistic_merge.py).

Everything runs on fixtures: workflow text comes from the shipped starters, and
GitHub is a fake that answers the audit's GraphQL queries and compare reads.
"""
from __future__ import annotations

import contextlib
import copy
import datetime
import importlib.util
import io
import json
import os
import pathlib
import re
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("audit_optimistic_merge", ROOT / "scripts" / "audit_optimistic_merge.py")
audit = importlib.util.module_from_spec(SPEC)
sys.modules["audit_optimistic_merge"] = audit
SPEC.loader.exec_module(audit)

POLICY_PATH = ROOT / "policy" / "optimistic-merge.json"
POLICY = audit.load_policy(POLICY_PATH)
FLOOR = POLICY["pin_floor"]["sha"]
TODAY = datetime.date(2026, 10, 5)
NEWER = "a" * 40
OLDER = "b" * 40
STARTERS = ROOT / "workflow-templates"


def starter(name: str, pin: str = FLOOR) -> str:
    return (STARTERS / name).read_text().replace("@main", f"@{pin}")


def red_main(pin: str = FLOOR) -> str:
    return re.sub(r"red-main\.yml@[0-9a-f]{40}", f"red-main.yml@{pin}", (STARTERS / "red-main.yml").read_text())


def conformant(org: str = "SylphxAI", name: str = "tool", branch: str = "main", on_red: str | None = None) -> dict:
    on_red = on_red or ("revert" if org == "SylphxAI" else "notify")
    verify = starter("optimistic-verify.yml").replace("branches: [main]", f"branches: [{branch}]")
    return {
        "org": org, "repo": f"{org}/{name}", "name": name, "fork": False, "private": True, "branch": branch,
        "errors": [], "toml": f'version = "1"\n\n[ci]\nmerge = "optimistic"\non_red = "{on_red}"\n',
        "files": {"ci.yml": starter("optimistic-gate.yml"), "verify.yml": verify,
                  "red-main.yml": red_main()},
        "all_workflows": {"ci.yml": "", "verify.yml": "", "red-main.yml": ""},
        "merge_queue": True, "rules_error": None,
        "rulesets": [{"enforcement": "ACTIVE", "target": "BRANCH", "include": ["~DEFAULT_BRANCH"], "exclude": [],
                      "rules": [{"type": "MERGE_QUEUE", "contexts": []},
                                {"type": "REQUIRED_STATUS_CHECKS", "contexts": ["ci-ok"]}]}],
        "protection": [],
    }


def compare_fake(statuses: dict | None = None):
    statuses = {NEWER: "ahead", OLDER: "behind", **(statuses or {})}
    calls = []

    def read(floor, pin):
        calls.append(pin)
        value = statuses[pin]
        if isinstance(value, Exception):
            raise value
        return value
    return audit.Comparer(FLOOR, read), calls


def run_rows(facts: dict, compare=None) -> dict:
    return audit.evaluate_rows(facts, POLICY, compare or compare_fake()[0])


def failing(rows: dict) -> set:
    return {name for name, row in rows.items() if row["status"] == "FAIL"}


class ConformantTest(unittest.TestCase):
    def test_starters_are_conformant_out_of_the_box(self) -> None:
        rows = run_rows(conformant())
        self.assertEqual(failing(rows), set(), rows)
        self.assertEqual({r["status"] for r in rows.values()}, {"PASS"})

    def test_red_main_starter_pins_the_policy_floor(self) -> None:
        pins = audit.uses_refs((STARTERS / "red-main.yml").read_text(), r"\.github/workflows/red-main\.yml")
        self.assertEqual(pins, [FLOOR])

    def test_tenant_organization_notify_is_conformant(self) -> None:
        self.assertEqual(failing(run_rows(conformant("Cubeage", on_red="notify"))), set())

    def test_master_trunk_is_conformant_when_every_row_follows_it(self) -> None:
        facts = conformant("Cubeage", "big2-tycoon-keel", branch="master")
        self.assertEqual(failing(run_rows(facts)), set())

    def test_master_trunk_with_main_hardcoded_fails_caller_and_verify(self) -> None:
        facts = conformant("Cubeage", "big2-tycoon-keel", branch="master")
        facts["files"]["verify.yml"] = starter("optimistic-verify.yml")  # push: [main]
        facts["files"]["red-main.yml"] = red_main().replace(
            "== github.event.repository.default_branch", "== 'main'")
        rows = run_rows(facts)
        self.assertEqual(failing(rows), {"R3", "R4"})
        self.assertIn("master", rows["R4"]["detail"])

    def test_default_branch_expression_in_the_starter_is_conformant_on_any_trunk(self) -> None:
        facts = conformant("Cubeage", "x", branch="master")
        facts["files"]["red-main.yml"] = red_main()
        self.assertIn("== github.event.repository.default_branch", facts["files"]["red-main.yml"])
        self.assertNotIn("R4", failing(run_rows(facts)))


class RowFailureTest(unittest.TestCase):
    def only(self, facts: dict, row: str, text: str | None = None) -> None:
        rows = run_rows(facts)
        self.assertEqual(failing(rows), {row}, rows)
        if text:
            self.assertIn(text, rows[row]["detail"])

    def test_r1_missing_declaration_and_implicit_on_red(self) -> None:
        facts = conformant()
        facts["toml"] = 'version = "1"\n'
        rows = run_rows(facts)
        self.assertIn("R1", failing(rows))
        facts["toml"] = 'version = "1"\n[ci]\nmerge = "optimistic"\n'
        rows = run_rows(facts)
        self.assertEqual(failing(rows), {"R1", "R7"})
        self.assertIn("explicit", rows["R1"]["detail"])
        facts["toml"] = None
        self.assertIn("no sylphx.toml", run_rows(facts)["R1"]["detail"])

    def test_r1_ignores_a_merge_key_outside_the_ci_table(self) -> None:
        facts = conformant()
        facts["toml"] = '[other]\nmerge = "optimistic"\non_red = "revert"\n'
        self.assertIn("R1", failing(run_rows(facts)))

    def test_r2_needs_merge_group_and_ci_ok(self) -> None:
        facts = conformant()
        facts["files"]["ci.yml"] = facts["files"]["ci.yml"].replace("  merge_group:\n", "")
        self.only(facts, "R2", "merge_group")
        facts = conformant()
        facts["files"]["ci.yml"] = facts["files"]["ci.yml"].replace("  ci-ok:\n", "  verdict:\n")
        self.assertIn("R2", failing(run_rows(facts)))
        facts["files"]["ci.yml"] = None
        self.assertEqual(run_rows(facts)["R2"]["detail"], "no .github/workflows/ci.yml")

    def test_r3_needs_push_to_the_trunk_verified_and_no_cancel(self) -> None:
        facts = conformant()
        facts["files"]["verify.yml"] = facts["files"]["verify.yml"].replace("branches: [main]", "branches: [release]")
        self.only(facts, "R3", "push trigger")
        facts = conformant()
        facts["files"]["verify.yml"] = facts["files"]["verify.yml"].replace("  verified:\n    name: verified\n", "  done:\n    name: done\n")
        self.only(facts, "R3", "`verified`")
        facts = conformant()
        facts["files"]["verify.yml"] += "\nconcurrency:\n  group: v\n  cancel-in-progress: true\n"
        self.only(facts, "R3", "cancel-in-progress")

    def test_r3_accepts_the_starter_pull_request_only_cancel(self) -> None:
        facts = conformant()
        facts["files"]["verify.yml"] += ("\nconcurrency:\n  group: v\n"
                                         "  cancel-in-progress: ${{ github.event_name == 'pull_request' }}\n")
        self.assertEqual(failing(run_rows(facts)), set())

    def test_r3b_a_push_workflow_outside_verify_never_reaches_red_main(self) -> None:
        facts = conformant()
        facts["all_workflows"]["pages.yml"] = "name: Pages\non:\n  push:\n    branches: [main]\njobs:\n  deploy:\n    runs-on: x\n"
        self.only(facts, "R3b", "pages.yml")
        facts["all_workflows"]["pages.yml"] = "# optimistic-merge: advisory\n" + facts["all_workflows"]["pages.yml"]
        self.assertEqual(failing(run_rows(facts)), set())
        facts["all_workflows"]["pages.yml"] = facts["all_workflows"]["pages.yml"].replace("# optimistic-merge: advisory\n", "")
        facts["files"]["verify.yml"] += "\n  pages:\n    uses: ./.github/workflows/pages.yml\n"
        self.assertNotIn("R3b", failing(run_rows(facts)))

    def test_r3b_ignores_workflows_that_do_not_run_on_a_trunk_push(self) -> None:
        facts = conformant()
        facts["all_workflows"].update({
            "tags.yml": "on:\n  push:\n    tags: ['v*']\njobs:\n  a:\n    runs-on: x\n",
            "nightly.yml": "on:\n  schedule:\n    - cron: '0 1 * * *'\njobs:\n  a:\n    runs-on: x\n",
            "other.yml": "on:\n  push:\n    branches: [develop]\njobs:\n  a:\n    runs-on: x\n",
            "inline.yml": "on: [pull_request, workflow_dispatch]\njobs:\n  a:\n    runs-on: x\n",
        })
        self.assertEqual(failing(run_rows(facts)), set())
        facts["all_workflows"]["inline.yml"] = "on: [push]\njobs:\n  a:\n    runs-on: x\n"
        self.assertEqual(failing(run_rows(facts)), {"R3b"})

    def test_r3b_unreadable_workflow_files_fail(self) -> None:
        facts = conformant()
        facts["all_workflows"] = None
        self.only(facts, "R3b", "unreadable")

    def test_r8_merge_group_job_on_the_pull_request_pool_fails(self) -> None:
        facts = conformant()
        facts["files"]["ci.yml"] = facts["files"]["ci.yml"].replace(
            "${{ github.event_name == 'merge_group' && 'sylphx-linux-standard-merge' || 'sylphx-linux-standard' }}",
            "sylphx-linux-standard", 1)
        rows = run_rows(facts)
        self.assertEqual(failing(rows), {"R8"}, rows)
        self.assertIn("ci.yml", rows["R8"]["detail"])
        facts["files"]["ci.yml"] = facts["files"]["ci.yml"].replace(
            "runs-on: sylphx-linux-standard\n", "runs-on: ${{ github.event_name == 'merge_group' && "
            "'sylphx-linux-standard-merge' || 'sylphx-linux-standard' }}\n", 1)
        self.assertEqual(failing(run_rows(facts)), set())

    def test_r8_xlarge_and_other_workflows_with_a_merge_group_trigger(self) -> None:
        facts = conformant()
        wf = "on:\n  merge_group:\njobs:\n  build:\n    runs-on: sylphx-linux-xlarge\n    steps: []\n"
        facts["all_workflows"]["heavy.yml"] = wf
        self.only(facts, "R8", "heavy.yml (build)")
        facts["all_workflows"]["heavy.yml"] = wf.replace("xlarge", "xlarge-merge")
        self.assertEqual(failing(run_rows(facts)), set())

    def test_r8_leaves_verdicts_pr_only_jobs_and_workflows_without_merge_group(self) -> None:
        facts = conformant()
        facts["all_workflows"].update({
            "pr.yml": "on: [pull_request]\njobs:\n  a:\n    runs-on: sylphx-linux-standard\n",
            "push.yml": "# optimistic-merge: advisory\non:\n  push:\n    branches: [main]\njobs:\n  a:\n    runs-on: sylphx-linux-standard\n",
            "ctl.yml": "on:\n  merge_group:\njobs:\n  ci-ok:\n    runs-on: sylphx-linux-control\n",
            "prjob.yml": "on:\n  pull_request:\n  merge_group:\njobs:\n  lint:\n"
                         "    if: github.event_name == 'pull_request'\n    runs-on: sylphx-linux-standard\n",
            "matrix.yml": "on:\n  merge_group:\njobs:\n  m:\n    runs-on: ${{ matrix.runner }}\n",
            "call.yml": "on:\n  merge_group:\njobs:\n  c:\n    uses: ./.github/workflows/x.yml\n",
        })
        rows = run_rows(facts)
        self.assertNotIn("R8", failing(rows))

    def test_r8_block_list_runs_on(self) -> None:
        facts = conformant()
        facts["all_workflows"]["lst.yml"] = ("on:\n  merge_group:\njobs:\n  a:\n    runs-on:\n"
                                              "      - self-hosted\n      - sylphx-linux-standard\n")
        self.only(facts, "R8", "lst.yml (a)")

    def test_r4_pin_behind_floor_or_not_a_full_sha_or_missing(self) -> None:
        facts = conformant()
        facts["files"]["red-main.yml"] = red_main(OLDER)
        self.only(facts, "R4", "behind")
        facts["files"]["red-main.yml"] = red_main("main")
        self.only(facts, "R4", "40-hex")
        facts["files"]["red-main.yml"] = red_main("4224d78")
        self.only(facts, "R4", "40-hex")
        facts["files"]["red-main.yml"] = None
        self.only(facts, "R4", "no .github/workflows/red-main.yml")

    def test_r4_a_descendant_of_the_floor_passes_and_each_pin_is_read_once(self) -> None:
        compare, calls = compare_fake()
        facts = conformant()
        facts["files"]["red-main.yml"] = red_main(NEWER)
        self.assertEqual(failing(run_rows(facts, compare)), set())
        run_rows(facts, compare)
        self.assertEqual(calls, [NEWER])

    def test_r4_unreadable_compare_is_a_failure(self) -> None:
        compare, _ = compare_fake({NEWER: RuntimeError("HTTP 502")})
        facts = conformant()
        facts["files"]["red-main.yml"] = red_main(NEWER)
        rows = run_rows(facts, compare)
        self.assertEqual(failing(rows), {"R4"})
        self.assertIn("could not be compared", rows["R4"]["detail"])

    def test_r4_default_lane_input_needs_a_lanes_dispatch_input(self) -> None:
        # The anymd shape: the caller keeps the handler's default lane-input
        # (lanes) while verify.yml's workflow_dispatch declares no inputs, so
        # every candidate dispatch is refused with HTTP 422.
        facts = conformant()
        facts["files"]["verify.yml"] = re.sub(
            r"  workflow_dispatch:\n    inputs:\n(?:      .*\n)+", "  workflow_dispatch:\n", facts["files"]["verify.yml"])
        self.assertEqual(audit.dispatch_inputs(facts["files"]["verify.yml"]), set())
        self.only(facts, "R4", "lane-input `lanes` is not a workflow_dispatch input of `verify.yml`")
        facts["files"]["red-main.yml"] = red_main() + '      lane-input: ""\n'
        self.assertNotIn("R4", failing(run_rows(facts)))

    def test_r4_a_named_lane_input_must_be_declared(self) -> None:
        facts = conformant()
        self.assertEqual(audit.dispatch_inputs(facts["files"]["verify.yml"]), {"lanes"})
        facts["files"]["red-main.yml"] = red_main() + "      lane-input: suites # the verify input\n"
        self.only(facts, "R4", "lane-input `suites`")
        facts["files"]["red-main.yml"] = red_main() + "      lane-input: 'lanes'\n"
        self.assertNotIn("R4", failing(run_rows(facts)))

    def test_r4_the_verify_workflow_must_be_dispatchable_and_readable(self) -> None:
        facts = conformant()
        facts["files"]["verify.yml"] = facts["files"]["verify.yml"].replace("  workflow_dispatch:\n", "  pull_request:\n")
        self.assertIsNone(audit.dispatch_inputs(facts["files"]["verify.yml"]))
        self.assertIn("no workflow_dispatch trigger", run_rows(facts)["R4"]["detail"])
        facts = conformant()
        facts["files"]["red-main.yml"] = red_main().replace("verify-workflow: verify.yml", "verify-workflow: suite.yml")
        self.only(facts, "R4", "`suite.yml` is unreadable")
        facts["all_workflows"]["suite.yml"] = facts["files"]["verify.yml"]
        self.assertNotIn("R4", failing(run_rows(facts)))

    def test_r5_main_state_job_pin_and_need(self) -> None:
        facts = conformant()
        facts["files"]["ci.yml"] = facts["files"]["ci.yml"].replace(
            "needs: [main-state, plan,", "needs: [plan,")
        self.only(facts, "R5", "does not need")
        facts = conformant()
        facts["files"]["ci.yml"] = starter("optimistic-gate.yml", OLDER)
        self.only(facts, "R5", "behind")
        facts = conformant()
        facts["files"]["ci.yml"] = starter("optimistic-gate.yml", "main")
        self.only(facts, "R5", "40-hex")
        facts = conformant()
        facts["files"]["ci.yml"] = re.sub(r"  main-state:.*?\n\n", "", facts["files"]["ci.yml"], flags=re.S)
        facts["files"]["ci.yml"] = facts["files"]["ci.yml"].replace("[main-state, plan,", "[plan,")
        self.only(facts, "R5", "no main-state")

    def test_r6_queue_ci_ok_and_never_verified(self) -> None:
        facts = conformant()
        facts["merge_queue"] = False
        facts["rulesets"][0]["rules"] = [{"type": "REQUIRED_STATUS_CHECKS", "contexts": ["ci-ok"]}]
        self.only(facts, "R6", "no merge queue")
        facts = conformant()
        facts["rulesets"][0]["rules"][1]["contexts"] = ["ci-ok", "verified"]
        self.only(facts, "R6", "`verified` is a required check")
        facts = conformant()
        facts["rulesets"][0]["rules"][1]["contexts"] = ["source-ci/pass"]
        self.only(facts, "R6", "`ci-ok` is not a required check")

    def test_r6_reads_the_default_branch_rules_only(self) -> None:
        facts = conformant()
        facts["rulesets"][0]["include"] = ["refs/heads/release/*"]
        facts["merge_queue"] = False
        self.only(facts, "R6", "no merge queue")
        facts = conformant()
        facts["rulesets"][0]["enforcement"] = "DISABLED"
        self.only(facts, "R6")
        facts = conformant()
        facts["rulesets"][0]["exclude"] = ["refs/heads/main"]
        self.only(facts, "R6")

    def test_r6_classic_protection_and_workflow_prefixed_context(self) -> None:
        facts = conformant()
        facts["rulesets"] = []
        facts["merge_queue"] = True
        facts["protection"] = [{"pattern": "main", "contexts": ["CI / ci-ok"]}]
        self.assertEqual(failing(run_rows(facts)), set())
        facts["protection"] = [{"pattern": "release", "contexts": ["ci-ok"]}]
        self.only(facts, "R6", "`ci-ok` is not a required check")

    def test_r6_unreadable_rules_are_a_failure(self) -> None:
        facts = conformant()
        facts["rules_error"] = "rulesets or branch protection not returned"
        self.only(facts, "R6", "unreadable")

    def test_r7_follows_where_the_builder_app_reaches(self) -> None:
        self.assertEqual(failing(run_rows(conformant("SylphxAI", on_red="revert_pr_unarmed"))), set())
        self.only(conformant("SylphxAI", on_red="notify"), "R7", "should be revert")
        self.only(conformant("Cubeage", on_red="revert"), "R7", "runs as notify")


class ExemptionTest(unittest.TestCase):
    def report(self, facts_list: list[dict], today: datetime.date = TODAY, policy: dict | None = None) -> dict:
        fleet: dict[str, list] = {}
        for facts in facts_list:
            fleet.setdefault(facts["org"], []).append(facts)
        compare, _ = compare_fake()
        return audit.evaluate(fleet, policy or POLICY, compare, today)

    def by_repo(self, report: dict) -> dict:
        return {r["repo"]: r for r in report["repos"]}

    def empty(self, org: str, name: str) -> dict:
        facts = conformant(org, name)
        facts.update(toml=None, files={}, all_workflows=None, merge_queue=False, rulesets=[], protection=[])
        return facts

    def test_no_build_and_hands_off_waive_every_row(self) -> None:
        report = self.report([self.empty("Cubeage", "brand"), self.empty("SylphxAI", "bgca")])
        for repo in report["repos"]:
            self.assertEqual(repo["status"], "EXEMPT", repo)
            self.assertEqual({r["status"] for r in repo["rows"].values()}, {"EXEMPT"})
        self.assertEqual(report["summary"], {"repos": 2, "PASS": 0, "FAIL": 0, "EXEMPT": 2})

    def test_glob_exemption_covers_a_family_of_repositories(self) -> None:
        report = self.report([self.empty("Cubeage", "solar2d-linux-builds")])
        self.assertEqual(report["repos"][0]["exemption"]["class"], "no-build")

    def test_suite_is_gate_keeps_the_queue_row(self) -> None:
        facts = self.empty("Cubeage", "big2-tycoon")
        facts.update(merge_queue=True, rulesets=[{"enforcement": "ACTIVE", "target": "BRANCH", "include": ["~DEFAULT_BRANCH"],
                                                  "exclude": [], "rules": [{"type": "REQUIRED_STATUS_CHECKS", "contexts": ["pass"]}]}])
        repo = self.by_repo(self.report([facts]))["Cubeage/big2-tycoon"]
        self.assertEqual(repo["status"], "PASS", repo)
        self.assertEqual(repo["rows"]["R6"]["status"], "PASS")
        self.assertEqual(repo["rows"]["R3"]["status"], "EXEMPT")
        facts["merge_queue"] = False
        repo = self.by_repo(self.report([facts]))["Cubeage/big2-tycoon"]
        self.assertEqual((repo["status"], repo["failing"]), ("FAIL", ["R6"]))

    def test_suite_is_gate_still_forbids_requiring_verified(self) -> None:
        facts = self.empty("Cubeage", "big2-tycoon")
        facts.update(merge_queue=True, rulesets=[{"enforcement": "ACTIVE", "target": "BRANCH", "include": ["~DEFAULT_BRANCH"],
                                                  "exclude": [], "rules": [{"type": "REQUIRED_STATUS_CHECKS", "contexts": ["verified"]}]}])
        repo = self.by_repo(self.report([facts]))["Cubeage/big2-tycoon"]
        self.assertEqual(repo["failing"], ["R6"])

    def test_row_scoped_exemption_waives_only_that_row(self) -> None:
        facts = conformant("Cubeage", "fun-big2-hk", on_red="revert")
        repo = self.by_repo(self.report([facts]))["Cubeage/fun-big2-hk"]
        self.assertEqual(repo["status"], "PASS", repo)
        self.assertEqual(repo["rows"]["R7"]["status"], "EXEMPT")
        facts["files"]["red-main.yml"] = red_main(OLDER)
        repo = self.by_repo(self.report([facts]))["Cubeage/fun-big2-hk"]
        self.assertEqual(repo["failing"], ["R4"])

    def test_an_exemption_past_its_review_date_stops_applying(self) -> None:
        report = self.report([self.empty("Cubeage", "big2-tycoon-2")], today=datetime.date(2027, 4, 5))
        repo = report["repos"][0]
        self.assertEqual(repo["status"], "FAIL")
        self.assertIn("lapsed on 2027-04-04", repo["note"])
        self.assertIsNone(repo["exemption"])

    def test_an_undeclared_repository_fails_and_a_fork_is_exempt(self) -> None:
        plain = self.empty("SylphxAI", "stt-bench")
        fork = self.empty("SylphxAI", "some-fork")
        fork["fork"] = True
        repos = self.by_repo(self.report([plain, fork]))
        self.assertEqual(repos["SylphxAI/stt-bench"]["status"], "FAIL")
        self.assertEqual(repos["SylphxAI/some-fork"]["status"], "EXEMPT")
        self.assertEqual(repos["SylphxAI/some-fork"]["exemption"]["class"], "fork")

    def test_a_fork_that_declares_optimistic_is_audited(self) -> None:
        fork = conformant("SylphxAI", "ghsa-fork")
        fork["fork"] = True
        repo = self.report([fork])["repos"][0]
        self.assertEqual(repo["status"], "PASS")

    def test_an_unreadable_organization_is_a_failure(self) -> None:
        fleet = {"Cubeage": [{"org": "Cubeage", "repo": "Cubeage/*", "name": "*", "branch": None, "unlisted": True,
                              "errors": ["Cubeage: repository list unreadable (HTTP 502)"]}]}
        report = audit.evaluate(fleet, POLICY, compare_fake()[0], TODAY)
        self.assertEqual(report["summary"]["FAIL"], 1)
        self.assertIn("unreadable", audit.render(report))

    def test_read_errors_are_named_on_a_failing_row(self) -> None:
        facts = conformant()
        facts["errors"] = ["query failed: HTTP 502"]
        facts["files"]["ci.yml"] = None
        self.assertIn("HTTP 502", run_rows(facts)["R2"]["detail"])


class PolicyTest(unittest.TestCase):
    def write(self, mutate) -> pathlib.Path:
        policy = json.loads(POLICY_PATH.read_text())
        mutate(policy)
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        self.addCleanup(lambda: pathlib.Path(handle.name).unlink())
        json.dump(policy, handle)
        handle.close()
        return pathlib.Path(handle.name)

    def test_shipped_policy_is_valid_and_names_only_the_four_organizations(self) -> None:
        self.assertEqual(POLICY["orgs"], ["SylphxAI", "Cubeage", "EpiowAI", "OzyrixLtd"])
        self.assertRegex(FLOOR, r"^[0-9a-f]{40}$")
        for entry in POLICY["exemptions"]:
            for repo in entry["repos"]:
                self.assertIn(repo.split("/")[0], POLICY["orgs"], repo)

    def test_the_lead_named_exemptions_are_present(self) -> None:
        today = datetime.date(2026, 10, 5)
        for repo, kind in (("Cubeage/big2-tycoon", "suite-is-gate"), ("Cubeage/cubeage-studio", "suite-is-gate"),
                           ("Cubeage/fun-big2-hk", "on-red-notify"), ("Cubeage/fun-big2-tw", "on-red-notify"),
                           ("SylphxAI/bgca", "hands-off"), ("Cubeage/hk-mahjong-tycoon", "hands-off"),
                           ("Cubeage/Big2TycoonHk", "frozen-legacy")):
            self.assertEqual(audit.exemption_for(POLICY, repo, today)[0]["class"], kind, repo)

    def test_a_security_advisory_fork_is_exempt_and_its_parent_is_not(self) -> None:
        today = datetime.date(2026, 10, 6)
        self.assertEqual(audit.exemption_for(POLICY, "SylphxAI/puzzled-ghsa-j965-ffwx-7vxf", today)[0]["class"],
                         "advisory-fork")
        self.assertIsNone(audit.exemption_for(POLICY, "SylphxAI/puzzled", today)[0])
        self.assertIsNone(audit.exemption_for(POLICY, "SylphxAI/puzzled-ghsa-notes", today)[0])

    def test_an_exemption_needs_reason_owner_and_review(self) -> None:
        for field in ("reason", "owner", "review"):
            path = self.write(lambda p, f=field: p["exemptions"][0].pop(f))
            with self.assertRaises(ValueError, msg=field):
                audit.load_policy(path)

    def test_a_short_floor_or_a_personal_owner_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            audit.load_policy(self.write(lambda p: p["pin_floor"].update(sha="3f5ec7d")))
        with self.assertRaises(ValueError):
            audit.load_policy(self.write(lambda p: p["orgs"].append("TseFamily")))

    def test_an_unknown_class_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            audit.load_policy(self.write(lambda p: p["exemptions"][0].update({"class": "nope"})))


class ParserTest(unittest.TestCase):
    def test_trigger_forms(self) -> None:
        self.assertEqual(set(audit.triggers("on: push\njobs: {}\n")), {"push"})
        self.assertEqual(set(audit.triggers("on: [push, pull_request]\n")), {"push", "pull_request"})
        self.assertEqual(set(audit.triggers("on:\n  - push\n  - merge_group\n")), {"push", "merge_group"})
        text = "on:\n  push:\n    branches:\n      - main\n      - 'release/**'\n  merge_group:\n"
        self.assertEqual(audit.triggers(text)["push"]["branches"], ["main", "release/**"])
        self.assertTrue(audit.push_covers(text, "release/1"))
        self.assertFalse(audit.push_covers(text, "develop"))

    def test_push_without_a_filter_covers_every_branch_but_not_tag_only(self) -> None:
        self.assertTrue(audit.push_covers("on:\n  push:\n", "master"))
        self.assertFalse(audit.push_covers("on:\n  push:\n    tags: ['v*']\n", "master"))
        self.assertFalse(audit.push_covers("on:\n  push:\n    branches-ignore: [master]\n", "master"))
        self.assertFalse(audit.push_covers("on:\n  pull_request:\n", "master"))

    def test_comments_do_not_count(self) -> None:
        text = "# on: push\non:\n  pull_request:\n  # merge_group:\n"
        self.assertEqual(set(audit.triggers(text)), {"pull_request"})

    def test_jobs_by_id_or_name(self) -> None:
        text = "jobs:\n  gate:\n    name: ci-ok\n    needs: [a, b]\n  c:\n    runs-on: x\n"
        self.assertTrue(audit.has_job(text, "ci-ok"))
        self.assertTrue(audit.has_job(text, "c"))
        self.assertFalse(audit.has_job(text, "d"))
        self.assertEqual(audit.job_needs(text, "gate"), ["a", "b"])
        listed = "jobs:\n  ci-ok:\n    needs:\n      - x\n      - y\n"
        self.assertEqual(audit.job_needs(listed, "ci-ok"), ["x", "y"])

    def test_toml_ci_table(self) -> None:
        self.assertEqual(audit.toml_ci('[ci]\nmerge = "optimistic" # yes\non_red = \'notify\'\n[x]\nmerge = "no"\n'),
                         {"merge": "optimistic", "on_red": "notify"})
        self.assertEqual(audit.toml_ci(None), {})


class StarterDraftTest(unittest.TestCase):
    """A draft pull request runs the gate lanes only; its ci-ok must not wait for the suite."""

    def setUp(self) -> None:
        import yaml
        gate = yaml.safe_load((STARTERS / "optimistic-gate.yml").read_text())
        self.step = gate["jobs"]["ci-ok"]["steps"][0]["with"]["required-unless-merge-group"]
        spec = importlib.util.spec_from_file_location("needs_pass", ROOT / ".github/actions/needs-pass/needs_pass.py")
        self.needs_pass = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.needs_pass)

    def required(self, draft: bool) -> set:
        # The starter's expression, evaluated the way Actions does.
        self.assertEqual(self.step, "${{ github.event.pull_request.draft && ' ' || 'suite' }}")
        return self.needs_pass.names(" " if draft else "suite")

    def test_draft_pull_request_passes_with_the_suite_skipped(self) -> None:
        needs = {"plan": {"result": "success"}, "suite": {"result": "skipped"}}
        bad = self.needs_pass.verdict(needs, {"plan"}, set(), "pull_request", self.required(True))
        self.assertEqual(bad, [])

    def test_ready_pull_request_and_dispatch_still_need_the_suite(self) -> None:
        needs = {"plan": {"result": "success"}, "suite": {"result": "skipped"}}
        for event in ("pull_request", "workflow_dispatch"):
            bad = self.needs_pass.verdict(needs, {"plan"}, set(), event, self.required(False))
            self.assertEqual(bad, ["suite: skipped (must succeed)"], event)

    def test_merge_group_never_needs_the_suite(self) -> None:
        needs = {"plan": {"result": "success"}, "suite": {"result": "skipped"}}
        self.assertEqual(self.needs_pass.verdict(needs, {"plan"}, set(), "merge_group", self.required(False)), [])


# --- collection against a fake GitHub -------------------------------------------------


def blob(text):
    return {"text": text} if text is not None else None


def node_for(facts: dict, with_workflows: bool) -> dict:
    entries = []
    names = sorted(set(facts["files"]) | set(facts.get("all_workflows") or {}))
    for name in names:
        entry = {"name": name}
        if with_workflows:
            texts = {**{k: v for k, v in facts["files"].items() if v is not None}, **(facts.get("all_workflows") or {})}
            entry["object"] = blob(texts.get(name) or "")
        entries.append(entry)
    return {
        "defaultBranchRef": {"name": facts["branch"]},
        "mergeQueue": {"id": "MQ"} if facts["merge_queue"] else None,
        "toml": blob(facts["toml"]), "ci": blob(facts["files"].get("ci.yml")),
        "verify": blob(facts["files"].get("verify.yml")), "redmain": blob(facts["files"].get("red-main.yml")),
        "wf": {"entries": entries},
        "rulesets": {"nodes": [{
            "name": "r", "enforcement": r["enforcement"], "target": r["target"],
            "conditions": {"refName": {"include": r["include"], "exclude": r["exclude"]}},
            "rules": {"nodes": [{"type": rule["type"], "parameters": (
                {"requiredStatusChecks": [{"context": c} for c in rule["contexts"]]}
                if rule["type"] == "REQUIRED_STATUS_CHECKS" else {})} for rule in r["rules"]]}}
            for r in facts["rulesets"]]},
        "branchProtectionRules": {"nodes": [{"pattern": p["pattern"], "requiredStatusCheckContexts": p["contexts"]}
                                            for p in facts["protection"]]},
    }


class FakeGh:
    def __init__(self, repos: dict[str, dict[str, dict]], statuses: dict | None = None):
        self.repos, self.statuses = repos, statuses or {}
        self.queries: list[str] = []
        self.compares: list[str] = []

    def graphql(self, query: str) -> dict:
        self.queries.append(query)
        listing = re.search(r'organization\(login:"([^"]+)"\)', query)
        if listing:
            org = listing.group(1)
            if org not in self.repos:
                return {"data": {"organization": None}, "errors": [{"message": "Could not resolve"}]}
            nodes = [{"name": name, "isFork": facts["fork"], "isPrivate": True, "defaultBranchRef": {"name": facts["branch"]}}
                     for name, facts in self.repos[org].items()]
            return {"data": {"organization": {"repositories": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                                                               "nodes": nodes}}}}
        with_workflows = "object{... on Blob{text}}}}" in query.replace(" ", "") or "entries{name object" in query
        data = {}
        for alias, owner, name in re.findall(r'(r\d+): repository\(owner:"([^"]+)",name:"([^"]+)"\)', query):
            data[alias] = node_for(self.repos[owner][name], with_workflows)
        return {"data": data}

    def rest(self, path: str) -> dict:
        self.compares.append(path)
        pin = path.rsplit("...", 1)[1]
        return {"status": self.statuses.get(pin, "ahead")}


class CollectTest(unittest.TestCase):
    def fleet(self) -> dict:
        good = conformant("SylphxAI", "good")
        good["all_workflows"]["pages.yml"] = "on:\n  push:\n    branches: [main]\njobs:\n  a:\n    runs-on: x\n"
        undeclared = conformant("SylphxAI", "plain")
        undeclared.update(toml=None, files={}, all_workflows=None, merge_queue=False, rulesets=[], protection=[])
        hands = conformant("SylphxAI", "bgca")
        brand = conformant("Cubeage", "brand")
        return {"SylphxAI": {f["name"]: f for f in (good, undeclared, hands)}, "Cubeage": {"brand": brand}}

    def test_collect_reads_files_rules_and_workflows(self) -> None:
        gh = FakeGh(self.fleet())
        fleet = audit.collect(gh, ["SylphxAI", "Cubeage"], POLICY, TODAY)
        good = next(f for f in fleet["SylphxAI"] if f["name"] == "good")
        self.assertEqual(good["branch"], "main")
        self.assertTrue(good["merge_queue"])
        self.assertIn("merge = \"optimistic\"", good["toml"])
        self.assertIn("pages.yml", good["all_workflows"])
        self.assertEqual(good["rulesets"][0]["rules"][1]["contexts"], ["ci-ok"])
        plain = next(f for f in fleet["SylphxAI"] if f["name"] == "plain")
        self.assertIsNone(plain["toml"])
        self.assertIsNone(plain["all_workflows"])

    def test_hands_off_and_fully_waived_repositories_are_never_read(self) -> None:
        gh = FakeGh(self.fleet())
        audit.collect(gh, ["SylphxAI", "Cubeage"], POLICY, TODAY)
        queried = " ".join(gh.queries)
        self.assertNotIn('name:"bgca"', queried)
        self.assertNotIn('name:"brand"', queried)
        self.assertIn('name:"good"', queried)

    def test_end_to_end_report(self) -> None:
        gh = FakeGh(self.fleet())
        fleet = audit.collect(gh, ["SylphxAI", "Cubeage"], POLICY, TODAY)
        report = audit.evaluate(fleet, POLICY, audit.Comparer(FLOOR, audit.compare_reader(gh, POLICY)), TODAY)
        repos = {r["repo"]: r for r in report["repos"]}
        self.assertEqual(repos["SylphxAI/good"]["failing"], ["R3b"])
        self.assertEqual(repos["SylphxAI/plain"]["status"], "FAIL")
        self.assertEqual(repos["SylphxAI/bgca"]["status"], "EXEMPT")
        self.assertEqual(repos["Cubeage/brand"]["status"], "EXEMPT")
        self.assertEqual(gh.compares, [])  # every pin is the floor itself: no compare read

    def test_one_compare_read_per_distinct_pin(self) -> None:
        fleet = {"SylphxAI": {}}
        for index in range(3):
            facts = conformant("SylphxAI", f"r{index}")
            facts["files"]["red-main.yml"] = red_main(NEWER)
            fleet["SylphxAI"][facts["name"]] = facts
        gh = FakeGh(fleet)
        collected = audit.collect(gh, ["SylphxAI"], POLICY, TODAY)
        report = audit.evaluate(collected, POLICY, audit.Comparer(FLOOR, audit.compare_reader(gh, POLICY)), TODAY)
        self.assertEqual(report["summary"]["FAIL"], 0, report)
        self.assertEqual(gh.compares, [f"repos/SylphxAI/.github/compare/{FLOOR}...{NEWER}"])

    def test_batched_queries_not_one_per_repository(self) -> None:
        fleet = {"SylphxAI": {}}
        for index in range(20):
            fleet["SylphxAI"][f"r{index}"] = conformant("SylphxAI", f"r{index}")
        gh = FakeGh(fleet)
        audit.collect(gh, ["SylphxAI"], POLICY, TODAY)
        self.assertLessEqual(len(gh.queries), 1 + 3 + 7)  # list + 3 batches of <=8 + <=7 workflow batches

    def test_unreadable_organization_and_failed_batch_are_failures(self) -> None:
        gh = FakeGh({"SylphxAI": {"good": conformant("SylphxAI", "good")}})
        fleet = audit.collect(gh, ["SylphxAI", "Cubeage"], POLICY, TODAY)
        report = audit.evaluate(fleet, POLICY, audit.Comparer(FLOOR, lambda f, p: "ahead"), TODAY)
        failed = [r for r in report["repos"] if r["status"] == "FAIL"]
        self.assertEqual([r["repo"] for r in failed], ["Cubeage/*"])

        class Broken(FakeGh):
            def graphql(self, query):
                if "organization(login" in query:
                    return super().graphql(query)
                raise RuntimeError("HTTP 502")
        fleet = audit.collect(Broken({"SylphxAI": {"good": conformant("SylphxAI", "good")}}), ["SylphxAI"], POLICY, TODAY)
        report = audit.evaluate(fleet, POLICY, audit.Comparer(FLOOR, lambda f, p: "ahead"), TODAY)
        self.assertEqual(report["repos"][0]["status"], "FAIL")
        self.assertIn("HTTP 502", " ".join(r["detail"] for r in report["repos"][0]["rows"].values()))


class CommandLineTest(unittest.TestCase):
    def facts_file(self, fleet: dict) -> str:
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        self.addCleanup(lambda: pathlib.Path(handle.name).unlink())
        json.dump(fleet, handle)
        handle.close()
        return handle.name

    def run_main(self, *args: str) -> int:
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return audit.main(list(args))

    def test_exit_codes_and_json(self) -> None:
        clean = self.facts_file({"SylphxAI": [conformant()]})
        out = pathlib.Path(clean + ".out")
        self.addCleanup(lambda: out.unlink(missing_ok=True))
        self.assertEqual(self.run_main("--facts", clean, "--json", str(out), "--today", "2026-10-05"), 0)
        report = json.loads(out.read_text())
        self.assertEqual(report["floor"], FLOOR)
        self.assertEqual(report["summary"], {"repos": 1, "PASS": 1, "FAIL": 0, "EXEMPT": 0})
        self.assertEqual(set(report["repos"][0]["rows"]), set(audit.ROWS))
        bad = conformant()
        bad["toml"] = None
        dirty = self.facts_file({"SylphxAI": [bad]})
        self.assertEqual(self.run_main("--facts", dirty, "--today", "2026-10-05"), 1)

    def test_an_organization_outside_the_policy_is_refused_before_any_read(self) -> None:
        for org in ("TseFamily", "shtse8", "SomeoneElse"):
            self.assertEqual(self.run_main("--org", org), 2)


CAP_REFUSAL = ("desk-gh-graphql-cap: refusing this live GraphQL read - lane x has made 30 of its 30 allowed in 10 min.")


class CapGh(audit.Gh):
    """The real retry loop over a fake transport that refuses `refusals` times."""

    def __init__(self, refusals: float, message: str = CAP_REFUSAL, **kwargs):
        self.naps: list[float] = []
        super().__init__(sleep=self.naps.append, **kwargs)
        self.refusals, self.message, self.calls = refusals, message, 0

    def _graphql_once(self, query: str) -> dict:
        self.calls += 1
        if self.calls <= self.refusals:
            raise RuntimeError(self.message)
        return {"data": {"ok": True}}


class CapRetryTest(unittest.TestCase):
    def test_a_cap_refusal_is_waited_out_and_the_read_succeeds(self) -> None:
        gh = CapGh(3, per_call_s=720, total_s=5400, step_s=45)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(gh.graphql("q"), {"data": {"ok": True}})
        self.assertEqual((gh.calls, gh.naps), (4, [45, 45, 45]))
        self.assertIn("waiting 45s", err.getvalue())

    def test_a_read_that_never_gets_a_slot_fails_as_before_within_the_per_call_budget(self) -> None:
        gh = CapGh(10 ** 6, per_call_s=100, total_s=5400, step_s=45)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(RuntimeError) as raised:
                gh.graphql("q")
        self.assertIn("desk-gh-graphql-cap", str(raised.exception))
        self.assertEqual(gh.naps, [45, 45, 10])

    def test_the_run_budget_is_shared_between_reads(self) -> None:
        gh = CapGh(10 ** 6, per_call_s=720, total_s=60, step_s=45)
        with contextlib.redirect_stderr(io.StringIO()):
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    gh.graphql("q")
        self.assertEqual(sum(gh.naps), 60)

    def test_any_other_failure_is_not_retried(self) -> None:
        gh = CapGh(10 ** 6, message="HTTP 502", per_call_s=720, total_s=5400)
        with self.assertRaises(RuntimeError):
            gh.graphql("q")
        self.assertEqual((gh.calls, gh.naps), (1, []))

    def test_env_overrides_the_budgets(self) -> None:
        with mock.patch.dict(os.environ, {"AUDIT_CAP_WAIT_S": "0", "AUDIT_CAP_WAIT_TOTAL_S": "7"}):
            gh = audit.Gh()
        self.assertEqual((gh.per_call_s, gh.total_s), (0, 7))

    def test_an_unreadable_repository_carries_its_errors_in_the_report(self) -> None:
        facts = conformant()
        facts["errors"] = ["query failed: " + CAP_REFUSAL[:60]]
        facts["files"]["ci.yml"] = None
        report = audit.evaluate({"SylphxAI": [facts]}, POLICY, compare_fake()[0], TODAY)
        record = report["repos"][0]
        self.assertEqual(record["status"], "FAIL")
        self.assertEqual(record["errors"], facts["errors"])
        clean = audit.evaluate({"SylphxAI": [conformant()]}, POLICY, compare_fake()[0], TODAY)
        self.assertNotIn("errors", clean["repos"][0])


if __name__ == "__main__":
    unittest.main()
