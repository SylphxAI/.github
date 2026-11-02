"""Literal boundaries and real added-line history; no committed count baseline."""
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / '.github/actions/literal-address-guard/check.py'
spec = importlib.util.spec_from_file_location('literal_address_guard', SCRIPT)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class Tokens(unittest.TestCase):
    def test_urls_ports_ipv6_and_boundaries(self):
        fixtures = {
            'https://192.0.2.10:443/path': ['192.0.2.10/32'],
            'https://192.0.2.10/health': ['192.0.2.10/32'],
            'http://192.0.2.10/': ['192.0.2.10/32'],
            'https://192.0.2.10/24': ['192.0.2.10/32'],
            '//192.0.2.10/99': ['192.0.2.10/32'],
            'http://user@192.0.2.10/24': ['192.0.2.10/32'],
            'http://user:password@192.0.2.10/24': ['192.0.2.10/32'],
            '//user:password@192.0.2.10/24': ['192.0.2.10/32'],
            '192.0.2.10/24/path': ['192.0.2.10/32'],
            '192.0.2.0/24': ['192.0.2.0/24'],
            'Retire 192.0.2.10.': ['192.0.2.10/32'],
            'Retire 192.0.2.10., then proceed.': ['192.0.2.10/32'],
            '192.0.2.10.10 192.0.2.10.example': [],
            '"192.0.2.10",192.0.2.11/32': ['192.0.2.10/32', '192.0.2.11/32'],
            'https://[2001:DB8:1234::1]:443/x': ['2001:db8:1234::1/128'],
            'Retire 2001:db8:1234::1.': ['2001:db8:1234::1/128'],
            'Retire 2001:db8:1234::/64.': ['2001:db8:1234::/64'],
            'Retire ::ffff:192.0.2.10.': [str(ipaddress.ip_network('::ffff:192.0.2.10/128')), '192.0.2.10/32'],
            '2001:0db8:1234:0000:0000:0000:0000:0001': ['2001:db8:1234::1/128'],
            '::ffff:192.0.2.10': [str(ipaddress.ip_network('::ffff:192.0.2.10/128')), '192.0.2.10/32'],
            '[fe80::1%eth0]': ['fe80::1/128'],
            'x192.0.2.10 192.0.2.1000 1192.0.2.10 192.0.2.10.example 192.0.2.10x': [],
            '999.0.2.10 2001:db8:::1 abc2001:db8::1suffix': [],
        }
        for text, expected in fixtures.items():
            with self.subTest(text=text):
                self.assertEqual([str(n) for n, _, _ in guard.literals(text)], expected)


class History(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.git('init', '-q')
        self.write('inventory.json', '{"ip":"192.0.2.10"}\n')
        self.write('old.ts', 'const old = "192.0.2.10:443"\n')
        self.base = self.commit()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], env={**os.environ,
            'GIT_AUTHOR_NAME': 'Test', 'GIT_AUTHOR_EMAIL': 'test@example.invalid',
            'GIT_COMMITTER_NAME': 'Test', 'GIT_COMMITTER_EMAIL': 'test@example.invalid'}).decode().strip()

    def write(self, path, text):
        p = self.root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def commit(self):
        self.git('add', '.')
        self.git('commit', '-qm', 'fixture')
        return self.git('rev-parse', 'HEAD')

    def policy(self):
        return {'version': 1, 'sourceHead': self.git('rev-parse', 'HEAD'),
                'sources': [{'path': 'inventory.json', 'revision': self.git('rev-parse', 'HEAD'),
                    'sha256': hashlib.sha256((self.root / 'inventory.json').read_bytes()).hexdigest()}],
                'networks': [{'cidr': '192.0.2.10/32'}, {'cidr': '2001:db8:1234::/64'}], 'permits': []}

    def check(self, policy=None, source_root=None):
        p = policy or self.policy()
        return guard.check(self.root, self.base, 'HEAD', p,
            [ipaddress.ip_network(n['cidr']) for n in p['networks']], source_root or self.root)

    def test_rejects_every_file_kind_and_new_context_without_count_allowance(self):
        for path in ('consumer.ts', 'docs/example.md', 'tests/test_fixture.py', '.github/workflows/check.yml'):
            self.write(path, 'target: https://192.0.2.10:443\n')
        self.write('old.ts', 'const renamed = "192.0.2.10:443"\n')
        self.commit()
        self.assertEqual(len(self.check()), 5)

    def test_unchanged_history_passes_but_move_and_duplicate_fail(self):
        self.write('unrelated.txt', 'no network change\n')
        self.commit()
        self.assertEqual(self.check(), [])
        (self.root / 'old.ts').rename(self.root / 'moved.ts')
        self.write('moved.ts', (self.root / 'moved.ts').read_text() * 2)
        self.commit()
        self.assertEqual(len(self.check()), 2)

    def test_authorizes_only_exact_scalar_span_not_neighbor_on_same_line(self):
        text = '{"ip":"192.0.2.10", "url":"192.0.2.10"}'
        self.write('inventory.json', text + '\n')
        self.commit()
        policy = self.policy()
        start = text.index('192.')
        policy['permits'] = [{'path': 'inventory.json', 'line': 1, 'text': text,
                             'start': start, 'end': start + len('192.0.2.10'), 'reason': 'node inventory'}]
        self.assertEqual(len(self.check(policy)), 1)
        self.assertEqual(len(self.check(policy, source_root=self.root / 'another-repository')), 2)

    def test_cidr_membership_mapped_addresses_and_distinct_ipv4_boundaries(self):
        self.write('new.txt', '\n'.join(['[2001:DB8:1234::beef]:443', '2001:db8:1234::/64',
            '::ffff:192.0.2.10', '192.0.2.0/24', '192.0.2.100', '2001:db8:1235::1', '0.0.0.0/0', '::/0']) + '\n')
        self.commit()
        self.assertEqual(len(self.check()), 3)

    def test_newline_and_unicode_paths_have_exact_line_positions(self):
        self.write('folder/\n日本語.ts', 'first\nx\u0085https://192.0.2.10:443\n')
        self.commit()
        self.assertEqual(self.check()[0][:2], ('folder/\n日本語.ts', 2))

    def test_every_filename_is_literal_not_a_git_pathspec(self):
        paths = (':foo', ':(literal)README', ':(exclude)nothing',
                 ':(glob)**', 'literal[1].md', 'folder/\n日本語.ts')
        for path in paths:
            self.write(path, 'target: https://192.0.2.10:443\n')
        self.commit()
        self.assertEqual({hit[0] for hit in self.check()}, set(paths))
        # Changing one magic-looking filename must not scan a different file.
        self.base = self.git('rev-parse', 'HEAD')
        self.write(':foo', 'changed: https://192.0.2.10:443\n')
        self.commit()
        self.assertEqual([hit[0] for hit in self.check()], [':foo'])

    def test_stale_and_forged_inventory_fail_closed(self):
        policy = self.policy()
        policy_path = self.root / 'policy-output.json'
        policy_path.write_text(json.dumps(policy))
        guard.load_policy(policy_path, self.root)
        self.write('unrelated.txt', 'head moved\n')
        self.commit()
        with self.assertRaisesRegex(ValueError, 'stale'):
            guard.load_policy(policy_path, self.root)
        policy = self.policy()
        policy['sources'][0]['sha256'] = '0' * 64
        policy_path.write_text(json.dumps(policy))
        with self.assertRaisesRegex(ValueError, 'digest mismatch'):
            guard.load_policy(policy_path, self.root)

    def test_cli_rejects_empty_or_missing_base_instead_of_skipping(self):
        path = self.root / 'policy-output.json'
        path.write_text(json.dumps(self.policy()))
        result = subprocess.run(['python3', str(SCRIPT), '--root', str(self.root),
            '--inventory-root', str(self.root), '--inventory', str(path), '--base', ''], capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn(b'base revision is required', result.stderr)


if __name__ == '__main__':
    unittest.main()
