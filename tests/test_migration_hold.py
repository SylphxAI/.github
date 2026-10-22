#!/usr/bin/env python3
"""Tests for the red-main migration queue hold."""

from __future__ import annotations

import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "migration_hold", ROOT / ".github" / "actions" / "migration-hold" / "migration_hold.py")
mh = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mh)

FIX = "red-main-forward-fix"
REPO = "o/r"


class DecideTest(unittest.TestCase):
    def test_no_hold_passes(self):
        self.assertEqual(mh.decide("merge_group", [], {}, FIX)[0], "pass")

    def test_open_hold_blocks_a_plain_pull_request_and_names_the_way_out(self):
        verdict, message = mh.decide("merge_group", [7], {5: ["owner:ops"]}, FIX)
        self.assertEqual(verdict, "fail")
        self.assertIn("#7", message)
        self.assertIn("merge the forward-fix", message)
        self.assertIn("close #7", message)

    def test_forward_fix_label_passes(self):
        self.assertEqual(mh.decide("merge_group", [7, 9], {5: [FIX]}, FIX)[0], "pass")

    def test_every_pull_request_of_a_batched_group_needs_the_label(self):
        self.assertEqual(mh.decide("merge_group", [7], {5: [FIX], 6: []}, FIX)[0], "fail")
        self.assertEqual(mh.decide("merge_group", [7], {5: [FIX], 6: [FIX]}, FIX)[0], "pass")

    def test_unidentified_group_with_a_hold_fails(self):
        self.assertEqual(mh.decide("merge_group", [7], {}, FIX)[0], "fail")

    def test_only_the_queue_is_held(self):
        self.assertEqual(mh.decide("pull_request", [7], {}, FIX)[0], "pass")
        self.assertEqual(mh.decide("push", [7], {}, FIX)[0], "pass")


class PrNumberTest(unittest.TestCase):
    def test_parses_the_queue_ref(self):
        self.assertEqual(mh.pr_number("refs/heads/gh-readonly-queue/main/pr-4123-0a1b2c3d4e"), 4123)

    def test_unparseable(self):
        self.assertIsNone(mh.pr_number(""))
        self.assertIsNone(mh.pr_number("refs/heads/main"))


def env(**extra):
    base = {"EVENT": "merge_group", "REPO": REPO, "TOKEN": "t",
            "HEAD_REF": "refs/heads/gh-readonly-queue/main/pr-5-abc123", "BASE_SHA": "", "HEAD_SHA": ""}
    base.update(extra)
    return base


def fake_api(issues, pr_labels=None, compare=None, pulls=None, boom=None, subjects=None, total=None):
    calls = []

    def api(path, token):
        calls.append(path)
        if boom is not None:
            raise boom
        if "/issues?state=open" in path:
            return issues
        if "/compare/" in path:
            return {"commits": [{"sha": s, "commit": {"message": (subjects or {}).get(s, "no number")}} for s in (compare or [])],
                    "total_commits": total if total is not None else len(compare or [])}
        if "/pulls" in path:
            return [{"number": n} for n in (pulls or {}).get(path.split("/commits/")[1].split("/")[0], [])]
        num = int(path.rsplit("/", 1)[1])
        return {"labels": [{"name": l} for l in (pr_labels or {}).get(num, [])]}

    api.calls = calls
    return api


class MainTest(unittest.TestCase):
    def run_main(self, e, api):
        sleeps = []
        code = mh.main(e, api, sleeps.append)
        return code, sleeps

    def test_403_fails_closed_after_three_tries(self):
        api = fake_api([], boom=RuntimeError("HTTP Error 403: Forbidden"))
        code, sleeps = self.run_main(env(), api)
        self.assertEqual(code, 1)
        self.assertEqual(len(api.calls), 3)
        self.assertEqual(sleeps, [2, 4])

    def test_timeout_fails_closed(self):
        code, _ = self.run_main(env(), fake_api([], boom=TimeoutError("timed out")))
        self.assertEqual(code, 1)

    def test_empty_result_passes(self):
        code, _ = self.run_main(env(), fake_api([]))
        self.assertEqual(code, 0)

    def test_pull_requests_are_not_holds(self):
        code, _ = self.run_main(env(), fake_api([{"number": 9, "pull_request": {}}]))
        self.assertEqual(code, 0)

    def test_open_hold_with_an_unidentified_group_fails(self):
        code, _ = self.run_main(env(HEAD_REF="refs/heads/main"), fake_api([{"number": 7}]))
        self.assertEqual(code, 1)

    def test_open_hold_passes_a_labelled_single_pr(self):
        code, _ = self.run_main(env(), fake_api([{"number": 7}], pr_labels={5: [FIX]}))
        self.assertEqual(code, 0)

    def test_batched_group_needs_every_pr_labelled(self):
        common = dict(compare=["c1", "c2"], pulls={"c1": [4], "c2": [5]})
        e = env(BASE_SHA="b", HEAD_SHA="h")
        code, _ = self.run_main(e, fake_api([{"number": 7}], pr_labels={5: [FIX], 4: []}, **common))
        self.assertEqual(code, 1)
        code, _ = self.run_main(e, fake_api([{"number": 7}], pr_labels={5: [FIX], 4: [FIX]}, **common))
        self.assertEqual(code, 0)

    def test_squash_commits_are_attributed_by_their_subject(self):
        # commits/{sha}/pulls is empty for squash queue commits.
        e = env(BASE_SHA="b", HEAD_SHA="h")
        common = dict(compare=["c1"], pulls={}, subjects={"c1": "fix the thing (#4)"})
        code, _ = self.run_main(e, fake_api([{"number": 7}], pr_labels={5: [FIX], 4: []}, **common))
        self.assertEqual(code, 1)
        code, _ = self.run_main(e, fake_api([{"number": 7}], pr_labels={5: [FIX], 4: [FIX]}, **common))
        self.assertEqual(code, 0)

    def test_an_unmapped_commit_fails(self):
        e = env(BASE_SHA="b", HEAD_SHA="h")
        api = fake_api([{"number": 7}], pr_labels={5: [FIX]}, compare=["c1"], pulls={}, subjects={"c1": "chore: no number"})
        code, sleeps = self.run_main(e, api)
        self.assertEqual((code, sleeps), (1, []))  # not retried

    def test_a_capped_compare_fails(self):
        e = env(BASE_SHA="b", HEAD_SHA="h")
        api = fake_api([{"number": 7}], pr_labels={5: [FIX], 4: [FIX]}, compare=["c1"], pulls={"c1": [4]}, total=300)
        code, _ = self.run_main(e, api)
        self.assertEqual(code, 1)

    def test_api_answer_wins_over_the_subject(self):
        self.assertEqual(mh.commit_pr("s", "x (#9)", [{"number": 4}]), 4)
        self.assertEqual(mh.commit_pr("s", "x (#9)", []), 9)
        self.assertIsNone(mh.commit_pr("s", "x (#9) tail", []))

    def test_label_is_url_encoded(self):
        api = fake_api([])
        self.run_main(env(HOLD_LABEL="a b&c"), api)
        self.assertIn("labels=a%20b%26c", api.calls[0])

    def test_other_events_pass_without_calling_the_api(self):
        api = fake_api([], boom=RuntimeError("must not be called"))
        code, _ = self.run_main(env(EVENT="pull_request"), api)
        self.assertEqual((code, api.calls), (0, []))


class ManifestTest(unittest.TestCase):
    def test_action_manifest_parses_and_passes_the_group_shas(self):
        import yaml
        doc = yaml.safe_load((ROOT / ".github/actions/migration-hold/action.yml").read_text())
        self.assertEqual(doc["runs"]["using"], "composite")
        env_block = doc["runs"]["steps"][0]["env"]
        self.assertIn("BASE_SHA", env_block)
        self.assertIn("HEAD_SHA", env_block)


if __name__ == "__main__":
    unittest.main()
