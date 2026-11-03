"""Unit tests for .github/actions/auth-surface-guard/check.py."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "auth_surface_guard",
    ROOT / ".github/actions/auth-surface-guard/check.py",
)
assert SPEC is not None and SPEC.loader is not None
guard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = guard
SPEC.loader.exec_module(guard)


class AuthSurfaceGuardTest(unittest.TestCase):
    def make_repo(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
        (repo / "README.md").write_text("# Product\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
        return repo

    def commit_files(self, repo: Path, files: dict[str, str], message: str = "change") -> tuple[str, str]:
        base = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
        for rel_path, content in files.items():
            full_path = repo / rel_path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", message], check=True)
        head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
        return base, head

    def test_rejects_standalone_signup_and_forgot_password_routes(self) -> None:
        repo = self.make_repo()
        base, head = self.commit_files(
            repo,
            {
                "src/app/(auth)/signup/page.tsx": "export default function Signup() { return <div /> }\n",
                "src/app/forgot-password/page.tsx": "export default function Forgot() { return <div /> }\n",
            },
        )
        findings = guard.scan_diff(repo, base, head)
        rules = [(f.path, f.rule) for f in findings]
        self.assertEqual(
            rules,
            [
                ("src/app/(auth)/signup/page.tsx", "AUTH-R1"),
                ("src/app/forgot-password/page.tsx", "AUTH-R1"),
            ],
        )
        self.assertTrue(all("@sylphx/apps" in f.remediation for f in findings))

    def test_rejects_handwritten_password_input_in_product_tsx(self) -> None:
        repo = self.make_repo()
        base, head = self.commit_files(
            repo,
            {
                "src/components/LoginForm.tsx": (
                    "export function LoginForm() {\n"
                    '  return <input name="password" type="password" />\n'
                    "}\n"
                ),
            },
        )
        findings = guard.scan_diff(repo, base, head)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].rule, "AUTH-R2")
        self.assertEqual(findings[0].line, 2)
        self.assertIn("autocomplete=\"username webauthn\"", findings[0].remediation)

    def test_rejects_public_web_sms_otp_calls_unless_legacy_continuity_annotated(self) -> None:
        repo = self.make_repo()
        base, head = self.commit_files(
            repo,
            {
                "src/auth/phone.ts": "export async function login(p: string) { return sendSmsOtp(p) }\n",
                "src/legacy/hkmt_phone.ts": (
                    "// auth-surface-guard: allow(legacy-continuity)\n"
                    "export async function legacyPhone(p: string) { return sendSmsOtp(p) }\n"
                ),
            },
        )
        findings = guard.scan_diff(repo, base, head)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].path, "src/auth/phone.ts")
        self.assertEqual(findings[0].rule, "AUTH-R3")

    def test_allows_unified_sylphx_auth_and_shared_auth_packages(self) -> None:
        repo = self.make_repo()
        base, head = self.commit_files(
            repo,
            {
                "src/app/login/page.tsx": (
                    "import { SylphxAuth } from '@sylphx/apps'\n"
                    "export default function Login() {\n"
                    '  return <SylphxAuth mode="continue" autocomplete="username webauthn" />\n'
                    "}\n"
                ),
                "services/auth/src/legacy.tsx": 'export const Legacy = () => <input type="password" />\n',
                "packages/apps/src/react/SylphxAuth.tsx": 'export const Input = () => <input type="password" />\n',
            },
        )
        self.assertEqual(guard.scan_diff(repo, base, head), [])
        self.assertEqual(guard.main(["--root", str(repo), "--base", base, "--head", head]), 0)


if __name__ == "__main__":
    unittest.main()
