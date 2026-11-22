#!/usr/bin/env python3
"""Enforce the group-wide Single Source of Truth (SSOT) Passwordless Auth standard.

Rules enforced in product repositories:
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
from collections import Counter
from dataclasses import dataclass
import hashlib
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from urllib.parse import quote

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
    r"<input\b[^>]*?\btype\s*=\s*(?:['\"]password['\"]|\{\s*['\"]password['\"]\s*\})",
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
    identity: str = ""

    def format_annotation(self) -> str:
        def escape(value, property=False):
            value = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
            return value.replace(",", "%2C").replace(":", "%3A") if property else value

        return (
            f"::error file={escape(self.path, property=True)},line={self.line},title={self.rule}::"
            f"{escape(self.path)}:{self.line}: [{self.rule}] {escape(self.message)} "
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
        git(root, "rev-parse", "--verify", "--end-of-options", f"{rev}^{{commit}}")
        return True
    except subprocess.CalledProcessError:
        return False


def is_exempt_path(path: str) -> bool:
    norm = path.replace("\\", "/").removeprefix("./")
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


def scan_line(path: str, line_no: int, text: str, *, route: bool = False, password: bool = True) -> list[Finding]:
    findings = []
    if route and STANDALONE_AUTH_ROUTE_RE.search(path):
        findings.append(
            Finding(
                path=path,
                line=1,
                rule="AUTH-R1",
                message=(
                    f"Standalone auth route '{path}' is prohibited by the group SSOT Auth standard."
                ),
                remediation=(
                    "Remove standalone /signup, /register, /forgot-password, or /reset-password routes and route all "
                    "sign-in/sign-up through the single Continue entry (<SylphxAuth /> from @sylphx/apps "
                    "or the hosted Sylphx Auth portal)."
                ),
            )
        )

    if password and UI_FILE_RE.search(path) and PASSWORD_INPUT_RE.search(text):
        findings.append(
            Finding(
                path=path,
                line=line_no,
                rule="AUTH-R2",
                identity=text,
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
                identity=text,
                message="Public web SMS OTP sign-in call violates the Email-only OTP + Passkey + Social OAuth standard.",
                remediation=(
                    "Use Email 6-digit OTP + Passkey + Social OAuth via Sylphx Auth (no public web SMS OTP, "
                    "to prevent SMS pumping and split phone/email accounts; annotate legacy game account "
                    "recovery with '// auth-surface-guard: allow(legacy-continuity)')."
                ),
            )
        )

    return findings


def scan_diff(root: Path, base: str, head: str) -> list[Finding]:
    findings: list[Finding] = []
    reported_routes: set[str] = set()
    for path, line_no, text, is_new_file in added_lines(root, base, head):
        if is_exempt_path(path) or ALLOW_MARKER in text:
            continue
        findings.extend(scan_line(path, line_no, text, route=is_new_file and path not in reported_routes))
        reported_routes.add(path)
    return findings



# Exact records follow the stack force-push ratchet: escaped path, digest,
# occurrence. Moving a line is harmless; copying or editing one is new debt.
RECORD_RE = re.compile(r"auth-surface AUTH-R[123] [^\s#]+ [a-f0-9]{64} [1-9][0-9]*")
CONTINUITY_RE = re.compile(r"\s*(?://|#)\s*" + re.escape(ALLOW_MARKER) + r"\s*")
REGULAR = {b"100644", b"100755"}


def tree_entries(root: Path, rev: str) -> dict[str, tuple[bytes, bytes]]:
    # The revision-bound equivalent of ls-files: never trust the mutable worktree
    # or index in place of the selected head.
    entries = {}
    for row in filter(None, git(root, "ls-tree", "-r", "-z", rev).split(b"\0")):
        header, path = row.split(b"\t", 1)
        mode, _, oid = header.split()
        entries[path.decode("utf-8", "surrogateescape")] = mode, oid
    return entries


def read_blobs(root: Path, entries):
    # Object IDs (not user paths) are safe to feed to the newline-framed batch.
    with subprocess.Popen(["git", "-C", str(root), "cat-file", "--batch"],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE) as batch:
        for path, (mode, oid) in entries:
            if mode not in REGULAR:
                raise ValueError(f"expected a tracked regular file: {path!r}")
            batch.stdin.write(oid + b"\n")
            batch.stdin.flush()
            header = batch.stdout.readline().split()
            if len(header) != 3 or header[:2] != [oid, b"blob"]:
                raise ValueError(f"unreadable tracked file: {path!r}")
            size = int(header[2])
            raw = batch.stdout.read(size)
            if len(raw) != size or batch.stdout.read(1) != b"\n":
                raise ValueError(f"incomplete tracked file: {path!r}")
            yield path, raw


def scan_tree(root: Path, entries, paths=None):
    candidates = ((path, entry) for path, entry in entries.items()
                  if (paths is None or path in paths) and not is_exempt_path(path)
                  and SOURCE_FILE_RE.search(path))
    records = {}
    continuity = set()
    for path, raw in read_blobs(root, candidates):
        if b"\0" in raw:
            raise ValueError(f"unreadable auth source (binary): {path!r}")
        text = raw.decode("utf-8", "surrogateescape")
        lines = text.splitlines()
        if any(CONTINUITY_RE.fullmatch(line) for line in lines):
            continuity.add(path)
        findings = scan_line(path, 1, "", route=True)
        for line, content in enumerate(lines, 1):
            findings.extend(scan_line(path, line, content, password=False))
        if UI_FILE_RE.search(path):
            # Unlike the legacy added-line recognizer, the committed-tree
            # audit sees a split opening tag and counts each matching input.
            line = 1
            offset = 0
            for match in PASSWORD_INPUT_RE.finditer(text):
                line += text.count("\n", offset, match.start())
                offset = match.start()
                findings.extend(f for f in scan_line(path, line, match[0]) if f.rule == "AUTH-R2")
        occurrences = Counter()
        escaped = quote(path, safe="/._-", errors="surrogateescape")
        for finding in findings:
            digest = hashlib.sha256(finding.identity.encode("utf-8", "surrogateescape")).hexdigest()
            key = f"auth-surface {finding.rule} {escaped} {digest}"
            occurrences[key] += 1
            records[f"{key} {occurrences[key]}"] = finding
    return records, continuity


def read_baseline(root: Path, entries, path: str, *, required: bool) -> set[str] | None:
    if path not in entries:
        if required:
            raise ValueError(f"baseline is not tracked at head: {path!r}")
        return None
    (_, raw), = read_blobs(root, [(path, entries[path])])
    records = set()
    for number, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if not RECORD_RE.fullmatch(line) or line in records:
            raise ValueError(f"invalid or duplicate baseline entry: {path}:{number}")
        records.add(line)
    return records


def check_tree(root: Path, args, base: str) -> int:
    head = tree_entries(root, args.head)
    before = tree_entries(root, base) if base else {}
    changed = {path for path in head if before.get(path) != head[path]}
    found, continuity = scan_tree(root, head, changed if args.mode == "diff" else None)
    inherited, base_continuity = scan_tree(root, before, changed) if base else ({}, set())
    # Unchanged paths are inherited by blob identity. On changed paths a
    # continuity annotation carries forward only exact, already annotated debt.
    prior = {key for key, finding in found.items() if finding.path not in changed} | inherited.keys()
    waived = {key for key, finding in found.items() if finding.path in continuity
              and (not base or finding.path not in changed
                   or finding.path in base_continuity and key in prior)}
    if args.print_baseline:
        print("# Auth surface departures; this list may only shrink.")
        for key in sorted(found.keys() - waived):
            print(key)
        return 0
    found = {key: finding for key, finding in found.items() if key not in waived}
    errors = []
    allowed = set()
    if args.baseline:
        path = PurePosixPath(args.baseline)
        if path.is_absolute() or ".." in path.parts or str(path) == ".":
            raise ValueError("baseline must be a repository-relative file path")
        allowed = read_baseline(root, head, str(path), required=True)
        previous = read_baseline(root, before, str(path), required=False) if base else None
        if previous is not None:
            errors += ["baseline grew: " + key for key in sorted(allowed - previous)]
        if base:
            errors += ["baseline finding is not inherited: " + key for key in sorted(allowed - prior)]
        errors += ["stale baseline entry: " + key for key in sorted(allowed - found.keys())]
    if args.mode == "diff":
        # Removing the comment exposes the debt, including unchanged lines.
        allowed = {key for key in prior if key in found and found[key].path not in base_continuity}
    violations = found.keys() - allowed
    for key, finding in found.items():
        if key in violations:
            print(finding.format_annotation())
    for error in errors:
        print("auth-surface-guard: " + error)
    print(f"auth-surface-guard: {args.mode} audit, {len(violations)} violation(s), {len(errors)} baseline error(s)")
    return int(bool(violations or errors))


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
    parser.add_argument("--mode", choices=("diff", "full"), default="diff")
    parser.add_argument("--baseline", default="", help="tracked departures file (full mode only)")
    parser.add_argument("--print-baseline", action="store_true", help="emit candidate records (full mode only)")
    parser.add_argument("--strict", action="store_true", help="require a valid explicit base")
    args = parser.parse_args(argv)
    if (args.baseline or args.print_baseline) and args.mode != "full":
        parser.error("--baseline and --print-baseline require --mode full")
    if args.print_baseline and (args.baseline or args.base or args.strict):
        parser.error("generate candidate records from one revision, without --base or --baseline")

    root = Path(args.root).resolve()
    if not commit_exists(root, args.head):
        print(f"auth-surface-guard: head revision '{args.head}' is not available", file=sys.stderr)
        return 2

    if args.mode == "full" or args.strict:
        base = args.base.strip()
        if (base and not commit_exists(root, base)) or (args.strict and not base):
            print("auth-surface-guard: explicit base revision is required and must be available", file=sys.stderr)
            return 2
        try:
            # Bind revision arguments before passing them to other Git commands.
            args.head = git(root, "rev-parse", "--verify", "--end-of-options", f"{args.head}^{{commit}}").decode().strip()
            if base:
                base = git(root, "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}").decode().strip()
            return check_tree(root, args, base)
        except (ValueError, OSError, subprocess.CalledProcessError) as error:
            print(f"auth-surface-guard: audit could not complete: {error}", file=sys.stderr)
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
