"""Literal process recognition and exact-occurrence force-push ratchet."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("no_force_push", ROOT / ".github/actions/stack-conformance/no_force_push.py")
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


class Tree:
    def __init__(self, files): self.files = files
    def paths(self): return self.files.keys()
    def read(self, path): return self.files.get(path)


def scan(text, path="scripts/publish.sh"):
    return guard.scan(Tree({path: text}))


class CommandTests(unittest.TestCase):
    def test_must_fail_and_pass_fixtures(self):
        fixtures = json.loads((ROOT / "tests/fixtures/no-force-push.json").read_text())
        for expected, commands in fixtures.items():
            for command in commands:
                with self.subTest(expected=expected, command=command):
                    report = scan(command)
                    self.assertEqual(report["unreadable"], [])
                    self.assertEqual(bool(report["findings"]), expected == "must_fail")

    def test_workflow_run_scalars_only_with_exact_lines(self):
        workflow = """name: git push --force
jobs:
  pin:
    steps:
      - name: git push --force
        run: |
          echo ready
          git push --force origin HEAD:pin
      - run: 'git push origin +HEAD:other'
      - run: echo 'git push --force'
"""
        report = scan(workflow, ".github/workflows/pin.yml")
        self.assertEqual(report["unreadable"], [])
        self.assertEqual([r["line"] for r in report["findings"]], [8, 9])

    def test_python_literal_process_calls_and_constant_argv(self):
        report = scan("""import subprocess
message = 'git push --force origin pin'
subprocess.run(['git', 'fetch', '--force', 'origin'])
subprocess.run(['git', 'push', '--force', 'origin'])
args = ['git', '-C', 'repo', 'push', 'origin', '+HEAD:pin']
subprocess.check_call(args)
subprocess.run('git push -f origin pin', shell=True)
""", "scripts/publish.py")
        self.assertEqual(report["unreadable"], [])
        self.assertEqual([r["line"] for r in report["findings"]], [4, 6, 7])

    def test_javascript_process_calls(self):
        report = scan("""const mention = 'git push --force origin pin';
execSync('git push --force origin pin');
spawn('git', ['push', '-f', 'origin', 'HEAD:pin']);
spawnSync('git', ['push', 'origin', 'pin']);
// execSync('git push --force origin pin');
""", "scripts/publish.ts")
        self.assertEqual(report["unreadable"], [])
        self.assertEqual([r["line"] for r in report["findings"]], [2, 3])

    def test_package_scripts(self):
        report = scan(json.dumps({"description": "git push --force", "scripts": {
            "publish": "git push origin +HEAD:pin", "ok": "git push origin pin"}}), "package.json")
        self.assertEqual(len(report["findings"]), 1)

    def test_test_scripts_are_checked_but_fixture_data_is_not_executed(self):
        report = guard.scan(Tree({"tests/publish.sh": "git push -f origin pin",
                                  "tests/fixtures/input.txt": "git push -f origin pin",
                                  "docs/readme.md": "git push -f origin pin"}))
        self.assertEqual([r["path"] for r in report["findings"]], ["tests/publish.sh"])

    def test_product_vcs_library_method_is_outside_automation_scope(self):
        report = scan("import subprocess\ndef push():\n subprocess.run(['git','push','--force','origin','pin'])\n", "src/vcs.py")
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["unreadable"], [])

    def test_commands_and_credentials_never_appear_in_findings_or_errors(self):
        report = scan("git push --force https://example.invalid/private-credential HEAD:pin")
        errors = guard.decide(report["findings"], set(), set(), [], True, True)
        self.assertNotIn("private-credential", json.dumps([report, errors]))

    def test_unreadable_candidate_fails_closed(self):
        report = guard.scan(Tree({"scripts/publish.sh": None}))
        self.assertEqual(report["unreadable"], [{"path": "scripts/publish.sh", "reason": "Git blob unavailable"}])

    def test_nested_workflow_and_root_composite_action(self):
        report = guard.scan(Tree({"clients/.github/workflows/publish.yml": "run: git push -f origin pin",
                                  "action.yml": "runs:\n  steps:\n    - run: git push origin +HEAD:pin\n"}))
        self.assertEqual(len(report["findings"]), 2)
        self.assertEqual(report["unreadable"], [])

    def test_shell_forwarding_helpers_resolve_across_files(self):
        helpers = '''git_c() {
  git -c credential.helper= "$@"
}
push_branch() {
  local branch="$1"
  shift
  git_c push "$@" origin "HEAD:refs/heads/$branch"
}
'''
        report = guard.scan(Tree({".github/helpers.sh": helpers,
            ".github/workflows/pin.yml": "run: |\n  if ! push_branch pin --force-with-lease; then exit 1; fi\n"}))
        self.assertEqual(len(report["findings"]), 1)
        self.assertEqual(report["findings"][0]["path"], ".github/workflows/pin.yml")
        self.assertEqual(report["findings"][0]["line"], 2)
        self.assertEqual(report["unreadable"], [])

    def test_comment_and_continuation_line_numbers(self):
        report = scan("echo ready # comment\ngit push \\\n --force origin pin\ngit push -f origin next\n")
        self.assertEqual([r["line"] for r in report["findings"]], [2, 4])

    def test_nested_command_substitutions_and_quoted_output(self):
        report = scan('''url="$(gh pr create --title "pin" --body "the release's digest")"
result="$(git push --force origin pin)"
echo '$(git push -f origin pin)'
''')
        self.assertEqual(report["unreadable"], [])
        self.assertEqual([r["line"] for r in report["findings"]], [2])

    def test_docker_and_make_commands(self):
        report = guard.scan(Tree({"Dockerfile": "FROM example\nRUN git push -f origin HEAD:pin\n",
                                  "Makefile": "publish:\n\t@git push --mirror origin\n"}))
        self.assertEqual(len(report["findings"]), 2)
        self.assertEqual(report["unreadable"], [])

    def test_heredoc_data_and_embedded_scripts(self):
        report = scan('''git push -f origin pin
body="$(cat <<EOF
The release's digest is ready. `echo ready`
EOF
)"
cat <<'DOC'
git push --force origin example
DOC
python3 - <<'PY'
import subprocess
subprocess.run(['git', 'push', '--force', 'origin', 'next'])
PY
''')
        self.assertEqual(report["unreadable"], [])
        self.assertEqual([r["line"] for r in report["findings"]], [11, 1])

    def test_indented_workflow_python_heredoc(self):
        report = scan('''jobs:
  publish:
    steps:
      - run: |
          python3 - <<'PY'
          import subprocess
          subprocess.run(['git', 'push', '--force', 'origin', 'pin'])
          PY
''', ".github/workflows/publish.yml")
        self.assertEqual(report["unreadable"], [])
        self.assertEqual([r["line"] for r in report["findings"]], [7])


class RatchetTests(unittest.TestCase):
    def setUp(self):
        self.rows = scan("git push -f origin HEAD:pin")["findings"]
        self.records = {guard.baseline_key(row) for row in self.rows}

    def decide(self, rows=None, head=None, base=None, before=None, *, adopted=True, was_adopted=True, seed=False):
        return guard.decide(copy.deepcopy(self.rows if rows is None else rows),
                            self.records if head is None else head,
                            self.records if base is None else base,
                            self.rows if before is None else before,
                            adopted, was_adopted, allow_seed=seed)

    def test_exact_inherited_record_passes_and_can_only_shrink(self):
        self.assertEqual(self.decide(), [])
        self.assertEqual(self.decide(rows=[], head=set()), [])
        self.assertTrue(any("stale" in e for e in self.decide(rows=[])))

    def test_first_adoption_only_seeds_exact_base_occurrences(self):
        self.assertEqual(self.decide(base=set(), was_adopted=False, seed=True), [])
        self.assertTrue(self.decide(base=set(), before=[], was_adopted=False, seed=True))
        self.assertTrue(self.decide(base=set(), was_adopted=False))

    def test_duplicate_and_changed_commands_cannot_hide_in_inherited_debt(self):
        duplicated = scan("git push -f origin HEAD:pin\ngit push -f origin HEAD:pin")["findings"]
        changed = scan("git push -f origin HEAD:another")["findings"]
        self.assertTrue(self.decide(rows=duplicated))
        self.assertTrue(self.decide(rows=changed))
        self.assertTrue(self.decide(rows=duplicated, head={guard.baseline_key(r) for r in duplicated}))

    def test_line_movement_preserves_exact_command_identity(self):
        moved = scan("# a comment\necho ready\ngit push -f origin HEAD:pin")["findings"]
        self.assertEqual(self.decide(rows=moved), [])

    def test_marker_removal_is_refused_even_when_empty(self):
        self.assertTrue(self.decide(rows=[], head=set(), base=set(), adopted=False))
        self.assertTrue(self.decide(adopted=False, was_adopted=False, seed=True))


if __name__ == "__main__":
    unittest.main()
