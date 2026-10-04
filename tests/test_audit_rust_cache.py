#!/usr/bin/env python3
"""Tests for the Rust compile-cache conformance check (scripts/audit_rust_cache.py)
and for the rust-sccache namespace default.

Everything runs on fixtures: workflow text is generated here, and GitHub is a
fake that answers the audit's GraphQL queries and compare reads.
"""
from __future__ import annotations

import datetime
import importlib.util
import json
import pathlib
import re
import sys
import tempfile
import unittest
from unittest import mock

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("audit_rust_cache", ROOT / "scripts" / "audit_rust_cache.py")
audit = importlib.util.module_from_spec(SPEC)
sys.modules["audit_rust_cache"] = audit
SPEC.loader.exec_module(audit)

ACTION = ROOT / ".github" / "actions" / "rust-sccache" / "action.yml"
POLICY_PATH = ROOT / "policy" / "rust-cache.json"
POLICY = audit.load_policy(POLICY_PATH)
FLOOR = POLICY["pin_floor"]["sha"]
TODAY = datetime.date(2026, 10, 5)
NEWER = "a" * 40
OLDER = "b" * 40
DEFAULT_SHA = "c" * 40
SCCACHE = "SylphxAI/.github/.github/actions/rust-sccache"
RUST_CI = "SylphxAI/.github/.github/workflows/rust-ci.yml"


def workflow(jobs: str, on: str = "on:\n  pull_request:\n  merge_group:\n  push:\n    branches: [main]\n") -> str:
    return f"name: CI\n{on}jobs:\n{jobs}"


def sccache_job(name: str = "test", pin: str = FLOOR, prefix: str | None = "rustc", runner: str = "sylphx-linux-standard",
                extra: str = "") -> str:
    key = f"          key-prefix: {prefix}\n" if prefix is not None else ""
    return (f"  {name}:\n    runs-on: {runner}\n    steps:\n      - uses: actions/checkout@v4\n"
            f"      - uses: {SCCACHE}@{pin} # main\n        with:\n          s3-access-key: x\n{key}"
            f"      - run: cargo test --locked\n{extra}")


def facts(workflows: dict | None, branch: str = "main", name: str = "tool", org: str = "Cubeage", **more) -> dict:
    return {"org": org, "repo": f"{org}/{name}", "name": name, "fork": False, "private": True, "branch": branch,
            "errors": [], "workflows": workflows, **more}


def compare_fake(statuses: dict | None = None):
    statuses = {NEWER: "ahead", OLDER: "behind", DEFAULT_SHA: "ahead", **(statuses or {})}
    calls = []

    def read(floor, pin):
        calls.append((floor, pin))
        value = statuses[pin]
        if isinstance(value, Exception):
            raise value
        return value
    return audit.om.Comparer(FLOOR, read), calls


def run_rows(f: dict, policy: dict | None = None, compare=None) -> dict:
    policy = policy or POLICY
    compare = compare or compare_fake()[0]
    default = policy.get("rustc_default_sha") or ""
    return audit.evaluate_rows(f, policy, compare, audit.om.Comparer(default, compare.read) if default else None)


def failing(rows: dict) -> set:
    return {name for name, row in rows.items() if row["status"] == "FAIL"}


class ActionDefaultTest(unittest.TestCase):
    def test_key_prefix_defaults_to_the_organization_namespace(self) -> None:
        spec = yaml.safe_load(ACTION.read_text())["inputs"]["key-prefix"]
        self.assertEqual(spec["default"], "rustc")
        self.assertNotIn("repository", spec["default"])
        self.assertNotIn("Defaults to the repository", spec["description"])

    def test_policy_floor_is_the_commit_rust_ci_pins(self) -> None:
        pins = re.findall(r"actions/rust-sccache@([0-9a-f]{40})", (ROOT / ".github" / "workflows" / "rust-ci.yml").read_text())
        self.assertEqual(pins, [FLOOR])

    def test_shipped_rust_workflows_pass_their_own_audit(self) -> None:
        names = ("rust-ci.yml", "rust-check.yml")
        f = facts({n: (ROOT / ".github" / "workflows" / n).read_text() for n in names}, name=".github", org="SylphxAI")
        rows = run_rows(f)
        self.assertEqual(failing(rows), set(), rows)


class PolicyTest(unittest.TestCase):
    def write(self, **changes) -> pathlib.Path:
        policy = json.loads(POLICY_PATH.read_text()) | changes
        path = pathlib.Path(tempfile.mkdtemp()) / "policy.json"
        path.write_text(json.dumps(policy))
        return path

    def test_shipped_policy_is_valid_and_exempts_with_owner_and_review(self) -> None:
        for entry in POLICY["exemptions"]:
            for key in ("class", "repos", "reason", "owner", "review"):
                self.assertTrue(entry[key], entry)

    def test_floor_and_default_must_be_full_shas(self) -> None:
        with self.assertRaises(ValueError):
            audit.load_policy(self.write(pin_floor={"repo": "SylphxAI/.github", "sha": "abc"}))
        with self.assertRaises(ValueError):
            audit.load_policy(self.write(rustc_default_sha="abc"))
        audit.load_policy(self.write(rustc_default_sha=DEFAULT_SHA))

    def test_exemption_needs_a_reason(self) -> None:
        bad = [{"class": "hands-off", "repos": ["a/b"], "reason": "", "owner": "x", "review": "2027-01-01"}]
        with self.assertRaises(ValueError):
            audit.load_policy(self.write(exemptions=bad))


class ConformantTest(unittest.TestCase):
    def test_rust_ci_caller_with_main_push_passes(self) -> None:
        f = facts({"ci.yml": workflow(f"  rust:\n    uses: {RUST_CI}@main\n    with:\n      workspace: .\n")})
        rows = run_rows(f)
        self.assertEqual({r["status"] for r in rows.values()}, {"PASS"}, rows)

    def test_direct_action_at_floor_with_rustc_passes(self) -> None:
        rows = run_rows(facts({"ci.yml": workflow(sccache_job())}))
        self.assertEqual({r["status"] for r in rows.values()}, {"PASS"}, rows)

    def test_pin_ahead_of_floor_passes(self) -> None:
        compare, calls = compare_fake()
        rows = run_rows(facts({"ci.yml": workflow(sccache_job(pin=NEWER))}), compare=compare)
        self.assertEqual(failing(rows), set(), rows)
        self.assertEqual(calls, [(FLOOR, NEWER)])

    def test_trailing_comment_and_matrix_runner_are_read(self) -> None:
        job = sccache_job(runner="${{ matrix.runner }}").replace(
            "    steps:", "    strategy:\n      matrix:\n        runner: [sylphx-linux-standard]\n    steps:", 1)
        self.assertEqual(failing(run_rows(facts({"ci.yml": workflow(job)}))), set())

    def test_this_repository_may_use_its_own_local_action(self) -> None:
        job = sccache_job().replace(f"{SCCACHE}@{FLOOR} # main", "./.github/actions/rust-sccache")
        own = facts({"ci.yml": workflow(job)}, name=".github", org="SylphxAI")
        self.assertEqual(failing(run_rows(own)), set())
        other = facts({"ci.yml": workflow(job)})
        self.assertEqual(failing(run_rows(other)), {"C1"})


class C1Test(unittest.TestCase):
    def test_cargo_without_any_shared_cache_fails(self) -> None:
        job = "  test:\n    runs-on: sylphx-linux-standard\n    steps:\n      - run: cargo test\n"
        rows = run_rows(facts({"ci.yml": workflow(job)}))
        self.assertEqual(failing(rows), {"C1"})
        self.assertIn("ci.yml:test", rows["C1"]["detail"])

    def test_swatinem_alone_fails(self) -> None:
        job = ("  test:\n    runs-on: sylphx-linux-standard\n    steps:\n"
               "      - uses: Swatinem/rust-cache@v2\n      - run: cargo build\n")
        self.assertEqual(failing(run_rows(facts({"ci.yml": workflow(job)}))), {"C1"})

    def test_pin_behind_the_floor_fails(self) -> None:
        rows = run_rows(facts({"ci.yml": workflow(sccache_job(pin=OLDER))}))
        self.assertEqual(failing(rows), {"C1"})
        self.assertIn("behind the floor", rows["C1"]["detail"])

    def test_branch_ref_is_not_a_pin(self) -> None:
        rows = run_rows(facts({"ci.yml": workflow(sccache_job(pin="main"))}))
        self.assertEqual(failing(rows), {"C1"})
        self.assertIn("40-hex", rows["C1"]["detail"])

    def test_unreadable_compare_is_a_fail_not_a_pass(self) -> None:
        compare, _ = compare_fake({NEWER: RuntimeError("rate limited")})
        rows = run_rows(facts({"ci.yml": workflow(sccache_job(pin=NEWER))}), compare=compare)
        self.assertEqual(failing(rows), {"C1"})
        self.assertIn("could not be compared", rows["C1"]["detail"])

    def test_one_bad_job_among_good_ones_names_that_job(self) -> None:
        bad = "  lint:\n    runs-on: sylphx-linux-standard\n    steps:\n      - run: cargo clippy\n"
        rows = run_rows(facts({"ci.yml": workflow(sccache_job() + bad)}))
        self.assertEqual(failing(rows), {"C1"})
        self.assertIn("ci.yml:lint", rows["C1"]["detail"])
        self.assertNotIn("ci.yml:test", rows["C1"]["detail"])

    def test_fmt_only_job_is_not_a_compile(self) -> None:
        job = "  fmt:\n    runs-on: sylphx-linux-standard\n    steps:\n      - run: cargo fmt --check\n"
        rows = run_rows(facts({"ci.yml": workflow(job)}))
        self.assertEqual({r["status"] for r in rows.values()}, {"SKIP"})

    def test_commented_out_cargo_is_ignored(self) -> None:
        job = "  t:\n    runs-on: sylphx-linux-standard\n    steps:\n      # - run: cargo test\n      - run: echo hi\n"
        self.assertEqual({r["status"] for r in run_rows(facts({"ci.yml": workflow(job)})).values()}, {"SKIP"})


class C2Test(unittest.TestCase):
    def test_repository_namespace_fails(self) -> None:
        rows = run_rows(facts({"ci.yml": workflow(sccache_job(prefix="${{ github.repository }}"))}))
        self.assertEqual(failing(rows), {"C2"})
        self.assertIn("github.repository", rows["C2"]["detail"])

    def test_rolled_namespace_fails_until_the_policy_allows_it(self) -> None:
        self.assertEqual(failing(run_rows(facts({"ci.yml": workflow(sccache_job(prefix="rustc-2"))}))), {"C2"})

    def test_unset_prefix_fails_while_the_default_commit_is_unknown(self) -> None:
        rows = run_rows(facts({"ci.yml": workflow(sccache_job(prefix=None))}))
        self.assertEqual(failing(rows), {"C2"})
        self.assertIn("key-prefix: rustc", rows["C2"]["detail"])

    def test_unset_prefix_passes_on_a_pin_at_or_after_the_rustc_default(self) -> None:
        policy = POLICY | {"rustc_default_sha": DEFAULT_SHA}
        compare, _ = compare_fake({NEWER: "ahead"})
        rows = run_rows(facts({"ci.yml": workflow(sccache_job(pin=NEWER, prefix=None))}), policy, compare)
        self.assertEqual(failing(rows), set(), rows)

    def test_unset_prefix_fails_on_a_pin_before_the_rustc_default(self) -> None:
        policy = POLICY | {"rustc_default_sha": DEFAULT_SHA}
        compare, _ = compare_fake({FLOOR: "identical"})
        # the floor itself is at or after the floor, but behind the default commit
        compare.read = lambda floor, pin: "behind" if floor == DEFAULT_SHA else "ahead"
        rows = run_rows(facts({"ci.yml": workflow(sccache_job(pin=NEWER, prefix=None))}), policy, compare)
        self.assertEqual(failing(rows), {"C2"})

    def test_reusable_workflow_caller_with_another_prefix_fails(self) -> None:
        job = f"  rust:\n    uses: {RUST_CI}@main\n    with:\n      key-prefix: my-repo\n"
        rows = run_rows(facts({"ci.yml": workflow(job)}))
        self.assertEqual(failing(rows), {"C2"})

    def test_reusable_workflow_caller_with_rustc_passes(self) -> None:
        job = f"  rust:\n    uses: {RUST_CI}@main\n    with:\n      key-prefix: rustc\n"
        self.assertEqual(failing(run_rows(facts({"ci.yml": workflow(job)}))), set())

    def test_input_expression_resolves_to_its_default(self) -> None:
        on = ("on:\n  workflow_call:\n    inputs:\n      key-prefix:\n        type: string\n        default: {d}\n")
        job = sccache_job(prefix="${{ inputs.key-prefix }}")
        good = facts({"w.yml": workflow(job, on.format(d="rustc"))})
        bad = facts({"w.yml": workflow(job, on.format(d="rustc-2"))})
        self.assertEqual(failing(run_rows(good)), set())
        self.assertEqual(failing(run_rows(bad)), {"C2"})


class C3Test(unittest.TestCase):
    PR_ONLY = "on:\n  pull_request:\n  merge_group:\n"

    def test_pull_request_gate_without_a_main_push_fails(self) -> None:
        rows = run_rows(facts({"ci.yml": workflow(sccache_job(), self.PR_ONLY)}))
        self.assertEqual(failing(rows), {"C3"})
        self.assertIn("ci.yml", rows["C3"]["detail"])

    def test_push_to_tags_only_does_not_warm(self) -> None:
        on = self.PR_ONLY + "  push:\n    tags: ['v*']\n"
        self.assertEqual(failing(run_rows(facts({"ci.yml": workflow(sccache_job(), on)}))), {"C3"})

    def test_push_to_another_branch_does_not_warm(self) -> None:
        on = self.PR_ONLY + "  push:\n    branches: [develop]\n"
        self.assertEqual(failing(run_rows(facts({"ci.yml": workflow(sccache_job(), on)}))), {"C3"})

    def test_default_branch_is_read_from_the_repository(self) -> None:
        on = self.PR_ONLY + "  push:\n    branches: [master]\n"
        self.assertEqual(failing(run_rows(facts({"ci.yml": workflow(sccache_job(), on)}, branch="master"))), set())
        self.assertEqual(failing(run_rows(facts({"ci.yml": workflow(sccache_job(), on)}, branch="main"))), {"C3"})

    def test_gate_called_from_a_main_push_workflow_is_warmed(self) -> None:
        gate = workflow(sccache_job(), self.PR_ONLY + "  workflow_call:\n")
        verify = workflow("  gate:\n    uses: ./.github/workflows/ci.yml\n", "on:\n  push:\n    branches: [main]\n")
        self.assertEqual(failing(run_rows(facts({"ci.yml": gate, "verify.yml": verify}))), set())

    def test_two_level_call_chain_is_followed(self) -> None:
        leaf = workflow(sccache_job(), self.PR_ONLY + "  workflow_call:\n")
        mid = workflow("  a:\n    uses: ./.github/workflows/leaf.yml\n", "on:\n  workflow_call:\n")
        top = workflow("  b:\n    uses: ./.github/workflows/mid.yml\n", "on:\n  push:\n    branches: [main]\n")
        self.assertEqual(failing(run_rows(facts({"leaf.yml": leaf, "mid.yml": mid, "top.yml": top}))), set())

    def test_release_and_scheduled_rust_need_no_main_push(self) -> None:
        on = "on:\n  workflow_dispatch:\n  schedule:\n    - cron: '0 3 * * 0'\n"
        self.assertEqual(failing(run_rows(facts({"nightly.yml": workflow(sccache_job(), on)}))), set())


class ScopeTest(unittest.TestCase):
    def skipped(self, f: dict) -> bool:
        return {r["status"] for r in run_rows(f).values()} == {"SKIP"}

    def test_github_hosted_rust_is_out_of_scope(self) -> None:
        job = "  t:\n    runs-on: ubuntu-latest\n    steps:\n      - run: cargo test\n"
        self.assertTrue(self.skipped(facts({"ci.yml": workflow(job)})))

    def test_macos_and_windows_runners_are_out_of_scope(self) -> None:
        mac = "  t:\n    runs-on: [self-hosted, macos, sylphx, standard]\n    steps:\n      - run: cargo build\n"
        win = "  t:\n    runs-on: sylphx-windows\n    steps:\n      - run: cargo build\n"
        self.assertTrue(self.skipped(facts({"a.yml": workflow(mac), "b.yml": workflow(win)})))

    def test_unresolvable_runner_is_audited(self) -> None:
        job = "  t:\n    runs-on: ${{ inputs.runner }}\n    steps:\n      - run: cargo build\n"
        self.assertEqual(failing(run_rows(facts({"ci.yml": workflow(job)}))), {"C1"})

    def test_no_workflows_directory_is_skip(self) -> None:
        self.assertTrue(self.skipped(facts({})))

    def test_non_rust_repository_is_skip(self) -> None:
        job = "  t:\n    runs-on: sylphx-linux-standard\n    steps:\n      - run: npm test\n"
        self.assertTrue(self.skipped(facts({"ci.yml": workflow(job)})))


class UnreadableTest(unittest.TestCase):
    def test_unreadable_workflows_fail_every_row(self) -> None:
        rows = run_rows(facts(None, errors=["repository unreadable"]))
        self.assertEqual(failing(rows), set(audit.ROWS))

    def test_a_read_error_fails_even_with_text_present(self) -> None:
        f = facts({"ci.yml": workflow(sccache_job())})
        f["errors"] = ["ci.yml unreadable"]
        self.assertEqual(failing(run_rows(f)), set(audit.ROWS))


class EvaluateTest(unittest.TestCase):
    def report(self, fleet: dict, today: datetime.date = TODAY) -> dict:
        return audit.evaluate(fleet, POLICY, compare_fake()[0], today)

    def test_exempt_repositories_are_not_audited(self) -> None:
        fleet = {"SylphxAI": [facts(None, name="cloud", org="SylphxAI") | {"errors": []},
                              facts(None, name="bgca", org="SylphxAI") | {"errors": []}]}
        report = self.report(fleet)
        self.assertEqual([r["status"] for r in report["repos"]], ["EXEMPT", "EXEMPT"])
        self.assertEqual(report["repos"][0]["exemption"]["owner"], "Build")
        self.assertEqual(report["summary"]["FAIL"], 0)

    def test_lapsed_exemption_is_audited_again_with_a_note(self) -> None:
        fleet = {"SylphxAI": [facts({"ci.yml": workflow(sccache_job(prefix="x"))}, name="cloud", org="SylphxAI")]}
        report = self.report(fleet, today=datetime.date(2027, 1, 1))
        record = report["repos"][0]
        self.assertEqual(record["status"], "FAIL")
        self.assertIn("lapsed", record["note"])

    def test_fork_is_exempt(self) -> None:
        report = self.report({"Cubeage": [facts({"ci.yml": workflow(sccache_job(prefix="x"))}, fork=True)]})
        self.assertEqual(report["repos"][0]["status"], "EXEMPT")

    def test_unlisted_organization_fails(self) -> None:
        fleet = {"EpiowAI": [{"org": "EpiowAI", "repo": "EpiowAI/*", "name": "*", "branch": None, "unlisted": True,
                              "errors": ["EpiowAI: repository list unreadable (boom)"]}]}
        report = self.report(fleet)
        self.assertEqual(report["repos"][0]["status"], "FAIL")
        self.assertIn("unreadable", report["repos"][0]["errors"][0])

    def test_summary_counts_and_render_list_only_failures(self) -> None:
        fleet = {"Cubeage": [facts({"ci.yml": workflow(sccache_job())}, name="good"),
                             facts({"ci.yml": workflow(sccache_job(prefix="x"))}, name="bad"),
                             facts({}, name="none")]}
        report = self.report(fleet)
        self.assertEqual(report["summary"], {"repos": 3, "PASS": 1, "FAIL": 1, "EXEMPT": 0, "SKIP": 1})
        text = audit.render(report)
        self.assertIn("FAIL   Cubeage/bad", text)
        self.assertNotIn("Cubeage/good", text)
        self.assertIn("3 repositories: 1 PASS, 1 SKIP, 0 EXEMPT, 1 FAIL", text)


class FakeGh:
    """Answers the audit's GraphQL reads from a table of repositories."""

    def __init__(self, repos: dict[str, dict], fail_batches: bool = False):
        self.repos, self.queries, self.fail_batches = repos, [], fail_batches

    def graphql(self, query: str) -> dict:
        self.queries.append(query)
        if "organization(login" in query:
            nodes = [{"name": n, "isFork": r.get("fork", False), "isPrivate": True, "defaultBranchRef": {"name": "main"}}
                     for n, r in self.repos.items()]
            return {"data": {"organization": {"repositories": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                                                               "nodes": nodes}}}}
        if self.fail_batches:
            raise RuntimeError("502")
        data = {}
        for alias, name in re.findall(r'(r\d+): repository\(owner:"[^"]+",name:"([^"]+)"\)', query):
            files = self.repos[name].get("files")
            data[alias] = {"defaultBranchRef": {"name": "main"},
                           "wf": None if files is None else {"entries": [
                               {"name": k, "object": {"text": v, "isTruncated": False}} for k, v in files.items()]}}
        return {"data": data}


class CollectTest(unittest.TestCase):
    def test_exempt_and_fork_repositories_are_never_queried(self) -> None:
        gh = FakeGh({"cloud": {"files": {}}, "bgca": {"files": {}}, "forked": {"files": {}, "fork": True},
                     "app": {"files": {"ci.yml": workflow(sccache_job())}}})
        fleet = audit.collect(gh, ["SylphxAI"], POLICY, TODAY)
        queried = " ".join(gh.queries[1:])
        self.assertIn('name:"app"', queried)
        for name in ("cloud", "bgca", "forked"):
            self.assertNotIn(f'name:"{name}"', queried)
        report = audit.evaluate(fleet, POLICY, compare_fake()[0], TODAY)
        by = {r["repo"]: r["status"] for r in report["repos"]}
        self.assertEqual(by, {"SylphxAI/app": "PASS", "SylphxAI/bgca": "EXEMPT", "SylphxAI/cloud": "EXEMPT",
                              "SylphxAI/forked": "EXEMPT"})

    def test_repositories_are_read_in_batches(self) -> None:
        gh = FakeGh({f"r{i}": {"files": {}} for i in range(9)})
        audit.collect(gh, ["Cubeage"], POLICY, TODAY, batch=4)
        self.assertEqual(len(gh.queries) - 1, 3)

    def test_a_failed_batch_query_is_a_fail(self) -> None:
        gh = FakeGh({"app": {"files": {}}}, fail_batches=True)
        report = audit.evaluate(audit.collect(gh, ["Cubeage"], POLICY, TODAY), POLICY, compare_fake()[0], TODAY)
        self.assertEqual(report["repos"][0]["status"], "FAIL")
        self.assertIn("query failed", json.dumps(report["repos"][0]))

    def test_a_truncated_workflow_is_a_fail(self) -> None:
        node = {"defaultBranchRef": {"name": "main"},
                "wf": {"entries": [{"name": "ci.yml", "object": {"text": "x", "isTruncated": True}}]}}
        f = audit._facts("Cubeage", {"name": "app"}, node, [])
        self.assertIn("ci.yml unreadable", f["errors"])
        self.assertEqual(failing(run_rows(f)), set(audit.ROWS))

    def test_a_missing_workflows_directory_is_not_an_error(self) -> None:
        f = audit._facts("Cubeage", {"name": "app"}, {"defaultBranchRef": {"name": "main"}, "wf": None}, [])
        self.assertEqual((f["errors"], f["workflows"]), ([], {}))

    def test_a_missing_repository_node_is_unreadable(self) -> None:
        f = audit._facts("Cubeage", {"name": "app"}, None, [])
        self.assertEqual(failing(run_rows(f)), set(audit.ROWS))

    def test_an_unlisted_organization_fails(self) -> None:
        class Broken(FakeGh):
            def graphql(self, query: str) -> dict:
                raise RuntimeError("nope")
        fleet = audit.collect(Broken({}), ["EpiowAI"], POLICY, TODAY)
        self.assertTrue(fleet["EpiowAI"][0]["unlisted"])


class CliTest(unittest.TestCase):
    def test_exit_code_and_json_report_on_saved_facts(self) -> None:
        tmp = pathlib.Path(tempfile.mkdtemp())
        good = facts({"ci.yml": workflow(sccache_job())}, name="good")
        bad = facts({"ci.yml": workflow(sccache_job(prefix="x"))}, name="bad")
        for name, fleet in (("good", [good]), ("bad", [good, bad])):
            (tmp / f"{name}.json").write_text(json.dumps({"Cubeage": fleet}))
        out = tmp / "report.json"
        with mock.patch.object(audit.om, "Gh"):
            self.assertEqual(audit.main(["--facts", str(tmp / "good.json"), "--org", "Cubeage"]), 0)
            self.assertEqual(audit.main(["--facts", str(tmp / "bad.json"), "--org", "Cubeage", "--json", str(out)]), 1)
        report = json.loads(out.read_text())
        self.assertEqual(report["summary"]["FAIL"], 1)
        self.assertEqual(report["policy"], "rust-cache.json")
        self.assertEqual(report["floor"], FLOOR)

    def test_organization_outside_the_policy_is_refused(self) -> None:
        with mock.patch.object(audit.om, "Gh"):
            self.assertEqual(audit.main(["--org", "SomeoneElse"]), 2)


if __name__ == "__main__":
    unittest.main()
