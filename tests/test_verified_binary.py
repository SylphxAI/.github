"""Hash-before-probe and private-copy behavior for every pre-baked tool consumer."""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('verified_binary', ROOT / '.github/actions/_shared/verified_binary.py')
VERIFY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFY)


class VerifiedBinaryTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        self.candidate = self.root / 'image-binary'
        self.destination = self.root / 'private-binary'
        self.probed = self.root / 'probed'
        self.body = ('#!/bin/sh\nprintf "%s" "$0" > "' + str(self.probed) +
                     '"\nprintf "1.2.3\\n"\n').encode()
        self.candidate.write_bytes(self.body)
        self.candidate.chmod(0o700)
        self.digest = hashlib.sha256(self.body).hexdigest()

    def verify(self, **overrides):
        kwargs = dict(sha256=self.digest, version='1.2.3', version_args=('--version',))
        kwargs.update(overrides)
        return VERIFY.copy_verified(self.candidate, self.destination, **kwargs)

    def test_hash_and_exact_version_publish_the_same_private_bytes_that_were_probed(self):
        self.assertTrue(self.verify())
        self.assertEqual(self.destination.read_bytes(), self.body)
        self.assertNotEqual(self.probed.read_text(), str(self.candidate))
        self.assertEqual(Path(self.probed.read_text()).name, self.destination.name)
        self.assertEqual(set(self.root.iterdir()), {self.candidate, self.destination, self.probed})

    def test_wrong_hash_is_never_executed_and_preserves_existing_destination(self):
        self.destination.write_bytes(b'previous trusted tool')
        self.assertFalse(self.verify(sha256='0' * 64))
        self.assertFalse(self.probed.exists())
        self.assertEqual(self.destination.read_bytes(), b'previous trusted tool')
        self.assertEqual(set(self.root.iterdir()), {self.candidate, self.destination})

    def test_hash_match_without_exact_version_never_publishes(self):
        self.assertFalse(self.verify(version='1.2.30'))
        self.assertTrue(self.probed.exists())
        self.assertFalse(self.destination.exists())
        self.assertEqual(set(self.root.iterdir()), {self.candidate, self.probed})

    def test_missing_candidate_and_failed_probe_leave_no_partial_destination(self):
        self.candidate.unlink()
        self.assertFalse(self.verify())
        self.assertEqual(list(self.root.iterdir()), [])
        self.candidate.write_bytes(self.body)
        with mock.patch.object(VERIFY.subprocess, 'run', side_effect=OSError('probe unavailable')):
            self.assertFalse(self.verify())
        self.assertEqual(set(self.root.iterdir()), {self.candidate})


if __name__ == '__main__':
    unittest.main()
