"""Run the dependency-free publisher contract in the repository's normal suite."""
import pathlib
import subprocess
import unittest


class PrReviewTests(unittest.TestCase):
    def test_review_contract(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        # This ceiling catches hangs; individual Node tests retain their own timeouts.
        contracts = sorted(str(path.relative_to(root)) for path in
                           (root / "tests").glob("pr_review*.test.mjs"))
        self.assertTrue(contracts, "No review contracts found")
        subprocess.run(["node", "--test", *contracts],
                       cwd=root, check=True, timeout=600)
