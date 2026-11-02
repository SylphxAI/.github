#!/usr/bin/env python3
"""No compiler, registry, or network needed for these Rust wiring regressions."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('reachability', Path(__file__).resolve().parents[1] / '.github/actions/stack-conformance/rust_reachability.py')
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


class ReachabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        self.put('Cargo.toml', '[package]\nname="fixture"\nversion="0.1.0"\nedition="2021"\n')
        self.put('src/lib.rs', '')

    def put(self, path, source):
        file = self.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(source)

    def result(self):
        census = check.Census(self.root)
        census.discover()
        return census.result()

    def test_orphan_fails_then_module_wiring_passes(self):
        self.put('src/money_tests.rs', '#[test] fn payment_is_granted() {}')
        self.assertEqual(self.result()['orphans'], ['src/money_tests.rs'])
        run = subprocess.run(['python3', str(Path(check.__file__)), str(self.root)], capture_output=True, text=True)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('src/money_tests.rs', run.stderr)
        self.put('src/lib.rs', '#[cfg(test)] mod money_tests;')
        self.assertEqual(self.result()['orphans'], [])
        self.assertEqual(subprocess.run(['python3', str(Path(check.__file__)), str(self.root)], capture_output=True).returncode, 0)

    def test_comments_and_strings_cannot_wire_orphans(self):
        self.put('src/fake.rs', '')
        self.put('src/lib.rs', '''// mod fake;
/* nested /* mod fake; */ mod fake; */
const A: &str = "mod fake;";
const B: &str = r###"mod fake; /* \" }"###;
const C: char = '\\'';
''')
        self.assertEqual(self.result()['orphans'], ['src/fake.rs'])

    def test_raw_string_does_not_swallow_following_module(self):
        self.put('src/real.rs', '')
        self.put('src/lib.rs', 'const X: &str = r#"a \" { /*"#; mod real;')
        self.assertEqual(self.result()['orphans'], [])

    def test_cfg_union_raw_ident_and_inline_modules(self):
        self.put('src/lib.rs', '#[cfg(windows)] mod r#box; mod inline { mod child; }')
        self.put('src/box.rs', '')
        self.put('src/inline/child.rs', '')
        self.assertEqual(self.result()['orphans'], [])

    def test_path_override_does_not_count_shadowed_file(self):
        self.put('src/lib.rs', '#[path="actual.rs"] mod shadow;')
        self.put('src/actual.rs', '')
        self.put('src/shadow.rs', '')
        self.assertEqual(self.result()['orphans'], ['src/shadow.rs'])

    def test_cfg_attr_paths_keep_default_branch(self):
        self.put('src/lib.rs', '#[cfg_attr(windows,path="windows.rs")] mod portable;')
        self.put('src/windows.rs', '')
        self.put('src/portable.rs', '')
        self.assertEqual(self.result()['orphans'], [])

    def test_non_mod_rs_paths_and_nested_inline_path(self):
        self.put('src/lib.rs', 'mod outer;')
        self.put('src/outer.rs', '#[path="sibling.rs"] mod sibling; mod inline { #[path="special.rs"] mod inner; }')
        self.put('src/sibling.rs', '')
        self.put('src/outer/inline/special.rs', '')
        self.assertEqual(self.result()['orphans'], [])

    def test_comments_between_module_tokens(self):
        self.put('src/lib.rs', 'mod /* { nested /* comment */ } */ real /* } */;')
        self.put('src/real.rs', '')
        self.assertEqual(self.result()['orphans'], [])

    def test_include_literal_concat_and_generated_output(self):
        self.put('src/lib.rs', 'include!("parts.rs"); include!(concat!(env!("CARGO_MANIFEST_DIR"),"/other.rs")); include!(concat!(env!("OUT_DIR"),"/generated.rs"));')
        self.put('src/parts.rs', '')
        self.put('other.rs', '')
        result = self.result()
        self.assertEqual(result['orphans'], [])
        self.assertEqual(result['dynamic_includes'], [])

    def test_include_str_is_not_compilation(self):
        self.put('src/lib.rs', 'const SOURCE: &str = include_str!("tests.rs");')
        self.put('src/tests.rs', '#[test] fn never_runs() {}')
        self.assertEqual(self.result()['orphans'], ['src/tests.rs'])

    def test_build_bins_tests_benches_examples_and_excluded_crates(self):
        self.put('Cargo.toml', (self.root/'Cargo.toml').read_text()+'\n[workspace]\nexclude=["other"]\n')
        for path in ['build.rs','src/main.rs','src/bin/tool.rs','src/bin/dir/main.rs','tests/api.rs','tests/suite/main.rs','benches/bench.rs','examples/demo.rs']:
            self.put(path, '')
        self.put('other/Cargo.toml', '[package]\nname="other"\nversion="0.1.0"\n')
        self.put('other/src/lib.rs', 'mod unit;')
        self.put('other/src/unit.rs', '')
        self.assertEqual(self.result()['orphans'], [])

    def test_explicit_target_overrides_automatic_same_name(self):
        self.put('Cargo.toml', (self.root/'Cargo.toml').read_text()+'\n[[bin]]\nname="tool"\npath="special.rs"\n')
        self.put('special.rs', '')
        self.put('src/bin/tool.rs', '')
        self.assertEqual(self.result()['orphans'], ['src/bin/tool.rs'])

    def test_disabled_automatic_targets_are_not_roots(self):
        self.put('Cargo.toml', (self.root/'Cargo.toml').read_text()+'autotests=false\n')
        self.put('tests/unused.rs', '')
        self.assertEqual(self.result()['orphans'], ['tests/unused.rs'])

    def test_trybuild_glob_fixtures_are_reached_but_not_string_payloads(self):
        self.put('tests/ui.rs', 'fn ui() { let t = trybuild::TestCases::new(); t.compile_fail("tests/ui/fail_*.rs"); t.pass("tests/ui/pass.rs"); }')
        for path in ['tests/ui/fail_type.rs', 'tests/ui/pass.rs']:
            self.put(path, 'fn main() {}')
        self.put('tests/ui/unused.rs', 'fn main() {}')
        self.assertEqual(self.result()['orphans'], ['tests/ui/unused.rs'])

    def test_edition_2015_explicit_targets_disable_automatic_discovery(self):
        self.put('Cargo.toml', '[package]\nname="fixture"\nversion="0.1.0"\n[[bin]]\nname="chosen"\npath="chosen.rs"\n')
        self.put('chosen.rs', '')
        self.put('tests/unused.rs', '')
        self.assertEqual(self.result()['orphans'], ['src/lib.rs', 'tests/unused.rs'])

    def test_inherited_modern_edition_keeps_automatic_discovery(self):
        self.put('Cargo.toml', '[workspace]\nmembers=["child"]\n[workspace.package]\nedition="2021"\n')
        (self.root/'src/lib.rs').unlink()
        self.put('child/Cargo.toml', '[package]\nname="child"\nversion="0.1.0"\nedition.workspace=true\n[[bin]]\nname="chosen"\npath="chosen.rs"\n')
        self.put('child/chosen.rs', '')
        self.put('child/src/lib.rs', '')
        self.put('child/tests/automatic.rs', '')
        self.assertEqual(self.result()['orphans'], [])

    def test_dynamic_include_requires_inspection(self):
        self.put('src/lib.rs', 'include!(custom_source!());')
        self.assertEqual(self.result()['dynamic_includes'], ['src/lib.rs'])

    def test_exact_exceptions_need_reasons_and_expire_when_wired(self):
        self.put('src/template.rs', '')
        self.put('.rust-reachability.json', json.dumps({'src/template.rs': 'Generator template copied into its consumer crate.'}))
        self.assertEqual(self.result()['orphans'], [])
        self.put('src/lib.rs', 'mod template;')
        self.assertTrue(self.result()['errors'])
        self.put('.rust-reachability.json', json.dumps({'src/*.rs': 'Broad baseline is not allowed.'}))
        self.assertTrue(self.result()['errors'])


if __name__ == '__main__':
    unittest.main()
