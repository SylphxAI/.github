#!/usr/bin/env python3
"""Tests for the standards-lint action.

A newly added sleep or polling loop on an event-driven primary path fails.
An event path passes, and so does a bounded resync or recovery whose line
names the exception. Existing lines are not checked.
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import tempfile
import unittest

import yaml

ACTION = pathlib.Path(__file__).resolve().parents[1] / ".github" / "actions" / "standards-lint"


def run_check(
    after: str, path: str = "src/reconcile.rs", before: str = "", mode: str = "fail",
    rules: pathlib.Path | None = None, specs: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as tmp:
        def git(*args: str) -> None:
            subprocess.run(["git", *args], cwd=tmp, check=True, capture_output=True)

        git("init", "-q")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "t")
        doc = pathlib.Path(tmp, path)
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text(before)
        pathlib.Path(tmp, "keep").write_text("x\n")
        git("add", ".")
        git("commit", "-qm", "base")
        doc.write_text(after)
        git("add", ".")
        git("commit", "-qm", "change")
        argv = [str(ACTION / "check.sh"), "HEAD~1..HEAD", str(rules or ACTION / "rules.tsv"), *specs]
        if os.environ.get("SL_DEBUG_ARGV"):
            print("ARGV", argv, flush=True)
        return subprocess.run(
            argv, cwd=tmp, capture_output=True, text=True, env={**os.environ, "STANDARDS_LINT_MODE": mode},
        )


class PrimaryPathFails(unittest.TestCase):
    """A new primary path that sleeps or polls fails, and the finding names the rule and the line."""

    def test_rust_sleep_on_a_primary_path_fails(self) -> None:
        result = run_check("pub async fn tick() {\n    tokio::time::sleep(Duration::from_secs(30)).await;\n}\n")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("::error file=src/reconcile.rs,line=2,title=standards-lint/primary-path.sleep::", result.stdout)
        self.assertIn("standards/architecture.md", result.stdout)
        self.assertIn("[primary-path.sleep]", result.stdout)

    def test_rust_polling_loop_fails(self) -> None:
        result = run_check("fn watch() {\n    while running { let s = status(); }\n}\n")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("line=2,title=standards-lint/primary-path.poll::", result.stdout)

    def test_typescript_timer_and_poll_fail(self) -> None:
        result = run_check("setInterval(() => refresh(), 5000);\nwhile (open) { await fetch(url); }\n", "web/src/live.ts")
        self.assertEqual(result.returncode, 1)
        self.assertIn("file=web/src/live.ts,line=1,title=standards-lint/primary-path.timer::", result.stdout)
        self.assertIn("file=web/src/live.ts,line=2,title=standards-lint/primary-path.poll::", result.stdout)
        self.assertEqual(result.stdout.count("::error "), 2, result.stdout)

    def test_javascript_promise_sleep_fails(self) -> None:
        result = run_check("await new Promise((resolve) => setTimeout(resolve, 1000));\n", "src/wait.mjs")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("title=standards-lint/primary-path.sleep::", result.stdout)
        self.assertIn("title=standards-lint/primary-path.timer::", result.stdout)

    def test_python_sleep_and_poll_fail(self) -> None:
        result = run_check("def loop():\n    while True: status = probe()\n    time.sleep(2)\n", "app/worker.py")
        self.assertEqual(result.returncode, 1)
        self.assertIn("file=app/worker.py,line=2,title=standards-lint/primary-path.poll::", result.stdout)
        self.assertIn("file=app/worker.py,line=3,title=standards-lint/primary-path.sleep::", result.stdout)

    def test_shell_sleep_fails(self) -> None:
        result = run_check("while true; do\n  sleep 5\n  check\ndone\n", "bin/watch.sh")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("file=bin/watch.sh,line=2,title=standards-lint/primary-path.sleep::", result.stdout)

    def test_go_polling_loop_fails(self) -> None:
        result = run_check("func watch() {\n    for {\n        resp, err := client.Get(ctx, key)\n    }\n}\n", "pkg/watch.go")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("file=pkg/watch.go,line=3,title=standards-lint/primary-path.poll::", result.stdout)

    def test_cronjob_schedule_fails(self) -> None:
        result = run_check("kind: CronJob\nspec:\n  schedule: \"*/2 * * * *\"\n", "deploy/reconcile.yaml")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("file=deploy/reconcile.yaml,line=3,title=standards-lint/primary-path.cron::", result.stdout)

    def test_each_finding_names_its_rule_and_its_line(self) -> None:
        result = run_check("time.sleep(1)\ntime.sleep(1)\n", "app/worker.py")
        self.assertEqual(result.returncode, 1)
        self.assertIn("line=1,title=standards-lint/primary-path.sleep::", result.stdout)
        self.assertIn("line=2,title=standards-lint/primary-path.sleep::", result.stdout)
        self.assertIn("2 standards-lint finding(s)", result.stdout)


class EventPathsAndBoundedResyncPass(unittest.TestCase):
    def test_event_paths_pass(self) -> None:
        samples = {
            "src/reconcile.rs": "fn on_event(ev: Event) { reconcile(current()); }\nlet _ = rx.recv().await;\n",
            "web/src/live.ts": "source.addEventListener('change', (ev) => apply(ev.data));\nconst stream = new EventSource(url);\n",
            "app/worker.py": "conn.add_listener('changed', reconcile)\nselect.select([conn], [], [])\n",
            "pkg/watch.go": "watcher, err := client.Watch(ctx, list)\nfor ev := range watcher.ResultChan() { reconcile(ev) }\n",
            "deploy/reconcile.yaml": "kind: Deployment\nspec:\n  replicas: 1\n",
        }
        for path, text in samples.items():
            result = run_check(text, path)
            self.assertEqual(result.returncode, 0, f"{path}: {result.stdout}")

    def test_bounded_resync_with_a_reason_passes(self) -> None:
        line = "tokio::time::sleep(RESYNC).await; // standards-lint: allow(bounded resync: repairs a missed notify, 300s)\n"
        result = run_check(line)
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_recovery_backoff_with_a_reason_passes(self) -> None:
        line = "time.sleep(backoff)  # standards-lint: allow(recovery: reconnect after a dropped watch)\n"
        result = run_check(line, "app/worker.py")
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_a_bare_allow_still_fails(self) -> None:
        result = run_check("time.sleep(2)  # standards-lint: allow\n", "app/worker.py")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("[primary-path.sleep]", result.stdout)

    def test_an_allow_without_a_reason_still_fails(self) -> None:
        result = run_check("time.sleep(2)  # standards-lint: allow()\n", "app/worker.py")
        self.assertEqual(result.returncode, 1, result.stdout)

    def test_existing_code_does_not_block_a_change(self) -> None:
        before = "while True:\n    probe()\n    time.sleep(2)\n"
        result = run_check(before + "log('still polling')\n", "app/worker.py", before)
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_deleting_a_loop_passes(self) -> None:
        result = run_check("", "app/worker.py", "while True: probe()\ntime.sleep(2)\n")
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_warn_mode_annotates_and_passes(self) -> None:
        result = run_check("time.sleep(2)\n", "app/worker.py", mode="warn")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("::warning file=app/worker.py,line=1,title=standards-lint/primary-path.sleep::", result.stdout)

    def test_an_excluded_path_is_not_checked(self) -> None:
        result = run_check("time.sleep(2)\n", "evidence/capture.py", specs=(":(exclude)evidence/**",))
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_a_file_with_no_rule_is_not_checked(self) -> None:
        result = run_check("time.sleep(2)\nsetInterval(() => {}, 1)\n", "notes.md")
        self.assertEqual(result.returncode, 0, result.stdout)


class RulesTable(unittest.TestCase):
    def test_every_row_has_four_columns_and_compiles(self) -> None:
        seen = set()
        for row in (ACTION / "rules.tsv").read_text().splitlines():
            if not row.strip() or row.lstrip().startswith("#"):
                continue
            ext, pattern, message, code = row.split("\t")
            re.compile(ext)
            re.compile(pattern)
            self.assertTrue(message.strip(), row)
            self.assertRegex(code, r"^primary-path\.[a-z]+$")
            seen.add((ext, pattern))
        self.assertGreaterEqual(len(seen), 8)

    def test_a_short_row_fails_loudly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bad = pathlib.Path(tmp, "rules.tsv")
            bad.write_text("\\.py$\ttime\\.sleep\\(\ta sleep\n")
            result = run_check("x\n", rules=bad)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rules row needs four columns", result.stderr)

    def test_action_points_at_its_own_rules_and_takes_no_rule_input(self) -> None:
        action = yaml.safe_load((ACTION / "action.yml").read_text())
        self.assertEqual(set(action["inputs"]), {"mode", "exclude"})
        run = action["runs"]["steps"][0]["run"]
        self.assertIn('"$GITHUB_ACTION_PATH/rules.tsv"', run)
        self.assertNotIn("inputs.rules", run)


if __name__ == "__main__":
    unittest.main()
