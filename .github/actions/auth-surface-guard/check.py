#!/usr/bin/env python3
"""Enforce the group-wide Single Source of Truth (SSOT) Passwordless Auth standard.

Rules enforced on added lines in product repositories:
1. AUTH-R1 (handwritten-auth-route):
   Product web apps must not add standalone `/signup`, `/register`,
   `/forgot-password`, or `/reset-password` route files. Use the single
   `Continue` entry via `<SylphxAuth />` (`@sylphx/apps`) or the hosted
   Sylphx Auth portal.
2. AUTH-R2 (handwritten-password-input):
   Product web UI files (`*.tsx`, `*.jsx`, `*.vue`, `*.svelte`) must not add
   handwritten `<input type="password">` fields. Use passwordless Email 6-digit
   OTP + Passkey (`autocomplete="username webauthn"`) + Social OAuth.
3. AUTH-R3 (public-web-sms-otp):
   Product web UI and API callers must not add public web SMS OTP sign-in flows
   (`sendSmsOtp`, `verifySmsOtp`, `signInWithPhone`, `signInWithSms`, or SMS OTP
   verification inputs). Legacy live-title phone login continuity is permitted
   only when explicitly annotated with
   `# auth-surface-guard: allow(legacy-continuity)` or
   `// auth-surface-guard: allow(legacy-continuity)`.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
import sys

ALLOW_MARKER = "auth-surface-guard: allow(legacy-continuity)"

# Shared auth provider and SDK implementation paths that legitimately implement
# protocol primitives or legacy compatibility adapters.
SHARED_AUTH_PREFIXES = (
    "services/auth/",
    "packages/apps/",
    "packages/core/",
    "packages/ui/",
    "crates/cubeage-kit/",
    ".github/",
)

TEST_PATH_RE = re.compile(
    r"(?:^|/)(?:tests?|__tests__|fixtures?|e2e)/|"
    r"\.(?:test|spec)\.[A-Za-z0-9]+$"
)

STANDALONE_AUTH_ROUTE_RE = re.compile(
    r"(?:^|/)(?:app|pages|routes|src/app|src/pages|src/routes)/"
    r"(?:.*?\b)(?:signup|sign-up|register|forgot-password|reset-password)"
    r"(?:/(?:\[\[?\.{0,3}[^\]]+\]?\]/)*(?:page|route|index))?\.(?:tsx|ts|jsx|js)$",
    re.IGNORECASE,
)

UI_FILE_RE = re.compile(r"\.(?:tsx|jsx|vue|svelte)$", re.IGNORECASE)
SOURCE_FILE_RE = re.compile(r"\.(?:tsx|ts|jsx|js|vue|svelte|rs)$", re.IGNORECASE)

PASSWORD_INPUT_RE = re.compile(
    r"<input\b[^>]*\btype\s*=\s*(?:['\"]password['\"]|\{\s*['\"]password['\"]\s*\})",
    re.IGNORECASE,
)

PUBLIC_SMS_OTP_RE = re.compile(
    r"\b(?:sendSmsOtp|verifySmsOtp|signInWithSms|signInWithPhone|requestSmsOtp|smsOtpLogin)\b",
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    message: str
    remediation: str

    def format_annotation(self) -> str:
        return (
            f"::error file={self.path},line={self.line},title={self.rule}::"
            f"{self.path}:{self.line}: [{self.rule}] {self.message} "
            f"Fix: {self.remediation}"
        )


def git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(
        ["git", "--literal-pathspecs", "-C", str(root), *args],
        stderr=subprocess.PIPE,
    )


def commit_exists(root: Path, rev: str) -> bool:
    if not rev:
        return False
    try:
        git(root, "rev-parse", "--verify", f"{rev}^{{commit}}")
        return True
    except subprocess.CalledProcessError:
        return False


def is_exempt_path(path: str) -> bool:
    norm = path.replace("\\", "/").lstrip("./")
    if any(norm.startswith(prefix) for prefix in SHARED_AUTH_PREFIXES):
        return True
    if TEST_PATH_RE.search(norm):
        return True
    return False


def added_lines(root: Path, base: str, head: str):
    """Yield (path, line_number, line_text, is_new_file) across the diff."""
    raw_entries = git(
        root,
        "diff",
        "--name-status",
        "--no-renames",
        "--diff-filter=AM",
        "-z",
        base,
        head,
    ).split(b"\0")
    it = iter(raw_entries)
    for status_bytes in it:
        if not status_bytes:
            continue
        status = status_bytes.decode("utf-8", errors="replace")
        try:
            path = next(it).decode("utf-8", errors="replace")
        except StopIteration:
            break
        is_new_file = status.startswith("A")
        content = git(root, "show", f"{head}:{path}")
        if b"\0" in content:
            continue
        full_text = content.decode("utf-8", errors="replace")
        if ALLOW_MARKER in full_text:
            continue
        diff = git(
            root,
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--text",
            "--no-renames",
            "--unified=0",
            base,
            head,
            "--",
            path,
        ).decode("utf-8", errors="replace")
        line_no: int | None = None
        yielded_for_file = False
        for raw_line in diff.split("\n"):
            line = raw_line.removesuffix("\r")
            if line.startswith("@@"):
                match = re.search(r"\+(\d+)", line)
                line_no = int(match.group(1)) if match else 1
            elif line_no is not None and line.startswith("+") and not line.startswith("+++"):
                yielded_for_file = True
                yield path, line_no, line[1:], is_new_file
                line_no += 1
            elif line_no is not None and line.startswith(" "):
                line_no += 1
        if is_new_file and not yielded_for_file:
            yield path, 1, "", True


def scan_diff(root: Path, base: str, head: str) -> list[Finding]:
    findings: list[Finding] = []
    reported_routes: set[str] = set()

    for path, line_no, text, is_new_file in added_lines(root, base, head):
        if is_exempt_path(path):
            continue
        if ALLOW_MARKER in text:
            continue

        if is_new_file and path not in reported_routes and STANDALONE_AUTH_ROUTE_RE.search(path):
            reported_routes.add(path)
            findings.append(
                Finding(
                    path=path,
                    line=1,
                    rule="AUTH-R1",
                    message=(
                        f"Standalone auth route '{path}' is prohibited by the group SSOT Auth standard."
                    ),
                    remediation=(
                        "Remove standalone /signup, /register, or /forgot-password routes and route all "
                        "sign-in/sign-up through the single Continue entry (<SylphxAuth /> from @sylphx/apps "
                        "or the hosted Sylphx Auth portal)."
                    ),
                )
            )

        if UI_FILE_RE.search(path) and PASSWORD_INPUT_RE.search(text):
            findings.append(
                Finding(
                    path=path,
                    line=line_no,
                    rule="AUTH-R2",
                    message="Handwritten <input type=\"password\"> in product web UI violates passwordless SSOT Auth.",
                    remediation=(
                        "Replace handwritten password inputs with <SylphxAuth /> (@sylphx/apps) using "
                        "Email 6-digit OTP + Passkey (autocomplete=\"username webauthn\") + Social OAuth."
                    ),
                )
            )

        if SOURCE_FILE_RE.search(path) and PUBLIC_SMS_OTP_RE.search(text):
            findings.append(
                Finding(
                    path=path,
                    line=line_no,
                    rule="AUTH-R3",
                    message="Public web SMS OTP sign-in call violates the Email-only OTP + Passkey + Social OAuth standard.",
                    remediation=(
                        "Use Email 6-digit OTP + Passkey + Social OAuth via Sylphx Auth (no public web SMS OTP, "
                        "to prevent SMS pumping and split phone/email accounts; annotate legacy game account "
                        "recovery with '// auth-surface-guard: allow(legacy-continuity)')."
                    ),
                )
            )

    return findings


def resolve_base(root: Path, base_arg: str, head: str) -> str | None:
    if base_arg and commit_exists(root, base_arg):
        return base_arg
    if commit_exists(root, f"{head}^1"):
        return f"{head}^1"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--base", default="")
    parser.add_argument("--head", default="HEAD")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    if not commit_exists(root, args.head):
        print(f"auth-surface-guard: head revision '{args.head}' is not available", file=sys.stderr)
        return 2

    base = resolve_base(root, args.base.strip(), args.head)
    if base is None:
        print("auth-surface-guard: single-commit history without parent; 0 findings")
        return 0

    findings = scan_diff(root, base, args.head)
    for finding in findings:
        print(finding.format_annotation())
    print(f"auth-surface-guard: checked {base[:12]}..{args.head[:12]}, {len(findings)} violation(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
