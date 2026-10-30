"""Initial adoption cannot turn an inherited economy read into approved debt."""
import json
import subprocess
import tempfile
import unittest

from test_rule_adoption import ACTION, ROOT, TODAY, Fixture, OLD, UNKNOWN, WORKFLOW, sc, workflow

a = sc.adoption
SOURCE = "src/shop.ts"
BAD = "export function purchase() { return offerCache.get(1); }\n"


class CacheAdoptionPolicyTests(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Fixture(temporary.name)

    def test_unapproved_inherited_read_refused_by_helper_and_required_cli(self):
        f = self.fixture()
        base = f.commit({WORKFLOW: workflow(OLD), SOURCE: BAD})
        with self.assertRaisesRegex(ValueError, "unapproved inherited cache read"):
            f.plan(base)
        row = sc.architecture.scan_file(SOURCE, BAD, "example/app", set(), [])[0]
        baseline = (sc.architecture.BASELINE_MARKER + "\n" + a.RULE_MARKER + a.CACHE_RULE + "\n"
                    + sc.architecture.baseline_key(row) + "\n")
        f.commit({WORKFLOW: workflow(UNKNOWN), sc.BASELINE: baseline})
        output, _ = f.check(base, 1)
        self.assertIn("unapproved inherited cache read", output)

    def test_pin_only_adoption_cannot_omit_marker_to_skip_approval(self):
        f = self.fixture()
        base = f.commit({WORKFLOW: workflow(OLD), SOURCE: BAD})
        f.commit({WORKFLOW: workflow(UNKNOWN)})
        output, _ = f.check(base, 1)
        self.assertIn("unapproved inherited cache read", output)

    def test_existing_marker_is_not_proof_of_prior_approval(self):
        f = self.fixture()
        row = sc.architecture.scan_file(SOURCE, BAD, "example/app", set(), [])[0]
        baseline = (sc.architecture.BASELINE_MARKER + "\n" + a.RULE_MARKER + a.CACHE_RULE + "\n"
                    + sc.architecture.baseline_key(row) + "\n")
        base = f.commit({WORKFLOW: workflow(OLD), SOURCE: BAD, sc.BASELINE: baseline})
        f.commit({WORKFLOW: workflow(UNKNOWN)})
        output, _ = f.check(base, 1)
        self.assertIn("unapproved inherited cache read", output)

    def test_current_pin_unchanged_preserves_existing_ci(self):
        f = self.fixture()
        base = f.commit({WORKFLOW: workflow(OLD), SOURCE: BAD})
        f.commit({"README.md": "unrelated documentation\n"})
        _, report = f.check(base, 0)
        self.assertEqual(report["findings"][0]["disposition"], "inherited")

    def test_approved_expression_survives_unrelated_line_but_not_read_edit(self):
        policy = json.loads(a.CACHE_APPROVALS.read_text())
        binding = policy["reads"][0]["bindings"][0]
        path = binding["record"].split()[3]
        source = ("class Display {\n public void buyDisplay() {\n"
                  "  ShopCommodityTemplateCache.getShopCommodityTemplateCache().getShopCommodityTemplate(1);\n"
                  " }\n}\n")
        def scan(text):
            return sc.architecture.scan_file(path, text, binding["repository"], set(), [])
        self.assertEqual(sc.architecture.baseline_key(scan(source)[0]), binding["record"])
        self.assertFalse(a.cache_approval_errors(scan(source), binding["repository"]))
        self.assertFalse(a.cache_approval_errors(scan("// unrelated line\n" + source), binding["repository"]))
        self.assertTrue(a.cache_approval_errors(scan(source.replace("getShopCommodityTemplateCache", "getOtherCommodityTemplateCache")), binding["repository"]))
        self.assertTrue(a.cache_approval_errors(scan(source + source), binding["repository"]))
        self.assertTrue(a.cache_approval_errors(scan(source), "example/other"))

    def test_policy_has_32_reviewed_reads_and_43_exact_bindings(self):
        policy = json.loads(a.CACHE_APPROVALS.read_text())
        self.assertEqual(len(policy["reads"]), 32)
        self.assertEqual(sum(len(r["bindings"]) for r in policy["reads"]), 43)
        for read in policy["reads"]:
            self.assertTrue(read["reason"].strip())
        self.assertFalse(a.cache_approval_errors([], "example/app"))

    def test_approved_read_adopts_then_ratchets_through_required_cli(self):
        binding = json.loads(a.CACHE_APPROVALS.read_text())["reads"][0]["bindings"][0]
        path = binding["record"].split()[3]
        source = ("class Display {\n public void buyDisplay() {\n"
                  "  ShopCommodityTemplateCache.getShopCommodityTemplateCache().getShopCommodityTemplate(1);\n"
                  " }\n}\n")
        f = self.fixture()
        base = f.commit({WORKFLOW: workflow(OLD), path: source})
        result = a.plan_adoption(sc.Tree(str(f.root), base), sc.Tree(str(f.root), "HEAD"),
                                 binding["repository"], shared_repo=str(ROOT), today=TODAY)
        self.assertIn(binding["record"], result["evidence"]["added_records"])
        adopted = f.commit({WORKFLOW: workflow(UNKNOWN), sc.BASELINE: result["baseline_text"]})

        def check(base, expected):
            report = f.root / "report.json"
            process = subprocess.run(["python3", str(ACTION / "stack_conformance.py"), "check",
                "--repo", str(f.root), "--base", base, "--repository", binding["repository"],
                "--report", str(report), "--shared-source", str(ROOT)], capture_output=True, text=True)
            self.assertEqual(process.returncode, expected, process.stdout + process.stderr)
            return process.stdout

        check(base, 0)
        f.commit({path: "// unrelated line\n" + source})
        check(adopted, 0)
        previous = f.git("rev-parse", "HEAD")
        f.commit({path: source.replace("getShopCommodityTemplateCache", "getOtherCommodityTemplateCache")})
        self.assertIn("stale architecture baseline", check(previous, 1))


if __name__ == "__main__":
    unittest.main()
