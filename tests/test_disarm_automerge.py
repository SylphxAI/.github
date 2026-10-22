"""Exercise the exact reusable workflow script with a fake GitHub client."""
import json
from pathlib import Path
import subprocess
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/disarm-automerge-on-push.yml").read_text())
SCRIPT = WORKFLOW["jobs"]["disarm"]["steps"][0]["with"]["script"]


def run_script(event, pr):
    harness = """
    const data = JSON.parse(process.argv[1]);
    const calls = [];
    const context = {payload: data.event, repo: {owner: 'test', repo: 'repo'}};
    const core = {info: () => {}};
    const github = {graphql: async (query, variables) => {
      calls.push({query, variables});
      return {repository: {pullRequest: data.pr}};
    }};
    (async () => { SCRIPT })().then(() => console.log(JSON.stringify(calls)),
      e => { console.error(e.message); process.exit(1); });
    """.replace("SCRIPT", SCRIPT)
    return subprocess.run(["node", "-e", harness, json.dumps({"event": event, "pr": pr})], capture_output=True, text=True)


class DisarmTest(unittest.TestCase):
    def setUp(self):
        self.event = {"after": "new", "pull_request": {"number": 7, "auto_merge": {"enabled_by": {"id": 42}}, "updated_at": "2026-10-01T12:00:00Z"}}
        self.pr = {"id": "PR_7", "state": "OPEN", "headRefOid": "new", "mergeQueueEntry": None, "autoMergeRequest": {"enabledAt": "2026-10-01T11:59:00Z"}}

    def assert_mutations(self, count):
        result = run_script(self.event, self.pr)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = json.loads(result.stdout)
        self.assertEqual(sum("disablePullRequestAutoMerge" in c["query"] for c in calls), count)
        if count:
            self.assertEqual(calls[-1]["variables"], {"id": "PR_7"})

    def test_earlier_head_arm_is_disabled(self):
        self.assert_mutations(1)

    def test_unarmed_at_push_is_preserved(self):
        self.event["pull_request"]["auto_merge"] = None
        self.assert_mutations(0)

    def test_later_and_same_second_rearms_are_preserved(self):
        for date in ("2026-10-01T12:00:00Z", "2026-10-01T12:00:01Z"):
            self.pr["autoMergeRequest"]["enabledAt"] = date
            self.assert_mutations(0)

    def test_later_push_is_preserved(self):
        self.pr["headRefOid"] = "newer"
        self.assert_mutations(0)

    def test_same_head_queue_entry_never_dequeued(self):
        self.pr["mergeQueueEntry"] = {"headCommit": {"oid": "new"}}
        self.assert_mutations(0)

    def test_old_head_queue_entry_does_not_hide_stale_arm(self):
        self.pr["mergeQueueEntry"] = {"headCommit": {"oid": "old"}}
        self.assert_mutations(1)

    def test_closed_or_already_disarmed_is_noop(self):
        self.pr["state"] = "CLOSED"
        self.assert_mutations(0)
        self.pr["state"] = "OPEN"
        self.pr["autoMergeRequest"] = None
        self.assert_mutations(0)

    def test_invalid_timestamp_fails_without_mutation(self):
        self.event["pull_request"]["updated_at"] = "invalid"
        result = run_script(self.event, self.pr)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid push or arm timestamp", result.stderr)

    def test_no_checkout_or_paid_runner(self):
        self.assertEqual(WORKFLOW["jobs"]["disarm"]["runs-on"], "sylphx-linux-standard")
        self.assertNotIn("checkout", json.dumps(WORKFLOW))
        self.assertEqual(WORKFLOW["permissions"]["pull-requests"], "write")
