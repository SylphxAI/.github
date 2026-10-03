#!/usr/bin/env python3
"""Move a repository's Keel pin to a Keel tag and open or update the repin pull request.

Two steps, run from the repository root:

  keel_repin.py run --tag keel-verified-2026-10-03-1|latest [--check-command CMD] [--report FILE]
      Resolve the tag to its commit, rewrite every Keel pin, refresh Cargo.lock for the
      Keel crates, run the check command, and write a report. Never fails because the
      build is broken: the break goes in the report so the pull request shows it.

  keel_repin.py pr --report FILE [--dry-run]
      Branch, commit, push and open (or report) the pull request. A broken build opens it
      as a draft with the error summary. It never merges or enables auto-merge.

What counts as a pin: a `rev = "<40 hex>"` or `tag = "keel-..."` on a Cargo.toml line that
names the Keel git repository, every `deps/keel.rev`, and the old full or short commit
anywhere else in tracked text files (Dockerfiles, cargo config, CI, docs). A repository
that needs more (vendored sources, a patch block) keeps `tools/repin_keel.sh <tag>`; when
it exists it replaces the rewrite and the lock refresh.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

TAG_RE = re.compile(r"keel-(verified|weekly)-(\d{4}-\d{2}-\d{2})(?:-(\d+))?")
SHA_RE = re.compile(r"[0-9a-f]{40}")
KEEL_URL = re.compile(r"SylphxAI/keel(?:\.git)?(?![\w-])")
REV_RE = re.compile(r'rev\s*=\s*"([0-9a-f]{40})"')
TAG_PIN_RE = re.compile(r'(tag\s*=\s*")keel-(?:verified|weekly)-[0-9-]+(")')
TAG_COMMENT_RE = re.compile(r"^(# Keel (?:weekly |verified )?tag: *)\S+", re.MULTILINE)
DEFAULT_REMOTE = "https://github.com/SylphxAI/keel"
HOOK = "tools/repin_keel.sh"
BRANCH_PREFIX = "chore/keel-repin-"
SKIP_DIRS = ("vendor/", "target/", "node_modules/")
SKIP_NAMES = ("Cargo.lock", "CHANGELOG.md")
MAX_TEXT_BYTES = 2_000_000
LOG_TAIL_LINES = 40
LOG_TAIL_CHARS = 5000


class Refused(Exception):
    """A condition the caller must see as a failed run."""


def run(cmd, cwd=None, check=True, env=None, capture=True):
    try:
        p = subprocess.run(cmd, cwd=cwd, env=env, text=True, capture_output=capture)
    except FileNotFoundError as e:
        p = subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: command not found ({e})")
    if check and p.returncode != 0:
        raise Refused(f"{' '.join(cmd[:3])} failed ({p.returncode}): {(p.stderr or p.stdout or '').strip()[-400:]}")
    return p


def tag_key(tag):
    """Sortable key: date, then weekly before verified, then N."""
    m = TAG_RE.fullmatch(tag)
    if not m:
        return None
    kind, date, n = m.groups()
    return (date, 0 if kind == "weekly" else 1, int(n or 0))


def newest_verified_tag(remote):
    """The newest keel-verified-* tag at the remote (the daily catch-up has no tag to name)."""
    out = run(["git", "ls-remote", "--tags", remote, "refs/tags/keel-verified-*"]).stdout
    tags = [line.split()[1][len("refs/tags/"):].removesuffix("^{}") for line in out.splitlines() if line.strip()]
    tags = [t for t in set(tags) if tag_key(t) and t.startswith("keel-verified-")]
    if not tags:
        raise Refused(f"no keel-verified-* tag exists at {remote}")
    return max(tags, key=tag_key)


def resolve_tag(tag, remote):
    """The commit a Keel tag points at (the peeled commit of an annotated tag)."""
    if tag_key(tag) is None:
        raise Refused(f"{tag!r} is not a keel-verified-YYYY-MM-DD-N or keel-weekly-YYYY-MM-DD tag")
    out = run(["git", "ls-remote", "--tags", remote, f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"]).stdout
    refs = {line.split()[1]: line.split()[0] for line in out.splitlines() if line.strip()}
    sha = refs.get(f"refs/tags/{tag}^{{}}") or refs.get(f"refs/tags/{tag}")
    if not sha or not SHA_RE.fullmatch(sha):
        raise Refused(f"Keel tag {tag} does not exist at {remote}")
    return sha


def tracked_files(root):
    out = run(["git", "ls-files", "-z"], cwd=root).stdout
    return [p for p in out.split("\0") if p]


def pin_files(files):
    cargo = [f for f in files if f.endswith("Cargo.toml") and not f.startswith(SKIP_DIRS) and "/vendor/" not in f]
    revs = [f for f in files if (f == "deps/keel.rev" or f.endswith("/deps/keel.rev")) and "/vendor/" not in f]
    return cargo, revs


def find_pins(root):
    """Every Keel commit a repository pins today (full shas)."""
    files = tracked_files(root)
    cargo, revs = pin_files(files)
    found = []
    for f in cargo:
        for line in (Path(root) / f).read_text(errors="replace").splitlines():
            if KEEL_URL.search(line):
                found += REV_RE.findall(line)
    for f in revs:
        text = (Path(root) / f).read_text(errors="replace").strip()
        if SHA_RE.fullmatch(text):
            found.append(text)
    return sorted(set(found))


def has_tag_pins(root):
    cargo, _ = pin_files(tracked_files(root))
    return any(
        KEEL_URL.search(line) and TAG_PIN_RE.search(line)
        for f in cargo
        for line in (Path(root) / f).read_text(errors="replace").splitlines()
    )


def is_text(path):
    try:
        if path.stat().st_size > MAX_TEXT_BYTES:
            return False
        return b"\0" not in path.read_bytes()
    except OSError:
        return False


def rewrite(root, old_shas, new_sha, tag):
    """Move every pin to new_sha and the tag; returns the changed file list."""
    root = Path(root)
    prefix = re.compile("|".join(f"(?<![0-9a-f]){re.escape(o[:n])}(?![0-9a-f])" for o in old_shas for n in (12, 8, 7)) or "(?!)")
    changed = []
    for f in tracked_files(root):
        if f.startswith(SKIP_DIRS) or "/vendor/" in f or Path(f).name in SKIP_NAMES:
            continue
        p = root / f
        if not p.is_file() or p.is_symlink() or not is_text(p):
            continue
        text = p.read_text(errors="surrogateescape")
        new = text
        for old in old_shas:
            new = new.replace(old, new_sha)
        new = prefix.sub(lambda m: new_sha[: len(m.group(0))], new)
        if f.endswith("Cargo.toml"):
            new = "\n".join(
                TAG_PIN_RE.sub(lambda m: f"{m.group(1)}{tag}{m.group(2)}", line) if KEEL_URL.search(line) else line
                for line in new.split("\n")
            )
            new = TAG_COMMENT_RE.sub(lambda m: m.group(1) + tag, new)
        if new != text:
            p.write_text(new, errors="surrogateescape")
            changed.append(f)
    return changed


def lock_packages(lock_text):
    """Names of the packages Cargo.lock takes from the Keel git source."""
    names, name = [], None
    for line in lock_text.splitlines():
        if line.startswith("name = "):
            name = line.split('"')[1]
        elif line.startswith("source = ") and KEEL_URL.search(line) and name:
            names.append(name)
    return sorted(set(names))


def refresh_locks(root, log):
    """cargo update for the Keel crates of every tracked Cargo.lock; a failure is returned, not raised."""
    root = Path(root)
    for f in tracked_files(root):
        if Path(f).name != "Cargo.lock" or f.startswith(SKIP_DIRS) or "/vendor/" in f:
            continue
        manifest = (root / f).with_name("Cargo.toml")
        pkgs = lock_packages((root / f).read_text(errors="replace"))
        if not pkgs or not manifest.exists():
            continue
        cmd = ["cargo", "update", "--manifest-path", str(manifest)]
        for p in pkgs:
            cmd += ["-p", p]
        proc = run(cmd, cwd=root, check=False)
        log.append(f"$ {' '.join(cmd)}\n{proc.stdout}{proc.stderr}")
        if proc.returncode != 0:
            return "lock"
    return None


def remote_branch_exists(root, branch):
    return run(["git", "ls-remote", "--exit-code", "--heads", "origin", branch], cwd=root, check=False).returncode == 0


def cmd_run(args):
    root = Path(args.root).resolve()
    if args.tag in ("", "latest"):
        args.tag = newest_verified_tag(args.remote)
    sha = resolve_tag(args.tag, args.remote)
    branch = BRANCH_PREFIX + args.tag
    report = {"tag": args.tag, "sha": sha, "branch": branch, "status": "unchanged", "stage": "", "changed": [], "old": [], "log": ""}

    def save():
        Path(args.report).write_text(json.dumps(report, indent=2))

    old = find_pins(root)
    report["old"] = old
    hook = (root / HOOK).exists()
    if not old and not hook and not has_tag_pins(root):
        raise Refused("no Keel pin found in any tracked Cargo.toml or deps/keel.rev; nothing to repin")
    if old == [sha]:
        print(f"::notice::already pinned to {args.tag} ({sha[:9]}); nothing to do")
        save()
        return 0
    if not args.ignore_branch and remote_branch_exists(root, branch):
        report["status"] = "branch-exists"
        print(f"::notice::{branch} already exists; leaving it as is. Close the pull request and delete the branch to rebuild it")
        save()
        return 0

    log = []
    stage = None
    if hook:
        proc = run(["bash", HOOK, args.tag], cwd=root, check=False)
        log.append(f"$ bash {HOOK} {args.tag}\n{proc.stdout}{proc.stderr}")
        if proc.returncode != 0:
            stage = "hook"
    else:
        rewrite(root, old, sha, args.tag)
        stage = refresh_locks(root, log)
    if stage is None and args.check_command:
        proc = run(["bash", "-c", args.check_command], cwd=Path(root, args.check_dir), check=False)
        log.append(f"$ {args.check_command}\n{proc.stdout}{proc.stderr}")
        if proc.returncode != 0:
            stage = "check"
    changed = run(["git", "status", "--porcelain", "-z"], cwd=root).stdout.split("\0")
    report["changed"] = sorted(e[3:] for e in changed if len(e) > 3)
    if any(f.startswith("target/") or "/target/" in f for f in report["changed"]):
        raise Refused("build output (target/) is not ignored by git in this repository; add it to .gitignore")
    report["status"] = "ok" if stage is None else "failed"
    report["stage"] = stage or ""
    report["log"] = "\n".join(log)
    save()
    if not report["changed"]:
        report["status"] = "unchanged"
        save()
    print(f"repin {args.tag} -> {sha[:9]}: {report['status']} ({len(report['changed'])} files changed)")
    return 0


def log_tail(log):
    tail = "\n".join(log.splitlines()[-LOG_TAIL_LINES:])[-LOG_TAIL_CHARS:]
    return tail.replace("```", "'''")


def pr_text(report, writer):
    tag, sha = report["tag"], report["sha"]
    old = report["old"]
    title = f"chore(keel): repin Keel to {tag} ({sha[:9]})"
    lines = [f"Moves the Keel pin to `{tag}`, commit `{sha}`."]
    if old:
        lines.append("")
        lines.append("Old pin: " + ", ".join(f"`{o}`" for o in old))
        lines.append(f"Keel changes: https://github.com/SylphxAI/keel/compare/{old[0]}...{sha} (private repository)")
    if report["changed"]:
        lines += ["", "Files changed:", *[f"- `{f}`" for f in report["changed"][:60]]]
    if report["status"] == "failed":
        what = {"hook": "the repository's repin script", "lock": "the Cargo.lock refresh", "check": "the build check"}[report["stage"]]
        lines += [
            "",
            f"**Draft: {what} fails on this Keel tag.** The pull request is open as a draft so the break is visible. "
            "Fix it on this branch (the bot never touches it again), then mark it ready.",
            "",
            "Last lines of the output:",
            "```",
            log_tail(report["log"]),
            "```",
        ]
    else:
        lines += ["", "The build check passed on this pin."]
    lines += ["", "Opened by the keel-repin action. It never merges: review and queue it as usual."]
    if not writer:
        lines.append(
            "It was opened with the workflow token, which does not start other workflows: close and reopen this pull request to start CI."
        )
    return title, "\n".join(lines) + "\n"


def resolve_label(gh, explicit):
    if explicit:
        return explicit
    p = run([*gh, "label", "list", "--limit", "200", "--json", "name", "--jq", ".[].name"], check=False)
    owners = [n for n in p.stdout.split() if n.startswith("owner:")]
    return owners[0] if len(owners) == 1 else ""


def cmd_pr(args):
    root = Path(args.root).resolve()
    report = json.loads(Path(args.report).read_text())
    if report["status"] not in ("ok", "failed"):
        print(f"::notice::nothing to open ({report['status']})")
        return 0
    gh = os.environ.get("KEEL_REPIN_GH", "gh").split()
    writer = os.environ.get("KEEL_REPIN_WRITER", "app") == "app"
    title, body = pr_text(report, writer)
    draft = report["status"] == "failed"
    branch = report["branch"]
    label = resolve_label(gh, args.label)
    if args.dry_run:
        print(f"DRY RUN: would push {branch} and open a pull request into {args.base}")
        print(f"title: {title}\ndraft: {draft}\nlabel: {label or '(none: no single owner: label)'}\nfiles: {len(report['changed'])}\n---\n{body}")
        return 0
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        raise Refused("GH_TOKEN is required to push and open the pull request")
    import base64

    auth = "AUTHORIZATION: basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode()
    git = ["git", "-c", "user.name=keel-repin[bot]", "-c", "user.email=keel-repin@users.noreply.github.com"]
    run([*git, "checkout", "-B", branch], cwd=root)
    run([*git, "add", "-A"], cwd=root)
    run([*git, "commit", "-q", "-m", title], cwd=root)
    run([*git, "-c", f"http.extraheader={auth}", "push", "origin", f"HEAD:refs/heads/{branch}"], cwd=root)
    cmd = [*gh, "pr", "create", "--base", args.base, "--head", branch, "--title", title, "--body", body]
    if draft:
        cmd.append("--draft")
    if label:
        cmd += ["--label", label]
    out = run(cmd, cwd=root).stdout.strip()
    print(f"opened: {out}")
    # A newer tag replaces an older open repin pull request; an older tag never closes a newer one.
    listing = run([*gh, "pr", "list", "--state", "open", "--json", "number,headRefName", "--jq", '.[] | "\\(.number) \\(.headRefName)"'], cwd=root, check=False).stdout
    mine = tag_key(report["tag"])
    for line in listing.splitlines():
        num, head = line.split(" ", 1)
        if head == branch or not head.startswith(BRANCH_PREFIX):
            continue
        other = tag_key(head[len(BRANCH_PREFIX):])
        if other and mine and other < mine:
            run([*gh, "pr", "close", num, "--delete-branch", "--comment", f"Superseded by the repin to `{report['tag']}` (`{branch}`)."], cwd=root, check=False)
            print(f"closed superseded #{num} ({head})")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--tag", required=True)
    r.add_argument("--root", default=".")
    r.add_argument("--remote", default=os.environ.get("KEEL_REMOTE", DEFAULT_REMOTE))
    r.add_argument("--check-command", default="cargo check")
    r.add_argument("--check-dir", default=".")
    r.add_argument("--report", required=True)
    r.add_argument("--ignore-branch", action="store_true", help="rebuild even when the branch exists (dry runs)")
    p = sub.add_parser("pr")
    p.add_argument("--root", default=".")
    p.add_argument("--report", required=True)
    p.add_argument("--base", default="main")
    p.add_argument("--label", default="")
    p.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    try:
        return {"run": cmd_run, "pr": cmd_pr}[args.cmd](args)
    except Refused as e:
        print(f"::error::{e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
