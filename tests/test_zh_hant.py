#!/usr/bin/env python3
"""Tests for the zh-hant action: it flags Simplified characters on added zh-Hant lines only."""

from __future__ import annotations

import pathlib
import subprocess
import tempfile
import unittest

ACTION = pathlib.Path(__file__).resolve().parents[1] / ".github" / "actions" / "zh-hant"


def run_check(path: str, before: str, after: str) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as tmp:
        def git(*args: str) -> None:
            subprocess.run(["git", *args], cwd=tmp, check=True, capture_output=True)

        git("init", "-q")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "t")
        doc = pathlib.Path(tmp, path)
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text(before, encoding="utf-8")
        git("add", ".")
        git("commit", "-qm", "base")
        doc.write_text(after, encoding="utf-8")
        git("commit", "-qam", "change")
        return subprocess.run(
            [str(ACTION / "check.sh"), "HEAD~1..HEAD", str(ACTION / "simplified.tsv")],
            cwd=tmp, capture_output=True, text=True, encoding="utf-8",
        )


class ZhHantTest(unittest.TestCase):
    def test_flags_simplified_in_zh_tw_file_with_location_and_suggestion(self) -> None:
        result = run_check("locales/zh_TW.json", "{}\n", '{\n  "hello": "欢迎来到游戏"\n}\n')
        self.assertEqual(result.returncode, 1)
        self.assertIn("file=locales/zh_TW.json,line=2::", result.stdout)
        self.assertIn('write "歡"', result.stdout)

    def test_android_and_ios_locale_paths(self) -> None:
        for path in ("res/values-zh-rHK/strings.xml", "App/zh-Hant.lproj/Localizable.strings"):
            result = run_check(path, "", "设置\n")
            self.assertEqual(result.returncode, 1, path)

    def test_traditional_text_and_shared_characters_pass(self) -> None:
        result = run_check("locales/zh-HK.json", "", "台灣 後面 只有 乾淨 為了 裡面\n")
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_existing_text_does_not_block(self) -> None:
        result = run_check("zh_TW.po", "欢迎\n", "欢迎\n歡迎\n")
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_zh_tw_key_in_multi_locale_file(self) -> None:
        result = run_check("i18n/strings.json", "", '{"zh_cn": "设置", "zh_tw": "设置"}\n')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout.count("::error"), 1)

    def test_simplified_locale_and_other_files_pass(self) -> None:
        self.assertEqual(run_check("i18n/strings.json", "", '{"zh_cn": "设置"}\n').returncode, 0)
        self.assertEqual(run_check("locales/zh_CN.json", "", "设置\n").returncode, 0)

    def test_allow_marker_skips_the_line(self) -> None:
        result = run_check("zh_TW.md", "", "简体示例 zh-hant: allow\n")
        self.assertEqual(result.returncode, 0, result.stdout)


if __name__ == "__main__":
    unittest.main()
