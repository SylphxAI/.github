"""Exact-history relocation controls shared by stack and SQL guards."""
import contextlib
import datetime
import io
import importlib.util
import json
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

sc = load('sc_relocation_tests', '.github/actions/stack-conformance/stack_conformance.py')
mg = load('mg_relocation_tests', '.github/actions/migration-guard/migration_guard.py')
p = sc.source_provenance
TODAY = datetime.date(2026, 11, 1)


class RelocationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='relocation-test-')
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name) / 'repo'
        self.root.mkdir()
        self.policy = self.root.parent / 'trusted-policy.json'
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')
        self.write('README.md', 'canonical root\n')
        self.commit()
        self.base = self.git('rev-parse', 'HEAD')
        self.write('legacy/hk/server/package.json', '{"dependencies":{"hono":"4"}}\n')
        self.write('legacy/hk/src/config.py', 'import os\nvalue = os.getenv("HISTORICAL_UNKNOWN")\n')
        self.write('legacy/hk/.github/stack-departures.txt', 'ts-server server/package.json hono\n# architecture-ratchet-v2\n')
        self.write('legacy/hk/db/0014_historical.sql', 'DROP TABLE historical_unused;\n')
        self.commit()
        self.anchor = self.git('rev-parse', 'HEAD')
        self.record = {'repository': 'Cubeage/combined', 'prefix': 'legacy/hk',
            'source_repository': 'Cubeage/original', 'source_commit': 'a' * 40,
            'source_tree': self.git('rev-parse', 'HEAD:legacy/hk'),
            'evidence': 'https://github.com/Cubeage/original/tree/' + 'a' * 40}
        self.save_policy([self.record])

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], text=True, stderr=subprocess.PIPE).strip()

    def write(self, path, content):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def commit(self):
        self.git('add', '.')
        self.git('commit', '-qm', 'fixture')

    def save_policy(self, rows):
        self.policy.write_text(json.dumps({'version': 1, 'relocations': rows}))

    def records(self, base=None):
        return p.roots(self.root, base or self.base, 'HEAD', 'Cubeage/combined', self.policy)

    def scoped_report(self, base=None):
        before = sc.Tree(str(self.root), base or self.base)
        head = sc.Tree(str(self.root), 'HEAD')
        records = self.records(base)
        report, errors = p.architecture_report(head, before, records, sc.BASELINE,
            sc.added_lines(str(self.root), base or self.base, 'HEAD'), TODAY, sc.architecture, sc.parse_baseline)
        errors += p.legacy_errors(head, before, records, sc.BASELINE, sc.departures, sc.parse_baseline, sc.decide)
        return report, errors

    def test_exact_origin_moves_as_visible_inherited_source_not_new_sql(self):
        self.assertEqual('exact-origin-tree', self.records()[0]['proof'])
        report, errors = self.scoped_report()
        self.assertEqual([], errors)
        self.assertTrue(report['findings'])
        self.assertTrue(all(not row['changed'] for row in report['findings']))
        self.assertTrue(all(row['path'].startswith('legacy/hk/') for row in report['findings']))
        self.assertEqual(([], 0), mg.check_range(self.root, self.base, 'HEAD', ['legacy/hk/db'],
            'postgres', repository='Cubeage/combined', provenance_policy=self.policy))

    def test_unregistered_or_wrong_destination_gets_no_inheritance(self):
        for row in [{**self.record, 'repository': 'Cubeage/other'}, {**self.record, 'prefix': 'elsewhere'}]:
            self.save_policy([row])
            self.assertEqual([], self.records())
            errors, count = mg.check_range(self.root, self.base, 'HEAD', ['legacy/hk/db'],
                'postgres', repository='Cubeage/combined', provenance_policy=self.policy)
            self.assertEqual(1, count)
            self.assertIn('DROP TABLE', '\n'.join(errors))

    def test_modified_initial_import_and_mode_change_refused(self):
        self.write('legacy/hk/extra.txt', 'not in original\n')
        self.commit()
        with self.assertRaisesRegex(ValueError, 'differs from recorded origin tree'):
            self.records()
        self.git('reset', '--hard', self.anchor)
        self.git('update-index', '--chmod=+x', 'legacy/hk/db/0014_historical.sql')
        self.git('commit', '-qm', 'mode change')
        with self.assertRaisesRegex(ValueError, 'differs from recorded origin tree'):
            self.records()

    def test_new_architecture_debt_and_baseline_growth_still_fail(self):
        self.write('legacy/hk/src/new.py', 'import os\nvalue = os.getenv("NEW_UNKNOWN")\n')
        self.write('legacy/hk/other/package.json', '{"dependencies":{"express":"5"}}\n')
        baseline = self.root / 'legacy/hk/.github/stack-departures.txt'
        baseline.write_text(baseline.read_text() + 'ts-server other/package.json express\n')
        self.commit()
        report, errors = self.scoped_report(self.anchor)
        self.assertTrue(any(row['changed'] and row['path'].endswith('new.py') for row in report['findings']))
        self.assertIn('baseline grew', '\n'.join(errors))
        self.assertTrue(all(not row['changed'] for row in report['findings'] if row['path'].endswith('config.py')))

    def test_changed_or_deleted_imported_sql_remains_immutable(self):
        for deletion in (False, True):
            self.git('reset', '--hard', self.anchor)
            if deletion:
                self.git('rm', 'legacy/hk/db/0014_historical.sql')
            else:
                self.write('legacy/hk/db/0014_historical.sql', 'SELECT 1;\n')
            self.commit()
            errors, _ = mg.check_range(self.root, self.anchor, 'HEAD', ['legacy/hk/db'],
                'postgres', repository='Cubeage/combined', provenance_policy=self.policy)
            self.assertIn('merged migration was modified, deleted or renamed', '\n'.join(errors))

    def test_new_destructive_sql_still_refused(self):
        self.write('legacy/hk/db/0015_new.sql', 'DROP TABLE live_accounts;\n')
        self.commit()
        errors, count = mg.check_range(self.root, self.anchor, 'HEAD', ['legacy/hk/db'],
            'postgres', repository='Cubeage/combined', provenance_policy=self.policy)
        self.assertEqual(1, count)
        self.assertIn('DROP TABLE', '\n'.join(errors))

    def test_scoped_env_declarations_do_not_leak_between_roots(self):
        self.write('legacy/hk/.env.example', 'ONLY_HK=\n')
        self.write('legacy/tw/src/config.py', 'import os\nvalue = os.getenv("ONLY_HK")\n')
        self.commit()
        head = sc.Tree(str(self.root), 'HEAD')
        hk = p.TreeView(head, 'legacy/hk')
        tw = p.TreeView(head, 'legacy/tw')
        self.assertIn('ONLY_HK', sc.architecture.env_declarations(hk))
        self.assertNotIn('ONLY_HK', sc.architecture.env_declarations(tw))
        self.assertNotIn('ONLY_HK', sc.architecture.env_declarations(p.outer(head, [{'prefix': 'legacy/hk'}])))

    def public_check(self, base):
        report = self.root.parent / 'public-report.json'
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = sc.main(['check', '--repo', str(self.root), '--base', base,
                '--head', 'HEAD', '--repository', 'Cubeage/combined',
                '--source-provenance-policy', str(self.policy), '--shared-source', str(ROOT),
                '--report', str(report), '--today', TODAY.isoformat()])
        return result, json.loads(report.read_text())

    def test_public_cli_preserves_visible_findings_and_refuses_new_root_debt(self):
        result, report = self.public_check(self.base)
        self.assertEqual(0, result, report.get('legacy_errors'))
        self.assertTrue(report['source_relocations'])
        self.assertTrue(all(row['disposition'] == 'inherited' for row in report['findings']))
        self.write('src/new.py', 'import os\nvalue = os.getenv("CANONICAL_NEW_UNKNOWN")\n')
        self.commit()
        result, report = self.public_check(self.anchor)
        self.assertEqual(1, result)
        self.assertTrue(any(row['path'] == 'src/new.py' and row['disposition'] == 'regression' for row in report['findings']))

    def test_public_cli_does_not_waive_agent_runtime_ownership(self):
        self.write('legacy/hk/crates/vault-proxy/Cargo.toml', '[package]\nname = "vault-proxy"\nversion = "0.1.0"\n')
        self.commit()
        # Even an independently registered exact historical tree cannot waive
        # the full-tree agent runtime boundary enforced outside scoped reports.
        self.record['source_tree'] = self.git('rev-parse', 'HEAD:legacy/hk')
        self.save_policy([self.record])
        result, report = self.public_check(self.base)
        self.assertEqual(1, result)
        self.assertTrue(any('agent-runtime' in line for line in report['legacy_errors']))

    def test_capability_provenance_includes_shared_helper_and_registry(self):
        tree = sc.Tree(str(ROOT), 'HEAD')
        # The test edits only the in-memory source view of an immutable tree.
        class CurrentSources:
            def paths(self):
                return tree.paths() + ['scripts/source_provenance.py', 'policy/source-relocations.json']
            def read(self, path):
                if path in ('.github/actions/stack-conformance/stack_conformance.py',
                            'scripts/source_provenance.py', 'policy/source-relocations.json',
                            '.github/actions/stack-conformance/staged_retirement.py'):
                    return (ROOT / path).read_text()
                return tree.read(path)
        profile = sc.adoption.source_profile(CurrentSources())
        self.assertIn('scripts/source_provenance.py', profile['sources'])
        self.assertIn('policy/source-relocations.json', profile['sources'])

    def test_overlapping_and_traversal_policy_fail_closed(self):
        self.save_policy([self.record, {**self.record, 'prefix': 'legacy/hk/nested'}])
        with self.assertRaisesRegex(ValueError, 'overlap'):
            self.records()
        self.save_policy([{**self.record, 'prefix': '../escape'}])
        with self.assertRaisesRegex(ValueError, 'invalid source-relocation prefix'):
            self.records()


if __name__ == '__main__':
    unittest.main()
