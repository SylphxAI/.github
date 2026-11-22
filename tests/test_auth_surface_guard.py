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

    def run_guard(self, repo, *args):
        return subprocess.run([sys.executable, SPEC.origin, '--root', str(repo), *args],
                              text=True, capture_output=True)

    def test_full_audits_selected_revision_not_index_or_untracked_files(self):
        repo = self.make_repo()
        _, head = self.commit_files(repo, {
            'src/app/register/page.tsx': '',
            'src/ui/login.vue': '<input\n  type="password" />\n',
            'src/auth/phone.ts': 'sendSmsOtp(phone)\n',
            'src/ui/entry.tsx': '<SylphxAuth />\n',
        })
        (repo / 'src/auth/phone.ts').write_text('sendEmailOtp(email)\n')
        (repo / 'src/auth/untracked.ts').write_text('verifySmsOtp(code)\n')
        subprocess.run(['git', '-C', str(repo), 'add', 'src/auth/phone.ts'], check=True)
        result = self.run_guard(repo, '--mode', 'full', '--head', head)
        self.assertEqual(result.returncode, 1, result.stderr)
        for path, rule in [('src/app/register/page.tsx', 'AUTH-R1'),
                           ('src/ui/login.vue', 'AUTH-R2'), ('src/auth/phone.ts', 'AUTH-R3')]:
            self.assertIn(f'{path}:1: [{rule}]', result.stdout)
        self.assertNotIn('untracked', result.stdout)

    def test_full_passes_shared_test_and_legacy_continuity_paths(self):
        repo = self.make_repo()
        _, base = self.commit_files(repo, {
            **{p + 'routes/signup.tsx': '<input type="password" />\nsendSmsOtp(phone)\n'
               for p in guard.SHARED_AUTH_PREFIXES},
            'tests/app/register/page.tsx': '',
            'src/login.spec.tsx': '<input type="password" />\n',
            'src/legacy/phone.ts': '// auth-surface-guard: allow(legacy-continuity)\nsendSmsOtp(phone)\n',
        })
        result = self.run_guard(repo, '--mode', 'full', '--base', base)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_strict_diff_does_not_let_new_debt_inherit_a_continuity_comment(self):
        repo = self.make_repo()
        path = 'src/legacy/phone.ts'
        marker = '// auth-surface-guard: allow(legacy-continuity)\n'
        _, base = self.commit_files(repo, {path: marker + 'sendSmsOtp(phone)\n'})
        self.commit_files(repo, {path: marker + 'sendSmsOtp(phone)\nverifySmsOtp(code)\n'})
        for mode in ('diff', 'full'):
            result = self.run_guard(repo, '--strict', '--mode', mode, '--base', base)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn(f'{path}:3: [AUTH-R3]', result.stdout)
            self.assertNotIn(f'{path}:2: [AUTH-R3]', result.stdout)

    def test_strict_diff_accepts_inherited_debt_and_rejects_fresh_debt(self):
        repo = self.make_repo()
        path = 'src/ui/Login.tsx'
        route = 'src/app/reset-password/page.tsx'
        legacy = '<input type="password" />\n'
        _, base = self.commit_files(repo, {path: legacy, route: ''})
        self.commit_files(repo, {path: '\n' + legacy})
        result = self.run_guard(repo, '--strict', '--base', base)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.commit_files(repo, {path: legacy + legacy})
        result = self.run_guard(repo, '--strict', '--base', base)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(f'{path}:2: [AUTH-R2]', result.stdout)
        self.assertNotIn('AUTH-R1', result.stdout)

    def test_baseline_seed_from_base_may_shrink_but_not_hide_new_or_stale_debt(self):
        repo = self.make_repo()
        path = 'src/ui/(auth) Login.tsx'
        route = 'app/signup/page.tsx'
        baseline = '.github/auth-departures.txt'
        password = '<input type="password" />\n'
        _, base = self.commit_files(repo, {path: password, route: ''})
        report = self.run_guard(repo, '--mode', 'full', '--head', base, '--print-baseline')
        self.assertEqual(report.returncode, 0, report.stderr)
        self.assertIn('src/ui/%28auth%29%20Login.tsx', report.stdout)
        records = report.stdout
        self.commit_files(repo, {baseline: records, path: '// existing legacy UI\n' + password})
        args = ('--mode', 'full', '--baseline', baseline)
        result = self.run_guard(repo, *args, '--base', base)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.run_guard(repo, *args).returncode, 0)

        adopted = guard.git(repo, 'rev-parse', 'HEAD').decode().strip()
        self.commit_files(repo, {path: password + password})
        result = self.run_guard(repo, *args, '--base', adopted)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('AUTH-R2', result.stdout)
        grown = self.run_guard(repo, '--mode', 'full', '--print-baseline').stdout
        self.commit_files(repo, {baseline: grown})
        result = self.run_guard(repo, *args, '--base', adopted)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('baseline grew', result.stdout)
        result = self.run_guard(repo, *args, '--base', base)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('baseline finding is not inherited', result.stdout)

        self.commit_files(repo, {path: '<SylphxAuth />\n', baseline: records})
        result = self.run_guard(repo, *args, '--base', adopted)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('stale baseline entry', result.stdout)
        kept = ''.join(line + '\n' for line in records.splitlines() if 'AUTH-R1' in line)
        self.commit_files(repo, {baseline: kept})
        result = self.run_guard(repo, *args, '--base', adopted)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_required_inputs_fail_closed(self):
        repo = self.make_repo()
        baseline = '.github/auth-departures.txt'
        base, _ = self.commit_files(repo, {baseline: 'invalid baseline syntax\n'})
        for args, reason in [
            (('--strict', '--base', 'missing'), 'explicit base'),
            (('--strict',), 'explicit base'),
            (('--mode', 'full', '--base', 'missing'), 'explicit base'),
            (('--mode', 'full', '--head', base, '--baseline', baseline), 'not tracked'),
            (('--mode', 'full', '--baseline', baseline), 'invalid or duplicate'),
            (('--mode', 'full', '--baseline', '../baseline'), 'repository-relative'),
        ]:
            with self.subTest(args=args):
                result = self.run_guard(repo, *args)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(reason, result.stderr)


if __name__ == "__main__":
    unittest.main()
