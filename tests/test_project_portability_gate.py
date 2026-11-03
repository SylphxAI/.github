"""The delivered CLI refuses new debt and keeps inherited baselines shrinking."""
import pathlib
import tempfile
import unittest

from test_rule_adoption import Fixture, OLD, UNKNOWN, WORKFLOW, ROOT, sc, workflow

SOURCE = 'src/client.ts'
BAD = 'export const origin = "https://api.tenant.svc.cluster.local";\n'
SAFE = 'export const origin = "https://api.example.invalid";\n'
RULE = 'architecture/C1/project-portability'


class ProjectPortabilityGateTests(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Fixture(temporary.name)

    def test_live_cli_positive_clean_and_allowlisted_manifest_fixtures(self):
        f = self.fixture()
        base = f.commit({WORKFLOW: workflow(OLD), SOURCE: SAFE})
        f.commit({SOURCE: BAD})
        output, report = f.check(base, 1)
        self.assertIn('project-portability', output)
        self.assertEqual([r['disposition'] for r in report['findings'] if r['rule'] == 'project-portability'], ['regression'])
        f.commit({SOURCE: SAFE, 'deploy/service.yaml': 'origin: "https://api.tenant.svc.cluster.local"\n'})
        f.check(base, 0)

    def test_inherited_adoption_only_shrinks_and_copying_adds_debt(self):
        f = self.fixture()
        base = f.commit({WORKFLOW: workflow(OLD), SOURCE: BAD})
        f.commit({'README.md': 'unrelated change\n'})
        self.assertIn('project-portability', f.check(base, 1)[0])
        result = f.plan(base)
        self.assertIn(RULE, result['introduced_rules'])
        baseline = result['baseline_text']
        adopted = f.commit({WORKFLOW: workflow(UNKNOWN), sc.BASELINE: baseline})
        f.check(base, 0)
        f.commit({SOURCE: '// context moves the finding\n' + BAD})
        f.check(adopted, 0)
        f.commit({SOURCE: BAD + BAD})
        self.assertIn('project-portability', f.check(adopted, 1)[0])
        f.commit({SOURCE: SAFE})
        self.assertIn('stale architecture baseline', f.check(adopted, 1)[0])
        smaller = '\n'.join(line for line in baseline.splitlines() if not line.startswith('architecture C1 project-portability ')) + '\n'
        clean = f.commit({sc.BASELINE: smaller})
        f.check(adopted, 0)
        f.commit({SOURCE: BAD, sc.BASELINE: baseline})
        self.assertIn('cannot grow', f.check(clean, 1)[0])

    def test_new_debt_cannot_be_seeded_during_adoption(self):
        f = self.fixture()
        base = f.commit({WORKFLOW: workflow(OLD), SOURCE: SAFE})
        f.commit({SOURCE: BAD})
        with self.assertRaisesRegex(ValueError, 'new head debt'):
            f.plan(base)

    def test_selector_release_adopts_only_the_new_portability_rule(self):
        f = self.fixture()
        old = '054710a09ce85a2e0edbc14599bd00df0db879be'
        base = f.commit({WORKFLOW: workflow(old), SOURCE: BAD})
        f.commit({WORKFLOW: workflow(UNKNOWN)})
        result = f.plan(base)
        self.assertEqual(result['introduced_rules'], [RULE, 'legacy/solar2d'])
        self.assertEqual(len(result['evidence']['added_records']), 1)
        f.commit({sc.BASELINE: result['baseline_text']})
        f.check(base, 0)

    def rust_fixture(self, count=1):
        path = 'src/client.rs'
        destinations = ['"http://api.tenant.svc.cluster.local"', '"http://gate:24001"'][:count]
        source = ("fn client<'a>(v: &'a str) -> &'a str {\n"
                  " let sql = r#\"SELECT 'canonical'\"#;\n" +
                  ''.join(' let endpoint = ' + value + ';\n' for value in destinations) +
                  ' v\n}\n' + "fn next<'b>() {}\n")
        a = sc.architecture
        p = a.project_portability
        old = p.scan(path, source, lambda t, s: a.source_views(t, s, legacy_rust=True), legacy_rust=True)
        rows = a.with_occurrences([a.finding(path, source, start, 'C1', 'project-portability', why, end)
                                   for start, end, why in old])
        self.assertEqual(len(rows), 1)
        baseline = (a.BASELINE_MARKER + '\n' + sc.adoption.RULE_MARKER + RULE + '\n' +
                    '\n'.join(a.baseline_key(row) for row in rows) + '\n')
        f = self.fixture()
        base = f.commit({WORKFLOW: workflow('6ec33ba0786f402e1c6857eea6f6e3e7c8ed967b'),
                         path: source, sc.BASELINE: baseline})
        return f, base, path, source, baseline

    def test_explicit_literal_migration_corrects_cardinality_not_new_debt(self):
        for count in [1, 2]:
            with self.subTest(constituents=count):
                f, base, path, source, original = self.rust_fixture(count)
                f.commit({path: source.replace("'canonical'", "'stopped'")})
                # No implicit alias: malformed old records must be retired.
                self.assertIn('stale architecture baseline', f.check(base, 1)[0])
                result = f.plan(base)
                self.assertNotIn(RULE, result['introduced_rules'])
                self.assertEqual(result['evidence']['rekeyed_records'], 1)
                self.assertEqual(result['evidence']['corrected_constituents'], count)
                old, = [line for line in original.splitlines() if line.startswith('architecture ')]
                self.assertNotIn(old, result['baseline_text'])
                self.assertEqual(len(result['evidence']['portability_rekeys'][0]['corrected_records']), count)
                corrected = f.commit({sc.BASELINE: result['baseline_text']})
                f.check(base, 0)
                f.check('', 0)
                f.commit({path: source.replace("'canonical'", "'pending'")})
                f.check(corrected, 0)
                # A corrected record is ordinary inherited debt forever after.
                f.commit({path: source.replace('http://api.', 'http://new.')})
                self.assertIn('project-portability', f.check(corrected, 1)[0])

    def test_migration_refuses_changed_copied_and_unrecorded_constituents(self):
        changes = [
            lambda s: s.replace('http://api.', 'http://new.'),
            lambda s: s + 'const EXTRA: &str = "http://api.tenant.svc.cluster.local";\n',
            lambda s: s + 'const EXTRA: &str = "/var/run/secrets/kubernetes.io/serviceaccount/token";\n',
            lambda s: s + 'fn extra() { Config::incluster_env(); }\n',
        ]
        for change in changes:
            with self.subTest(change=change):
                f, base, path, source, _ = self.rust_fixture(2)
                f.commit({path: change(source)})
                with self.assertRaisesRegex(ValueError, 'new head debt cannot be migrated'):
                    f.plan(base)
                self.assertIn('project-portability', f.check(base, 1)[0])

    def test_migration_cannot_keep_old_seed_arbitrary_or_resurrect(self):
        f, base, path, source, original = self.rust_fixture(2)
        result = f.plan(base)
        corrected = result['baseline_text']
        old, = [line for line in original.splitlines() if line.startswith('architecture ')]
        f.commit({sc.BASELINE: corrected + old + '\n'})
        self.assertIn('cannot grow', f.check(base, 1)[0])
        forged = corrected.replace('architecture C1 project-portability src/client.rs ',
                                    'architecture C1 project-portability src/other.rs ')
        f.commit({sc.BASELINE: forged})
        self.assertIn('cannot grow', f.check(base, 1)[0])
        migrated = f.commit({sc.BASELINE: corrected})
        smaller = '\n'.join(line for line in corrected.splitlines() if not line.startswith('architecture ')) + '\n'
        safe_source = source.replace('http://api.tenant.svc.cluster.local', 'https://api.example.invalid').replace('http://gate:24001', 'https://gate.example.invalid')
        clean = f.commit({path: safe_source, sc.BASELINE: smaller})
        f.check(migrated, 0)
        f.commit({path: source, sc.BASELINE: corrected})
        self.assertIn('cannot grow', f.check(clean, 1)[0])


    def test_migration_reassigns_occurrences_from_exact_old_spans(self):
        path = 'src/client.rs'
        source = ("fn first<'a>() {\n let url = \"http://gate:24001\";\n}\n"
                  "fn next<'b>() {}\nconst SECOND: &str = \"http://gate:24001\";\n")
        a = sc.architecture
        old = a.project_portability.scan(path, source,
            lambda t, s: a.source_views(t, s, legacy_rust=True), legacy_rust=True)
        rows = a.with_occurrences([a.finding(path, source, s, 'C1', 'project-portability', why, e)
                                   for s, e, why in old])
        self.assertEqual(len(rows), 2)
        baseline = (a.BASELINE_MARKER + '\n' + sc.adoption.RULE_MARKER + RULE + '\n' +
                    '\n'.join(a.baseline_key(row) for row in rows) + '\n')
        f = self.fixture()
        base = f.commit({WORKFLOW: workflow('6ec33ba0786f402e1c6857eea6f6e3e7c8ed967b'),
                         path: source, sc.BASELINE: baseline})
        result = f.plan(base)
        self.assertEqual(result['evidence']['rekeyed_records'], 2)
        self.assertEqual(result['evidence']['corrected_constituents'], 2)
        self.assertEqual(len([line for line in result['baseline_text'].splitlines()
                              if line.startswith('architecture ')]), 2)
        f.commit({sc.BASELINE: result['baseline_text']})
        f.check(base, 0)
        f.commit({path: source + 'const THIRD: &str = "http://gate:24001";\n'})
        self.assertIn('project-portability', f.check(base, 1)[0])

    def test_migration_deduplicates_independently_recorded_call_in_broad_span(self):
        f, _, path, source, _ = self.rust_fixture()
        source = source.replace(' v\n', ' Config::incluster_env();\n v\n')
        a = sc.architecture
        old = a.project_portability.scan(path, source,
            lambda t, s: a.source_views(t, s, legacy_rust=True), legacy_rust=True)
        rows = a.with_occurrences([a.finding(path, source, start, 'C1', 'project-portability', why, end)
                                   for start, end, why in old])
        self.assertEqual(len(rows), 2)
        baseline = (a.BASELINE_MARKER + '\n' + sc.adoption.RULE_MARKER + RULE + '\n' +
                    '\n'.join(a.baseline_key(row) for row in rows) + '\n')
        base = f.commit({path: source, sc.BASELINE: baseline})
        result = f.plan(base)
        records = [line for line in result['baseline_text'].splitlines() if line.startswith('architecture ')]
        self.assertEqual(len(records), 2)
        self.assertEqual(len(set(records)), 2)
        f.commit({sc.BASELINE: result['baseline_text']})
        f.check(base, 0)

    def test_migration_normalizes_old_record_spacing_and_retains_annotations(self):
        for separator in [' ', '  ', '\t']:
            with self.subTest(separator=repr(separator)):
                f, _, path, source, original = self.rust_fixture(2)
                old, = [line for line in original.splitlines() if line.startswith('architecture ')]
                annotated = separator.join(old.split()) + ' # inherited endpoint'
                original = original.replace(old, annotated)
                base = f.commit({sc.BASELINE: original})
                result = f.plan(base)
                self.assertEqual(result['evidence']['rekeyed_records'], 1)
                self.assertNotIn(annotated, result['baseline_text'])
                self.assertIn('# inherited endpoint', result['baseline_text'])
                self.assertEqual(len(sc.parse_baseline(result['baseline_text'])), 2)
                f.commit({sc.BASELINE: result['baseline_text']})
                f.check(base, 0)

    def test_raw_byte_and_c_token_envelopes_migrate_exact_old_quoted_body(self):
        for prefix in ['r', 'br', 'cr']:
            for body in ['http://gate:24001', 'SQL "quoted" http://gate:24001',
                         'http://gate:24001 "quoted" https://api.tenant.svc.cluster.local']:
                with self.subTest(prefix=prefix, body=body):
                    path = 'src/client.rs'
                    source = 'const URL: &str = ' + prefix + '##"' + body + '"##;\n'
                    a = sc.architecture
                    old = a.project_portability.scan(path, source,
                        lambda t, s: a.source_views(t, s, legacy_rust=True), legacy_rust=True)
                    rows = a.with_occurrences([a.finding(path, source, start, 'C1', 'project-portability', why, end)
                                               for start, end, why in old])
                    self.assertEqual(len(rows), 1 if prefix == 'r' or 'svc' not in body else 2)
                    baseline = (a.BASELINE_MARKER + '\n' + sc.adoption.RULE_MARKER + RULE + '\n' +
                                '\n'.join(a.baseline_key(row) for row in rows) + '\n')
                    f = self.fixture()
                    base = f.commit({WORKFLOW: workflow('6ec33ba0786f402e1c6857eea6f6e3e7c8ed967b'),
                                     path: source, sc.BASELINE: baseline})
                    result = f.plan(base)
                    self.assertEqual(result['evidence']['rekeyed_records'], 0 if prefix == 'r' else len(rows))
                    migrated = f.commit({sc.BASELINE: result['baseline_text']})
                    f.check(base, 0)
                    f.commit({path: source.replace('http://gate:', 'http://new:')})
                    self.assertIn('project-portability', f.check(migrated, 1)[0])

    def test_migration_refuses_preexisting_but_unrecorded_literal(self):
        f, _, path, source, original = self.rust_fixture()
        # This private destination never belonged to the recorded old span.
        source += 'const UNRECORDED: &str = "http://elsewhere:24001";\n'
        base = f.commit({path: source})
        with self.assertRaisesRegex(ValueError, 'unrecorded portability debt cannot be migrated'):
            f.plan(base)
        self.assertIn('project-portability', f.check(base, 1)[0])
        self.assertEqual((f.root / sc.BASELINE).read_text(), original)


    def test_capability_profile_binds_the_executable_module(self):
        self.assertIn(RULE, sc.adoption.current_rules())
        action = pathlib.Path(sc.__file__).parent
        files = {sc.adoption.PREFIX + p.name: p.read_text() for p in action.iterdir() if p.suffix in {'.py', '.json'}}
        files[sc.adoption.PREFIX + 'action.yml'] = (action / 'action.yml').read_text()
        files['policy/deal-randomness-scope.json'] = (ROOT / 'policy/deal-randomness-scope.json').read_text()
        for dependency in ("scripts/source_provenance.py", "policy/source-relocations.json"):
            files[dependency] = (ROOT / dependency).read_text()
        profile = sc.adoption.source_profile(sc.DictTree(files))
        self.assertEqual(sc.adoption.load_registry()['profiles'][sc.adoption.profile_id(profile)], profile)
        self.assertIn(RULE, profile['rules'])
        self.assertIn(sc.adoption.PREFIX + 'project_portability.py', profile['sources'])
        self.assertIn(sc.adoption.PREFIX + 'rust_reachability.py', profile['sources'])
        rust_path = sc.adoption.PREFIX + 'rust_reachability.py'
        rust_source = files.pop(rust_path)
        with self.assertRaisesRegex(ValueError, 'Rust reachability checker source missing'):
            sc.adoption.source_profile(sc.DictTree(files))
        files[rust_path] = rust_source
        del files[sc.adoption.PREFIX + 'project_portability.py']
        with self.assertRaisesRegex(ValueError, 'project portability checker source missing'):
            sc.adoption.source_profile(sc.DictTree(files))


if __name__ == '__main__':
    unittest.main()
