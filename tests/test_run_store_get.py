"""The operator command uses the caller's identities and binds exact keys."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_run_store import ACCESS_KEY, Fake, bundle, regular

HELPER = Path(__file__).resolve().parents[1] / ".github/actions/run-store/get.py"
spec = importlib.util.spec_from_file_location("run_store_get", HELPER)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class ManualGetTest(unittest.TestCase):
    def setUp(self):
        self.fake = Fake()
        self.temp = tempfile.TemporaryDirectory()
        self.dest = Path(self.temp.name) / "retrieved"

    def tearDown(self):
        self.fake.close()
        self.temp.cleanup()

    def invoke(self, rows):
        argv = [str(HELPER), "owner/repository", "4242", "manual", str(self.dest), "--api-key-stdin"]
        env = {"BUILD_CACHE_URL": f"http://127.0.0.1:{self.fake.port}", "RUN_STORE_RETRY_DELAY": "0"}
        with patch.object(sys, "argv", argv), patch.object(sys, "stdin", io.StringIO(ACCESS_KEY)), \
                patch.dict(os.environ, env), patch.object(helper, "metadata", side_effect=rows):
            return helper.main()

    def test_exact_repository_run_and_name_retrieve_the_inner_file(self):
        self.fake.store["retained:run-store.v1.77.4242.manual"] = bundle([regular("inner.zip", b"fixture")])
        self.assertEqual(self.invoke([{"id": 77}, {"id": 4242, "repository": {"id": 77}}]), 0)
        self.assertEqual((self.dest / "inner.zip").read_bytes(), b"fixture")
        self.assertEqual(self.fake.token_requests[-1]["access"], "read")

    def test_mismatched_run_or_repository_never_requests_a_token(self):
        for run in [{"id": 4243, "repository": {"id": 77}}, {"id": 4242, "repository": {"id": 78}}]:
            with self.assertRaisesRegex(ValueError, "identities do not match"):
                self.invoke([{"id": 77}, run])
        self.assertFalse(self.fake.requests)
        self.assertFalse(self.dest.exists())

    def test_missing_entry_fails_without_creating_a_destination(self):
        self.assertNotEqual(self.invoke([{"id": 77}, {"id": 4242, "repository": {"id": 77}}]), 0)
        self.assertFalse(self.dest.exists())

    def test_corrupt_gzip_with_valid_transport_checksum_leaves_no_files(self):
        corrupt = bytearray(bundle([regular("inner.zip", b"fixture")]))
        corrupt[-8] ^= 1  # corrupt the gzip CRC while retaining a valid tar body
        self.fake.store["retained:run-store.v1.77.4242.manual"] = bytes(corrupt)
        self.assertNotEqual(self.invoke([{"id": 77}, {"id": 4242, "repository": {"id": 77}}]), 0)
        self.assertFalse(self.dest.exists())

    def test_repository_argument_cannot_be_a_path_or_command(self):
        with patch.object(sys, "argv", [str(HELPER), "../repository;echo", "4242", "manual", str(self.dest)]), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            helper.main()
        self.assertEqual(error.exception.code, 2)
        self.assertFalse(self.fake.requests)


if __name__ == "__main__":
    unittest.main()
