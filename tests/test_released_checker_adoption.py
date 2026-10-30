"""Released checker pins remain valid inputs to the supported adoption CLI."""
import base64
import contextlib
import copy
import hashlib
import io
import tempfile
import unittest
from unittest import mock

from test_rule_adoption import (
    ROOT, SOURCE, POLL, UNKNOWN, WORKFLOW, Fixture, sc, workflow,
)


RELEASES = (
    "4dcc910606d7624150d2004f2dd970eb570de30e",
    "9d1931731b8442a83b61203883dfd1a965e8e349",
    "0a8e0782e55506e4c61062f55a77428d7186d799",
    "b829fa2e3881ceadc0eaecdd39bf50e6611cff2b",
    "a133c488a95f46ad5770a839d410231a9e668c52",
)
TENANT_RULE = "architecture/C2/tenant-read-functions"
DEAL_RULE = "architecture/C1/deal-randomness"
TENANT_RELEASE = "b7bc8bacfa4852175676a41f0b51068998bafd5a"


class ReleasedCheckerAdoptionTests(unittest.TestCase):
    def test_initial_caller_seeds_only_inherited_stack_departures_when_no_baseline_exists(self):
        manifest = '[database.migrations]\nengine = "prisma"\n'
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            base = fixture.commit({"sylphx.toml": manifest})
            fixture.commit({WORKFLOW: workflow(UNKNOWN)})
            plan = fixture.plan(base)
            self.assertIn("migrations sylphx.toml prisma", plan["evidence"]["added_records"])
            adopted = fixture.commit({sc.BASELINE: plan["baseline_text"]})
            fixture.check(base, 0)
            fixture.commit({"sylphx.toml": manifest.replace("prisma", "other")})
            fixture.check(adopted, 1)
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            base = fixture.commit({"README.md": "existing repository\n"})
            fixture.commit({WORKFLOW: workflow(UNKNOWN), "sylphx.toml": manifest})
            with self.assertRaisesRegex(ValueError, "new head stack departure"):
                fixture.plan(base)

    def test_initial_caller_adopts_inherited_rules_and_then_refuses_growth(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            baseline = "# preserve original bytes\n"
            base = fixture.commit({SOURCE: POLL, sc.BASELINE: baseline,
                                   "sylphx.toml": '[resources.database]\ntier = "starter"\n'})
            with self.assertRaisesRegex(ValueError, "no immutable old"):
                fixture.plan(base)
            fixture.commit({WORKFLOW: workflow(UNKNOWN)})
            plan = fixture.plan(base)
            self.assertEqual(plan["evidence"]["old_checkers"], [])
            self.assertEqual(set(plan["introduced_rules"]), sc.adoption.current_rules())
            self.assertEqual(len(plan["evidence"]["added_records"]), 2)
            self.assertTrue(plan["baseline_text"].startswith(baseline))
            adopted = fixture.commit({sc.BASELINE: plan["baseline_text"]})
            fixture.check(base, 0)
            fixture.commit({SOURCE: POLL + POLL})
            output, _ = fixture.check(adopted, 1)
            self.assertIn("polling-loop", output)

    def test_initial_caller_refuses_new_debt_and_unapproved_cache_seeds(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            base = fixture.commit({SOURCE: POLL})
            fixture.commit({WORKFLOW: workflow(UNKNOWN), SOURCE: POLL + POLL})
            with self.assertRaisesRegex(ValueError, "new head debt"):
                fixture.plan(base)
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            path = "src/shop.ts"
            cached = "export function purchase() { return offerCache.get(1); }\n"
            base = fixture.commit({path: cached})
            fixture.commit({WORKFLOW: workflow(UNKNOWN)})
            with self.assertRaisesRegex(ValueError, "unapproved inherited cache read"):
                fixture.plan(base)
            row, = sc.architecture.scan_file(path, cached, "example/app", set(), [])
            fixture.commit({sc.BASELINE: sc.architecture.BASELINE_MARKER + "\n"
                + sc.adoption.RULE_MARKER + sc.adoption.CACHE_RULE + "\n"
                + sc.architecture.baseline_key(row) + "\n"})
            output, _ = fixture.check(base, 1)
            self.assertIn("unapproved inherited cache read", output)

    def test_required_cli_resolves_alias_without_offline_argument_and_refuses_unavailable_source(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            base = fixture.commit({WORKFLOW: workflow(RELEASES[-1])})
            fixture.commit({WORKFLOW: workflow(UNKNOWN), sc.BASELINE:
                            sc.adoption.RULE_MARKER + TENANT_RULE + "\n"
                            + sc.architecture.BASELINE_MARKER + "\n"})
            argv = ["check", "--repo", directory, "--base", base,
                    "--repository", "example/app", "--report", directory + "/report.json"]
            proof = {"head": UNKNOWN, "mode": "test source", "offenders": []}
            for unavailable, expected in ((False, 0), (True, 1)):
                with self.subTest(unavailable=unavailable), \
                        mock.patch.object(sc.adoption, "pin_provenance", return_value=proof), \
                        mock.patch.object(sc.adoption, "PublishedCheckerTree",
                            side_effect=ValueError("source unavailable") if unavailable else None,
                            return_value=sc.Tree(str(ROOT), RELEASES[-1])) as reader, \
                        contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(sc.main(argv), expected)
                    reader.assert_called_once_with(RELEASES[-1])

    def test_public_source_reader_verifies_commit_tree_and_blob_content(self):
        pin = RELEASES[-1]
        actual_tree = sc.Tree(str(ROOT), pin)
        names = [sc.adoption.PREFIX + name for name in
                 ("action.yml", "stack_conformance.py", "architecture.py", "model_pins.py")]
        blobs = {}
        for name in names:
            raw = actual_tree.read(name).encode()
            sha = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
            blobs[name] = (sha, raw)
        tree_sha = "1" * 40
        responses = {
            "repos/SylphxAI/.github/commits/" + pin: {"sha": pin, "commit": {
                "committer": {"date": "2026-10-30T07:28:40Z"}, "tree": {"sha": tree_sha}}},
            "repos/SylphxAI/.github/git/trees/" + tree_sha + "?recursive=1": {
                "sha": tree_sha, "truncated": False, "tree": [
                    {"path": name, "type": "blob", "mode": "100644", "sha": sha}
                    for name, (sha, _) in blobs.items()]},
            **{"repos/SylphxAI/.github/git/blobs/" + sha: {"sha": sha,
                "encoding": "base64", "content": base64.b64encode(raw).decode()}
               for sha, raw in blobs.values()},
        }
        gh = mock.Mock()
        gh.rest.side_effect = lambda path: responses[path]
        tree = sc.adoption.PublishedCheckerTree(pin, gh)
        self.assertEqual(sc.adoption.source_profile(tree), sc.adoption.source_profile(actual_tree))
        responses["repos/SylphxAI/.github/git/trees/" + tree_sha + "?recursive=1"]["truncated"] = True
        with self.assertRaisesRegex(ValueError, "tree is incomplete"):
            sc.adoption.PublishedCheckerTree(pin, gh)
        sha, _ = blobs[names[0]]
        responses["repos/SylphxAI/.github/git/blobs/" + sha]["content"] = base64.b64encode(b"changed").decode()
        with self.assertRaisesRegex(ValueError, "blob content mismatch"):
            tree.read(names[0])
        responses["repos/SylphxAI/.github/commits/" + pin]["sha"] = "2" * 40
        with self.assertRaisesRegex(ValueError, "does not match requested SHA"):
            sc.adoption.PublishedCheckerTree(pin, gh)

    def test_unlisted_commit_requires_immutable_reviewed_source(self):
        alias = RELEASES[-1]
        tree = sc.DictTree({WORKFLOW: workflow(alias)})
        self.assertNotIn(alias, sc.adoption.load_registry()["commits"])
        with self.assertRaisesRegex(ValueError, "unknown old checker pin"):
            sc.adoption.old_checkers(tree)
        actual = sc.adoption.source_profile(sc.Tree(str(ROOT), alias))
        expected = sc.adoption.profile_id(actual)
        self.assertIn(expected, sc.adoption.load_registry()["profiles"])
        self.assertEqual(sc.adoption.old_checkers(tree, shared_repo=str(ROOT))[0]["profile"], expected)

    def test_unlisted_changed_or_unavailable_source_still_refuses(self):
        tree = sc.DictTree({WORKFLOW: workflow(RELEASES[-1])})
        actual = sc.adoption.source_profile(sc.Tree(str(ROOT), RELEASES[-1]))
        changed = copy.deepcopy(actual)
        changed["sources"][sc.adoption.PREFIX + "architecture.py"] = "0" * 64
        with mock.patch.object(sc.adoption, "source_profile", return_value=changed):
            with self.assertRaisesRegex(ValueError, "source profile has not been reviewed"):
                sc.adoption.old_checkers(tree, shared_repo=str(ROOT))
        with mock.patch.object(sc.adoption, "source_profile", side_effect=ValueError("unreadable source")):
            with self.assertRaisesRegex(ValueError, "source is unavailable or invalid"):
                sc.adoption.old_checkers(tree, shared_repo=str(ROOT))

    def test_existing_tenant_release_preserves_baseline_without_reseeding(self):
        self.assertNotIn(TENANT_RELEASE, sc.adoption.load_registry()["commits"])
        row, = sc.architecture.scan_file(SOURCE, POLL, "example/app", set(), [])
        baseline = ("# preserved tenant release baseline\n" + sc.architecture.BASELINE_MARKER
                    + "\n" + sc.adoption.RULE_MARKER + "architecture/C5/polling-loop\n"
                    + sc.architecture.baseline_key(row) + "\n")
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            base = fixture.commit({WORKFLOW: workflow(TENANT_RELEASE), SOURCE: POLL,
                                   sc.BASELINE: baseline})
            plan = fixture.plan(base)
            self.assertEqual(plan["introduced_rules"], [DEAL_RULE, "force-push"])
            self.assertEqual(plan["evidence"]["added_records"], [])
            self.assertEqual(plan["baseline_text"], baseline + sc.no_force_push.BASELINE_MARKER + "\n"
                             + sc.adoption.RULE_MARKER + DEAL_RULE + "\n"
                             + sc.adoption.RULE_MARKER + "force-push\n")
            self.assertEqual(len(plan["evidence"]["old_checkers"][0]["rules"]), 18)

    def test_released_profiles_match_immutable_source_and_enforce_existing_rules(self):
        for pin in RELEASES:
            with self.subTest(pin=pin):
                profiles = sc.adoption.old_checkers(
                    sc.DictTree({WORKFLOW: workflow(pin)}), shared_repo=str(ROOT))
                self.assertEqual(len(profiles[0]["rules"]), 17)
                self.assertIn("model-pin", profiles[0]["rules"])
                self.assertIn("architecture/C1/cache-economy", profiles[0]["rules"])
                self.assertIn("architecture/C5/polling-loop", profiles[0]["rules"])
                self.assertNotIn(TENANT_RULE, profiles[0]["rules"])

    def test_released_pin_adoption_preserves_baseline_and_does_not_reopen_growth(self):
        row, = sc.architecture.scan_file(SOURCE, POLL, "example/app", set(), [])
        baseline = ("# preserved release baseline\n" + sc.architecture.BASELINE_MARKER
                    + "\n" + sc.adoption.RULE_MARKER + "architecture/C5/polling-loop\n"
                    + sc.architecture.baseline_key(row) + "\n")
        for pin in RELEASES:
            with self.subTest(pin=pin), tempfile.TemporaryDirectory() as directory:
                fixture = Fixture(directory)
                base = fixture.commit({WORKFLOW: workflow(pin), SOURCE: POLL,
                                       sc.BASELINE: baseline})
                plan = fixture.plan(base)
                self.assertEqual(plan["introduced_rules"], [DEAL_RULE, TENANT_RULE, "force-push"])
                self.assertEqual(plan["evidence"]["added_records"], [])
                self.assertEqual(plan["baseline_text"], baseline
                                 + sc.no_force_push.BASELINE_MARKER + "\n"
                                 + sc.adoption.RULE_MARKER + DEAL_RULE + "\n"
                                 + sc.adoption.RULE_MARKER + TENANT_RULE + "\n"
                                 + sc.adoption.RULE_MARKER + "force-push\n")
                fixture.commit({WORKFLOW: workflow(UNKNOWN),
                                sc.BASELINE: plan["baseline_text"]})
                fixture.check(base, 0)

                fixture.commit({SOURCE: POLL + POLL})
                output, report = fixture.check(base, 1)
                self.assertEqual(len([finding for finding in report["findings"]
                                      if finding["rule"] == "polling-loop"]), 2)
                self.assertIn("polling-loop", output)


if __name__ == "__main__":
    unittest.main()
