#!/usr/bin/env python3
"""Tests for the chat-senders action: direct chat-API hosts on added lines fail outside the allow-list."""

from __future__ import annotations

import os
import pathlib
import subprocess
import tempfile
import unittest

import yaml

ACTION = pathlib.Path(__file__).resolve().parents[1] / ".github" / "actions" / "chat-senders"


def run_check(
    after: str, path: str = "src/send.ts", before: str = "", *, mode: str = "fail",
    repository: str = "example/repo", allow: pathlib.Path | None = None,
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
        return subprocess.run(
            [str(ACTION / "check.sh"), "HEAD~1..HEAD", str(allow or ACTION / "allow-list.tsv")],
            cwd=tmp, capture_output=True, text=True,
            env={**os.environ, "CHAT_SENDERS_MODE": mode, "CHECK_REPOSITORY": repository},
        )


HOSTS = [
    "https://api.telegram.org/bot${token}/sendMessage",
    "https://hooks.slack.com/services/T000/B000/XXXX",
    "https://slack.com/api/chat.postMessage",
    "https://discord.com/api/webhooks/1/abc",
    "https://discordapp.com/api/webhooks/1/abc",
]


class ChatSendersTest(unittest.TestCase):
    def test_every_host_on_an_added_line_fails(self) -> None:
        for host in HOSTS:
            result = run_check(f"const url = `{host}`;\n")
            self.assertEqual(result.returncode, 1, host)
            self.assertIn("::error file=src/send.ts,line=1::chat-senders: ", result.stdout)
            self.assertIn("send through Notify", result.stdout)

    def test_host_match_ignores_case(self) -> None:
        result = run_check('URL = "https://API.Telegram.ORG/bot"\n', "app/send.py")
        self.assertEqual(result.returncode, 1, result.stdout)

    def test_each_offending_line_is_reported_once_with_its_number(self) -> None:
        result = run_check("a\nfetch('https://api.telegram.org/x')\nb\nfetch('https://hooks.slack.com/y')\n")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout.count("::error "), 2, result.stdout)
        self.assertIn("line=2::", result.stdout)
        self.assertIn("line=4::", result.stdout)
        self.assertIn("2 direct chat-API host(s)", result.stdout)

    def test_existing_lines_do_not_block_a_change(self) -> None:
        before = "const u = 'https://api.telegram.org/bot';\n"
        result = run_check(before + "const other = 1;\n", before=before)
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_other_hosts_pass(self) -> None:
        result = run_check("fetch('https://telegram.org/');\nfetch('https://slack.com/');\n"
                           "fetch('https://discord.com/channels/1');\n")
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_warn_mode_annotates_and_passes(self) -> None:
        result = run_check("fetch('https://api.telegram.org/x')\n", mode="warn")
        self.assertEqual(result.returncode, 0)
        self.assertIn("::warning file=src/send.ts,line=1::", result.stdout)

    def test_tests_fixtures_and_docs_pass_in_any_repository(self) -> None:
        for path in ("tests/adapter.rs", "web/tests/a.test.ts", "src/__tests__/x.ts", "pkg/send_test.go",
                     "crates/x/fixtures/golden.json", "src/send.spec.ts", "app/test_send.py",
                     "docs/adr/notify.md"):
            result = run_check("const u = 'https://api.telegram.org/bot';\n", path)
            self.assertEqual(result.returncode, 0, f"{path}: {result.stdout}")

    def test_notify_sender_passes_only_in_its_repository(self) -> None:
        path = "services/notify/crates/notify-api/src/senders.rs"
        line = 'const API: &str = "https://api.telegram.org";\n'
        self.assertEqual(run_check(line, path, repository="SylphxAI/cloud").returncode, 0)
        self.assertEqual(run_check(line, path, repository="SylphxAI/other").returncode, 1)

    def test_decision_seven_exceptions_pass(self) -> None:
        line = "https://api.telegram.org/bot\n"
        for repo, path in [
            ("SylphxAI/work", "src/v2_act.rs"),
            ("SylphxAI/work", "src/main.rs"),
            ("SylphxAI/agents", "crates/spiron-channels/src/telegram_file.rs"),
            ("SylphxAI/agents", "crates/spiron-api/src/ingress/telegram_webhook_live/part_00.rs"),
            ("SylphxAI/cloud",
             "services/ai/crates/ai-server/src/capabilities/control_plane/interfaces/telegram_operator_bot/wire.rs"),
            ("SylphxAI/infra", "status/worker/index.mjs"),
            ("SylphxAI/infra", "infra/addons/monitoring/backup-alerts/alert-tickets/configmap.yaml"),
        ]:
            result = run_check(line, path, repository=repo)
            self.assertEqual(result.returncode, 0, f"{repo} {path}: {result.stdout}")

    def test_a_product_path_beside_an_exception_fails(self) -> None:
        result = run_check("https://api.telegram.org/bot\n", "src/notify.rs", repository="SylphxAI/work")
        self.assertEqual(result.returncode, 1, result.stdout)

    def test_deleted_lines_and_files_pass(self) -> None:
        result = run_check("", before="fetch('https://api.telegram.org/x')\n")
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_a_malformed_allow_list_row_fails_loudly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bad = pathlib.Path(tmp, "allow.tsv")
            bad.write_text("SylphxAI/cloud\n")
            result = run_check("x\n", allow=bad)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("allow-list row needs", result.stderr)

    def test_allow_list_rows_have_three_columns_and_compile(self) -> None:
        import re
        for row in (ACTION / "allow-list.tsv").read_text().splitlines():
            if not row.strip() or row.lstrip().startswith("#"):
                continue
            repo, path, why = row.split("\t")
            self.assertTrue(repo == "*" or re.fullmatch(r"[\w.-]+/[\w.-]+", repo), row)
            re.compile(path)
            self.assertTrue(why.strip(), row)

    def test_action_has_no_caller_input_that_widens_the_allow_list(self) -> None:
        action = yaml.safe_load((ACTION / "action.yml").read_text())
        self.assertEqual(set(action["inputs"]), {"mode"})
        run = action["runs"]["steps"][0]["run"]
        self.assertIn('"$GITHUB_ACTION_PATH/allow-list.tsv"', run)
        self.assertEqual(action["runs"]["steps"][0]["env"]["CHECK_REPOSITORY"], "${{ github.repository }}")


if __name__ == "__main__":
    unittest.main()
