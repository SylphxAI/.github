"""Unit tests for the auth surface checker and standalone composite."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / ".github/actions/auth-surface-guard"
SPEC = importlib.util.spec_from_file_location(
    "auth_surface_guard",
    ACTION / "check.py",
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

    def run_action(self, repo, *, event_name="workflow_dispatch", event=None, **overrides):
        action = yaml.safe_load((ACTION / "action.yml").read_text(encoding="utf-8"))
        self.assertEqual(action["runs"]["using"], "composite")
        step, = action["runs"]["steps"]
        self.assertEqual(step["shell"], "bash")
        self.assertLessEqual(overrides.keys(), action["inputs"].keys())
        inputs = {name: metadata["default"] for name, metadata in action["inputs"].items()}
        inputs.update(root=str(repo), **overrides)
        context = {"inputs": inputs, "github": {"event": event or {}, "token": ""}}

        # The manifest uses only whole-value, dotted context references. Model
        # the runner's empty string for missing fields without copying the
        # composite's event or base-selection logic here.
        def resolve(value):
            self.assertIsInstance(value, str)
            if not value.startswith("${{"):
                return value
            self.assertTrue(value.endswith("}}"), value)
            result = context
            for key in value[3:-2].strip().split("."):
                result = result.get(key, {})
            return "" if result == {} else resolve(result)

        env = {**os.environ, **{key: resolve(value) for key, value in step["env"].items()}}
        env.update(GITHUB_EVENT_NAME=event_name, GITHUB_ACTION_PATH=str(ACTION))
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
            env=env, text=True, capture_output=True, check=False,
        )

    def test_action_diff_uses_the_current_event_base_not_head_parent(self):
        repo = self.make_repo()
        path = "src/app/signup/page.tsx"
        base, _ = self.commit_files(repo, {path: "export default function Signup() {}\n"})
        parent, head = self.commit_files(repo, {"README.md": "# Updated product\n"})
        for event_name in ("pull_request", "merge_group", "push"):
            for expected, event_base in ((0, parent), (1, base)):
                with self.subTest(event_name=event_name, expected=expected):
                    event = {
                        "pull_request": {"base": {"sha": event_base if event_name == "pull_request" else head}},
                        "merge_group": {"base_sha": event_base if event_name == "merge_group" else head},
                        "before": event_base if event_name == "push" else head,
                    }
                    result = self.run_action(repo, event_name=event_name, event=event)
                    self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                    self.assertIn("diff audit", result.stdout)
                    if expected:
                        self.assertIn(path, result.stdout)
                        self.assertIn("AUTH-R1", result.stdout)

    def test_action_explicit_base_ref_overrides_alias_and_event_base(self):
        repo = self.make_repo()
        base, head = self.commit_files(repo, {"src/app/signup/page.tsx": ""})
        events = (("pull_request", {"pull_request": {"base": {"sha": base}}}),
                  ("push", {"before": "0" * 40}), ("schedule", {}), ("workflow_dispatch", {}))
        for event_name, event in events:
            for inputs, expected in (({"base-ref": head, "base": base}, 0), ({"base": base}, 1),
                                     ({"base-ref": "absent-base", "base": head}, 2)):
                with self.subTest(event_name=event_name, inputs=inputs):
                    result = self.run_action(repo, event_name=event_name, event=event, **inputs)
                    self.assertEqual(result.returncode, expected, result.stdout + result.stderr)

    def test_action_diff_fails_closed_for_missing_or_unavailable_base(self):
        repo = self.make_repo()
        self.commit_files(repo, {"README.md": "# Updated product\n"})
        events = (
            ("pull_request", {"pull_request": {"base": {"sha": "absent-base"}}}),
            ("merge_group", {"merge_group": {"base_sha": "absent-base"}}),
            ("push", {"before": "absent-base"}),
            ("push", {"before": "0" * 40}),
            ("push", {"before": " "}),
            ("pull_request", {}), ("merge_group", {}), ("push", {}),
            ("workflow_dispatch", {}), ("schedule", {}),
        )
        for event_name, event in events:
            # Legacy parent fallback is only for events without a diff base
            # contract, not a substitute for a missing event range.
            strict_values = ("true",) if event_name in ("workflow_dispatch", "schedule") else ("true", "false")
            for strict in strict_values:
                with self.subTest(event_name=event_name, event=event, strict=strict):
                    result = self.run_action(repo, event_name=event_name, event=event, mode="diff", strict=strict)
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn("base revision", result.stdout + result.stderr)

    def test_action_legacy_diff_is_opt_in_and_requires_head_parent(self):
        repo = self.make_repo()
        root_only = self.run_action(repo, strict="false")
        self.assertEqual(root_only.returncode, 2, root_only.stdout + root_only.stderr)
        self.assertIn("head parent is unavailable", root_only.stderr)
        self.commit_files(repo, {"src/app/signup/page.tsx": ""})
        result = self.run_action(repo, strict="false")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("AUTH-R1", result.stdout)
        invalid = self.run_action(repo, strict="typo")
        self.assertEqual(invalid.returncode, 2, invalid.stdout + invalid.stderr)
        self.assertIn("strict must be true or false", invalid.stdout)

    def test_action_full_with_no_base_audits_entire_selected_head(self):
        repo = self.make_repo()
        _, clean = self.commit_files(repo, {"src/entry.tsx": "<SylphxAuth />\n"})
        self.commit_files(repo, {"src/app/signup/page.tsx": ""})
        _, violating = self.commit_files(repo, {"README.md": "# Unrelated change\n"})
        events = [(name, {}) for name in ("push", "workflow_dispatch", "schedule", "pull_request", "merge_group")]
        events += [("push", {"before": "0" * 40}), ("push", {"before": clean})]
        for event_name, event in events:
            for strict in ("true", "false"):
                for head, code, text in ((clean, 0, "full audit"), (violating, 1, "AUTH-R1"),
                                         ("missing-head", 2, "head revision")):
                    with self.subTest(event_name=event_name, event=event, strict=strict, head=head):
                        result = self.run_action(repo, event_name=event_name, event=event,
                                                 mode="full", strict=strict, head=head)
                        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                        self.assertIn(text, result.stdout + result.stderr)

    def test_action_strict_full_without_base_does_not_trust_head_only_waivers(self):
        repo = self.make_repo()
        baseline = ".github/auth-departures.txt"
        self.commit_files(repo, {
            "src/app/signup/page.tsx": "",
            "src/legacy/phone.ts": "// auth-surface-guard: allow(legacy-continuity)\nsendSmsOtp(phone)\n",
        })
        records = self.run_guard(repo, "--mode", "full", "--print-baseline")
        self.assertEqual(records.returncode, 0, records.stderr)
        self.commit_files(repo, {baseline: records.stdout})
        result = self.run_action(repo, event_name="push", mode="full", baseline=baseline)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("baseline finding is not inherited: auth-surface AUTH-R1", result.stdout)
        self.assertIn("[AUTH-R3]", result.stdout)

    def test_action_full_audits_inherited_findings_and_accepts_baseline(self):
        repo = self.make_repo()
        _, base = self.commit_files(repo, {"src/app/signup/page.tsx": ""})
        records = self.run_guard(repo, "--mode", "full", "--print-baseline")
        self.assertEqual(records.returncode, 0, records.stderr)
        path = ".github/auth-departures.txt"
        self.commit_files(repo, {path: records.stdout})
        event = {"merge_group": {"base_sha": base}}
        diff = self.run_action(repo, event_name="merge_group", event=event)
        self.assertEqual(diff.returncode, 0, diff.stdout + diff.stderr)
        full = self.run_action(repo, event_name="merge_group", event=event, mode="full")
        self.assertEqual(full.returncode, 1, full.stdout + full.stderr)
        self.assertIn("AUTH-R1", full.stdout)
        for inputs in ({"base-ref": base}, {"strict": "false"}):
            with self.subTest(inputs=inputs):
                result = self.run_action(repo, mode="full", baseline=path, **inputs)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

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

    def test_precise_masked_integration_credentials_in_all_modes(self):
        repo = self.make_repo()
        base, head = self.commit_files(repo, {
            'src/dashboard/bots/connect.tsx': '''<Input
  label="Bot token"
  type="password"
  autocomplete="off"
  value={props.botToken}
  onInput={(e) => props.onBotTokenChange(e.target.value)}
  placeholder="synthetic-bot-value"
  required
  hint={props.hint}
/>
<Input
  label="Signing secret"
  type="password"
  autocomplete="off"
  value={props.signingSecret}
  onInput={(e) => props.onSigningSecretChange(e.target.value)}
  required
/>
<Input
  label="Bot token"
  type="password"
  autocomplete="off"
  value={props.botToken}
  onInput={(e) => props.onBotTokenChange(e.target.value)}
  placeholder="synthetic-provider-value"
  required
/>
''',
            'src/dashboard/wizard.tsx': '''<Input
  label="Bot token"
  type="password"
  autocomplete="off"
  value={props.draft.botToken}
  onInput={(e) => props.onChange({ botToken: e.target.value })}
  placeholder="synthetic-bot-value"
  hint={props.hint}
/>
''',
            'src/dashboard/integrations/provider.jsx': '''
<input type={'password'} aria-label="API key" name="providerApiKey" autoComplete={'off'} />
<input type="password" aria-label="Webhook secret" name="webhook_secret" />
<input type="password" aria-label="Provider access token" id="provider-access-token" />
<input type="password" aria-label="Client secret" id="clientSecret" />
<input type="password" aria-label="Provider token" id="providerToken" />
''',
            'src/dashboard/integrations/editor.vue': '<input type="password" aria-label="API key" id="api-key">',
            'src/dashboard/integrations/editor.svelte': '<input type="password" aria-label="API key" bind:value={apiKey}>',
        })
        for mode in (('--mode', 'full'), ('--strict',), ()):
            with self.subTest(mode=mode):
                result = self.run_guard(repo, *mode, '--base', base, '--head', head)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('0 violation(s)', result.stdout)

    def test_account_or_ambiguous_password_inputs_in_all_modes(self):
        repo = self.make_repo()
        masked = '<Input type="password" label="Bot token" name="botToken" />'
        paths = {path: masked for path in (
            'src/app/(auth)/enter/page.tsx', 'src/app/login/page.tsx', 'src/app/sign-in/page.jsx',
            'src/app/sign-up/page.tsx', 'src/pages/register/index.tsx', 'src/pages/forgot/index.tsx',
            'src/app/settings/reset/page.tsx', 'src/components/ChangePassword.tsx',
        )}
        paths.update({
            'src/components/Dialog.tsx': f'const LoginForm = () => ({masked});',
            'src/components/Modal.tsx': f'<SignInForm>{masked}</SignInForm>',
            'src/settings/Profile.tsx': f'<form onSubmit={{updateAccount}}>{masked}</form>',
            'src/settings/page.tsx': '<input name="confirmPassword" type="password" />',
            'src/dashboard/plain.vue': '<input type=password>',
            'src/dashboard/multiline.svelte': '<input\n  type="password"\n>',
        })
        for i, attrs in enumerate((
            '', 'label="Secret" name="token"', 'label="Bot token"', 'name="botToken"',
            'label="Bot token" name="apiKey"', 'label="Bot token" name="confirmPassword"',
            'label="Bot token" name="botToken" id="passwd"',
            'label="Bot token" name="botToken" onInput={setPassphrase}',
            'label="Bot token" name="botToken" autoComplete="current-password"',
            'label="Bot token" name="botToken" autocomplete={"new-password"}',
            'label="Bot token" name="botToken" autocomplete={mode}',
            'label="Bot token" name="botToken" placeholder="Account password"',
            'label="Bot token" name="botToken" value={state[field]}',
            'label="Bot token" name="botToken" value={authState.botToken}',
            'label="Bot token" name="botToken" { ...props }',
            'label="Bot token" name="botToken" label={displayLabel}',
            'label="Bot token" name="botToken" name="password"',
            'label={displayLabel} name="botToken"',
            'label="Access token" name="accessToken"',
            'label="Bot token" name="botToken" onInput={() => login(input)}',
            'label="Bot token" name="botToken" hint={`A template`}',
            'label="Bot token" name="botToken" :autocomplete="mode"',
            'label="Bot token" name="botToken" data-alias="new&#45;password"',
        )):
            paths[f'src/dashboard/field{i}.tsx'] = f'<Input type="password" {attrs} />'
        paths['src/dashboard/broken.jsx'] = '<Input type="password" name="botToken" label="Bot token"'
        paths['src/dashboard/later-type.tsx'] = '''<Input
  label="Bot token > add integration"
  onInput={(e) => e.target.value}
  type="password"
  name="password"
/>'''
        base, head = self.commit_files(repo, paths)
        for mode in (('--mode', 'full'), ('--strict',), ()):
            with self.subTest(mode=mode):
                result = self.run_guard(repo, *mode, '--base', base, '--head', head)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(result.stdout.count('[AUTH-R2]'), len(paths), result.stdout)
                for path in paths:
                    self.assertIn(f'{path}:1: [AUTH-R2]', result.stdout)

    def test_diff_reports_a_field_when_only_its_credential_metadata_changes(self):
        repo = self.make_repo()
        path = 'src/dashboard/integrations/key.tsx'
        masked = '<Input\n  type="password"\n  label="API key"\n  value={props.apiKey}\n/>\n'
        _, base = self.commit_files(repo, {path: masked})
        _, head = self.commit_files(repo, {path: masked.replace('props.apiKey', 'props.password')})
        for mode in (('--strict',), ()):
            result = self.run_guard(repo, *mode, '--base', base, '--head', head)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn(f'{path}:1: [AUTH-R2]', result.stdout)

    def test_existing_password_baseline_digest_is_unchanged(self):
        text = 'const x = <input\n  id="confirmPassword"\n  type={"password"} />\n'
        finding, = guard.password_findings('src/settings/Profile.tsx', text)
        self.assertEqual(finding.identity, '<input\n  id="confirmPassword"\n  type={"password"}')

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
            (('--strict', '--base', 'missing'), 'base revision must be available'),
            (('--strict',), 'explicit base'),
            (('--mode', 'full', '--base', 'missing'), 'base revision must be available'),
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
