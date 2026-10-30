"""Released checker pins remain valid inputs to the supported adoption CLI."""
import tempfile
import unittest

from test_rule_adoption import (
    ROOT, SOURCE, POLL, UNKNOWN, WORKFLOW, Fixture, sc, workflow,
)


RELEASES = (
    "4dcc910606d7624150d2004f2dd970eb570de30e",
    "9d1931731b8442a83b61203883dfd1a965e8e349",
    "0a8e0782e55506e4c61062f55a77428d7186d799",
)


class ReleasedCheckerAdoptionTests(unittest.TestCase):
    def test_released_profiles_match_immutable_source_and_enforce_existing_rules(self):
        for pin in RELEASES:
            with self.subTest(pin=pin):
                profiles = sc.adoption.old_checkers(
                    sc.DictTree({WORKFLOW: workflow(pin)}), shared_repo=str(ROOT))
                self.assertEqual(len(profiles[0]["rules"]), 17)
                self.assertIn("model-pin", profiles[0]["rules"])
                self.assertIn("architecture/C1/cache-economy", profiles[0]["rules"])
                self.assertIn("architecture/C5/polling-loop", profiles[0]["rules"])

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
                self.assertEqual(plan["introduced_rules"], [])
                self.assertEqual(plan["evidence"]["added_records"], [])
                self.assertEqual(plan["baseline_text"], baseline)
                fixture.commit({WORKFLOW: workflow(UNKNOWN)})
                fixture.check(base, 0)

                fixture.commit({SOURCE: POLL + POLL})
                output, report = fixture.check(base, 1)
                self.assertEqual(len([finding for finding in report["findings"]
                                      if finding["rule"] == "polling-loop"]), 2)
                self.assertIn("polling-loop", output)


if __name__ == "__main__":
    unittest.main()
