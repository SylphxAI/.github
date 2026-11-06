"""Run the dependency-free publisher contract in the repository's normal suite."""
import pathlib
import subprocess
import unittest


class PrReviewTests(unittest.TestCase):
    def test_review_contract(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        subprocess.run(["node", "--test", "tests/pr_review.test.mjs", "tests/pr_review_binary.test.mjs", "tests/pr_review_gateway.test.mjs", "tests/pr_review_budget.test.mjs", "tests/pr_review_recovery.test.mjs", "tests/pr_review_synthesis_context.test.mjs", "tests/pr_review_final_round.test.mjs", "tests/pr_review_ws_snap.test.mjs", "tests/pr_review_corrective_retry.test.mjs", "tests/pr_review_terminal.test.mjs"],
                       cwd=root, check=True, timeout=30)
