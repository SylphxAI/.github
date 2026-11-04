"""One-time adoption of Rust literals wholly unseen by the frozen recognizer."""
import hashlib
import json
import subprocess
import tempfile
import unittest

from test_rule_adoption import Fixture, ROOT, WORKFLOW, sc, workflow

A = sc.adoption
ARCH = sc.architecture
RULE = A.PORTABILITY_RULE
COVERAGE = A.RUST_LITERAL_COVERAGE
LEGACY = '6ec33ba0786f402e1c6857eea6f6e3e7c8ed967b'
LEXICAL = 'd68026446d969c04a414a2abd40906db4aa8d929'
PATH = 'src/client.rs'
# The old apostrophe regex consumes the lifetime through the SQL quote, then
# consumes the remaining double quote through the destination's opening quote.
UNSEEN = ("fn client<'a>() {\n let sql = r#\"SELECT 'canonical'\"#;\n"
          ' let url = "http://gate:24001";\n}\n')
SEEN = 'const URL: &str = "http://other:24001";\n'
BASELINE = ARCH.BASELINE_MARKER + '\n' + A.RULE_MARKER + RULE + '\n'


class RustPortabilityAdoptionTests(unittest.TestCase):
    def fixture(self, source=UNSEEN, pin=LEGACY, baseline=BASELINE):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        f = Fixture(temporary.name)
        base = f.commit({WORKFLOW: workflow(pin), PATH: source, sc.BASELINE: baseline})
        return f, base

    def test_cli_one_time_adoption_receipt_and_independent_required_check(self):
        f, base = self.fixture()
        rows = ARCH.scan_file(PATH, UNSEEN, 'example/app', set(), [])
        self.assertEqual(len(rows), 1)
        self.assertNotIn('legacy_portability_keys', rows[0])
        self.assertIn('project-portability', f.check(base, 1)[0])
        f.write({WORKFLOW: workflow(LEXICAL)})
        f.git('add', '.')
        candidate = f.git('write-tree')
        evidence = f.root.parent / (f.root.name + '-evidence.json')
        result = subprocess.run(['python3', str(A.HERE / 'adoption.py'), 'adopt',
            '--repo', str(f.root), '--base', base, '--head', candidate,
            '--shared-repo', str(ROOT), '--repository', 'example/app',
            '--output', str(f.root / sc.BASELINE), '--evidence', str(evidence)],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        receipt = json.loads(evidence.read_text())
        self.assertEqual(receipt['introduced_rules'], [COVERAGE])
        self.assertEqual(receipt['added_records'], [ARCH.baseline_key(rows[0])])
        baseline = (f.root / sc.BASELINE).read_text()
        self.assertTrue(baseline.startswith(BASELINE))
        self.assertIn(A.RULE_MARKER + COVERAGE + '\n', baseline)
        adopted = f.commit({})
        f.check(base, 0)
        f.check('', 0)
        # Another checker bump cannot reset the permission, even if its pin is
        # still the old regex checker rather than the already-aware lexer.
        f.commit({WORKFLOW: workflow(LEGACY), PATH: UNSEEN + UNSEEN})
        result = f.plan(adopted)
        self.assertNotIn(COVERAGE, result['introduced_rules'])
        duplicate = ARCH.scan_file(PATH, UNSEEN + UNSEEN, 'example/app', set(), [])[-1]
        f.commit({sc.BASELINE: baseline + ARCH.baseline_key(duplicate) + '\n'})
        self.assertIn('cannot grow', f.check(adopted, 1)[0])

    def test_new_changed_copied_and_moved_findings_refuse_without_output(self):
        for change in ('new', 'changed', 'copied', 'moved'):
            with self.subTest(change=change):
                f, base = self.fixture(source='' if change == 'new' else UNSEEN)
                source = UNSEEN if change == 'new' else (
                    UNSEEN.replace('gate:', 'new:') if change == 'changed' else UNSEEN + UNSEEN)
                files = {PATH: source}
                if change == 'moved':
                    files = {PATH: '', 'src/other.rs': UNSEEN}
                f.commit(files)
                with self.assertRaisesRegex(ValueError, 'new head debt'):
                    f.plan(base)
                self.assertEqual((f.root / sc.BASELINE).read_text(), BASELINE)
                # Forging exactly the current head keys does not fool the
                # required check, which independently scans the actual base.
                rows = []
                for path, text in files.items():
                    rows.extend(ARCH.scan_file(path, text, 'example/app', set(), []))
                forged = BASELINE + A.RULE_MARKER + COVERAGE + '\n' + ''.join(
                    ARCH.baseline_key(row) + '\n' for row in rows if row['rule'] == 'project-portability')
                f.commit({sc.BASELINE: forged})
                self.assertIn('cannot grow', f.check(base, 1)[0])

    def test_previously_seen_unrecorded_debt_and_other_languages_cannot_be_adopted(self):
        for path, text in ((PATH, SEEN), ('src/client.ts', 'const URL = "http://other:24001";\n')):
            with self.subTest(path=path):
                f, base = self.fixture(source='')
                base = f.commit({path: text})
                with self.assertRaisesRegex(ValueError, 'unrecorded portability debt'):
                    f.plan(base)
                rows = ARCH.scan_file(path, text, 'example/app', set(), [])
                forged = BASELINE + A.RULE_MARKER + COVERAGE + '\n' + ''.join(
                    ARCH.baseline_key(row) + '\n' for row in rows)
                f.commit({sc.BASELINE: forged})
                self.assertIn('cannot grow', f.check(base, 1)[0])

    def test_already_lexical_and_mixed_pins_cannot_reopen_coverage(self):
        for mixed in (False, True):
            with self.subTest(mixed=mixed):
                f, base = self.fixture(pin=LEXICAL)
                if mixed:
                    base = f.commit({'.github/workflows/old.yml': workflow(LEGACY)})
                result = f.plan(base)
                self.assertNotIn(COVERAGE, result['introduced_rules'])
                row, = ARCH.scan_file(PATH, UNSEEN, 'example/app', set(), [])
                f.commit({sc.BASELINE: BASELINE + A.RULE_MARKER + COVERAGE + '\n' +
                          ARCH.baseline_key(row) + '\n'})
                output, _ = f.check(base, 1)
                self.assertIn('already enforced', output)
                self.assertIn('cannot grow', output)

    def test_context_change_cannot_turn_ineligible_head_into_coverage(self):
        f, base = self.fixture()
        source = 'fn client() { let url = "http://gate:24001"; }\n'
        before, = ARCH.scan_file(PATH, UNSEEN, 'example/app', set(), [])
        after, = ARCH.scan_file(PATH, source, 'example/app', set(), [])
        self.assertEqual(ARCH.baseline_key(before), ARCH.baseline_key(after))
        self.assertTrue(ARCH.adoption_eligible(before, {COVERAGE}))
        self.assertFalse(ARCH.adoption_eligible(after, {COVERAGE}))
        f.commit({PATH: source})
        with self.assertRaisesRegex(ValueError, 'unrecorded portability debt'):
            f.plan(base)

    def test_equal_literal_ordinal_shift_cannot_relabel_previously_seen_debt(self):
        source = ("fn first<'a>() { let x = std::env::var(\"KUBERNETES_SERVICE_HOST\"); }\n"
                  "fn next<'b>() {}\n"
                  'fn standalone() { let x = std::env::var("KUBERNETES_SERVICE_HOST"); }\n')
        f, base = self.fixture(source=source)
        rows = [row for row in ARCH.scan_file(PATH, source, 'example/app', set(), [])
                if row['rule'] == 'project-portability']
        self.assertEqual(len(rows), 2)
        self.assertFalse(rows[0].get('legacy_portability_keys'))
        self.assertTrue(rows[1].get('legacy_portability_keys'))
        self.assertTrue(rows[0]['legacy_portability_seen_span'])
        shifted = source.replace('let x = std::env::var("KUBERNETES_SERVICE_HOST"); ', '', 1)
        after = [row for row in ARCH.scan_file(PATH, shifted, 'example/app', set(), [])
                 if row['rule'] == 'project-portability']
        forged = BASELINE + A.RULE_MARKER + COVERAGE + '\n' + ARCH.baseline_key(after[0]) + '\n'
        f.commit({PATH: shifted, sc.BASELINE: forged})
        self.assertIn('cannot grow', f.check(base, 1)[0])
        direct = ARCH.baseline_errors(after, {ARCH.baseline_key(after[0])}, set(), rows, {COVERAGE})
        self.assertTrue(any('cannot grow' in error for error in direct))

    def test_rekey_cannot_retain_colliding_old_key_after_recorded_member_disappears(self):
        source = ("fn first<'a>() { let x = std::env::var(\"KUBERNETES_SERVICE_HOST\"); }\n"
                  "fn next<'b>() {}\n"
                  'fn standalone() { let x = std::env::var("KUBERNETES_SERVICE_HOST"); }\n')
        rows = [row for row in ARCH.scan_file(PATH, source, 'example/app', set(), [])
                if row['rule'] == 'project-portability']
        old, = rows[1]['legacy_portability_keys']
        baseline = BASELINE + old + '\n'
        f, base = self.fixture(source=source, baseline=baseline)
        shifted = source.replace('fn standalone() { let x = std::env::var("KUBERNETES_SERVICE_HOST"); }',
                                 'fn standalone() {}')
        f.commit({PATH: shifted})
        with self.assertRaisesRegex(ValueError, 'changed or removed portability constituent'):
            f.plan(base)
        self.assertIn('cannot grow', f.check(base, 1)[0])

    def test_already_tokenized_literal_is_not_new_lexical_coverage(self):
        # A diagnostic-context refinement may start reporting an existing
        # literal, but this lexical capability cannot adopt that new category.
        source = 'fn run() { println!("http://gate:24001"); }\n'
        rows = ARCH.scan_file(PATH, source, 'example/app', set(), [])
        self.assertEqual(rows, [])
        # Exercise the predicate with exactly the metadata emitted for a
        # newly reported but already tokenized literal after context changes.
        start = source.index('"')
        row = ARCH.finding(PATH, source, start, 'C1', 'project-portability', 'context changed',
                           source.index('"', start + 1) + 1)
        row, = ARCH.with_occurrences([row])
        row['legacy_portability_literal_seen'] = True
        self.assertFalse(ARCH.adoption_eligible(row, {COVERAGE}))
        errors = ARCH.baseline_errors([row], {ARCH.baseline_key(row)}, set(), [row], {COVERAGE})
        self.assertTrue(any('cannot grow' in error for error in errors))

    def test_provenance_is_hash_bound_and_unknown_source_is_not_new_coverage(self):
        tree = sc.Tree(str(ROOT), LEGACY)
        profile, = A.old_checkers(sc.DictTree({WORKFLOW: workflow(LEGACY)}), shared_repo=str(ROOT))
        self.assertFalse(A.rust_literal_coverage_enforced(profile, tree_cache={LEGACY: tree}))
        source = tree.read(A.PREFIX + 'project_portability.py')
        changed = sc.DictTree({A.PREFIX + 'project_portability.py': source + '\n# changed\n'})
        with self.assertRaisesRegex(ValueError, 'provenance mismatch'):
            A.rust_literal_coverage_enforced(profile, tree_cache={LEGACY: changed})
        unknown = {**profile, 'sources': {**profile['sources'], A.PREFIX + 'project_portability.py':
                   hashlib.sha256((source + '\n# changed\n').encode()).hexdigest()}}
        self.assertTrue(A.rust_literal_coverage_enforced(unknown, tree_cache={LEGACY: changed}))
        missing = sc.DictTree({})
        with self.assertRaisesRegex(ValueError, 'provenance mismatch'):
            A.rust_literal_coverage_enforced(profile, tree_cache={LEGACY: missing})

    def test_coverage_record_growth_requires_the_explicit_one_time_marker(self):
        f, base = self.fixture()
        result = f.plan(base)
        forged = result['baseline_text'].replace(A.RULE_MARKER + COVERAGE + '\n', '')
        f.commit({sc.BASELINE: forged})
        self.assertIn('cannot grow', f.check(base, 1)[0])
        f.commit({sc.BASELINE: result['baseline_text']})
        f.check(base, 0)

    def test_marker_removal_and_removed_record_resurrection_are_refused(self):
        f, base = self.fixture()
        result = f.plan(base)
        baseline = result['baseline_text']
        adopted = f.commit({sc.BASELINE: baseline})
        f.check(base, 0)
        f.commit({sc.BASELINE: baseline.replace(A.RULE_MARKER + COVERAGE + '\n', '')})
        self.assertIn('marker cannot be removed', f.check(adopted, 1)[0])
        smaller = '\n'.join(line for line in baseline.splitlines() if not line.startswith('architecture ')) + '\n'
        clean = f.commit({PATH: '', sc.BASELINE: smaller})
        f.check(adopted, 0)
        f.commit({PATH: UNSEEN, sc.BASELINE: baseline})
        self.assertIn('cannot grow', f.check(clean, 1)[0])

    def test_coverage_combines_with_exact_rekeys_without_admitting_unrecorded_debt(self):
        malformed = ("fn first<'a>() { let url = \"http://recorded:24001\"; }\n"
                     "fn next<'b>() {}\n")
        path = 'src/recorded.rs'
        old = ARCH.project_portability.scan(path, malformed,
            lambda t, s: ARCH.source_views(t, s, legacy_rust=True), legacy_rust=True)
        rows = ARCH.with_occurrences([ARCH.finding(path, malformed, start, 'C1', 'project-portability', why, end)
                                     for start, end, why in old])
        self.assertTrue(rows)
        baseline = BASELINE + ''.join(ARCH.baseline_key(row) + '\n' for row in rows)
        f, _ = self.fixture(baseline=baseline)
        base = f.commit({path: malformed})
        result = f.plan(base)
        self.assertIn(COVERAGE, result['introduced_rules'])
        self.assertEqual(result['evidence']['rekeyed_records'], 1)
        f.commit({sc.BASELINE: result['baseline_text']})
        f.check(base, 0)
        # Previously recognized but unrecorded debt is not a coverage expansion.
        f.commit({sc.BASELINE: baseline, 'src/unrecorded.rs': SEEN})
        broken = f.git('rev-parse', 'HEAD')
        with self.assertRaisesRegex(ValueError, 'unrecorded portability debt'):
            f.plan(broken)


if __name__ == '__main__':
    unittest.main()
