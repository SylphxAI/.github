#!/usr/bin/env python3
"""Move a repository's Keel pin to a Keel tag and open or update the repin pull request.

Steps, run from the repository root (the action calls them in this order):

  keel_repin.py poll [--tag latest]
      Resolve the tag, and write `tag`, `sha` and `needed` to $GITHUB_OUTPUT. Nothing is needed when
      the pin is already the tag, the tag is not ahead of the pin, or a pull request for the tag
      exists in any state (a closed one is never reopened by a later poll).

  keel_repin.py run --tag keel-verified-2026-10-03-1|latest [--check-command CMD] [--report FILE]
      Resolve the tag to its commit, rewrite every Keel pin, refresh Cargo.lock for the
      Keel crates, run the check command, and write a report. Never fails because the
      build is broken: the break goes in the report so the pull request shows it.

  keel_repin.py pr --report FILE [--dry-run]
      Branch, commit, push and open (or report) the pull request. A broken build opens it
      as a draft with the error summary. Merging is `settle`'s job, never this step's.

  keel_repin.py dispatch --branch B --workflows "ci.yml ..."
      Start the title's CI on the branch by workflow_dispatch, in order, waiting for each run to
      appear on the branch head. A pull request opened with the workflow token starts no
      pull_request run, so this is what puts the checks on the pull request's head commit.

  keel_repin.py settle [--required "ci-ok web-smoke"] [--owner @team] [--dry-run]
      For every open repin pull request: merge it when, and only when, every required check ran on
      its exact head commit and succeeded, no check on that commit failed, it is not a draft, and
      every commit on it is the bot's own. A red pull request is left open with one comment that
      names the failing check and the owner; a pending one is left for the next run. Idempotent:
      it keeps no state, so the title's schedule just runs it again.

What counts as a pin: a `rev = "<40 hex>"` or `tag = "keel-..."` on a Cargo.toml line that
names the Keel git repository, every `deps/keel.rev`, and the old full or short commit
anywhere else in tracked text files (Dockerfiles, cargo config, CI, docs). A repository
that needs more (vendored sources, a patch block) keeps `tools/repin_keel.sh <tag>`; when
it exists it replaces the rewrite and the lock refresh. Files under .github/workflows/ are never
edited (the workflow token cannot push them); the pull request lists those that name the old pin.
"""

import argparse
import calendar
import json
import os
import re
import subprocess
import sys
import tempfile
import time
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
WORKFLOWS = ".github/workflows/"
MAX_TEXT_BYTES = 2_000_000
LOG_TAIL_LINES = 40
LOG_TAIL_CHARS = 5000
BOT_EMAIL = "keel-repin@users.noreply.github.com"
RED_CONCLUSIONS = ("failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale")
RED_MARKER = "<!-- keel-repin-red:"
DEFAULT_REQUIRED = "ci-ok web-smoke"
DEFAULT_MAX_WAIT_MINUTES = 360


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
        if f.startswith(SKIP_DIRS) or "/vendor/" in f or Path(f).name in SKIP_NAMES or f.startswith(WORKFLOWS):
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


def workflow_mentions(root, old_shas):
    """Workflow files that still name an old pin: the workflow token cannot edit them, a person does."""
    if not old_shas:
        return []
    pat = re.compile("|".join(f"(?<![0-9a-f]){re.escape(o[:n])}(?![0-9a-f])" for o in old_shas for n in (40, 12, 8, 7)))
    out = []
    for f in tracked_files(root):
        p = Path(root) / f
        if f.startswith(WORKFLOWS) and p.is_file() and not p.is_symlink() and is_text(p) and pat.search(p.read_text(errors="replace")):
            out.append(f)
    return sorted(out)


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
    report = {"tag": args.tag, "sha": sha, "branch": branch, "status": "unchanged", "stage": "", "changed": [], "old": [], "manual": [], "log": ""}

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
    report["manual"] = workflow_mentions(root, old)
    run(["git", "checkout", "--", WORKFLOWS.rstrip("/")], cwd=root, check=False)  # a hook may have edited them
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


def pr_text(report):
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
    if report.get("manual"):
        lines += [
            "",
            "These workflow files still name the old pin. The workflow token cannot edit workflow files, so a person changes them on this branch:",
            *[f"- `{f}`" for f in report["manual"]],
        ]
    if report["status"] == "failed":
        what = {"hook": "the repository's repin script", "lock": "the Cargo.lock refresh", "check": "the build check"}[report["stage"]]
        lines += [
            "",
            f"**Draft: {what} fails on this Keel tag.** The pull request is open as a draft so the break is visible. "
            "Fix it on this branch (the bot never touches it again), then mark it ready. CI was not started by the bot.",
            "",
            "Last lines of the output:",
            "```",
            log_tail(report["log"]),
            "```",
        ]
    else:
        lines += [
            "",
            "The build check passed on this pin. A pull request opened with the workflow token starts no pull_request run, "
            "so the bot starts the repository's CI by workflow_dispatch on this branch; its checks land on this head commit.",
        ]
    lines += ["", "Opened by the keel-repin action. It merges this pull request itself once CI and the web smoke are green on this exact head; "
              "a person can merge it earlier, or close it to stop that."]
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
    title, body = pr_text(report)
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
    report["head_sha"] = run(["git", "rev-parse", "HEAD"], cwd=root).stdout.strip()
    run([*git, "-c", f"http.extraheader={auth}", "push", "origin", f"HEAD:refs/heads/{branch}"], cwd=root)
    cmd = [*gh, "pr", "create", "--base", args.base, "--head", branch, "--title", title, "--body", body]
    if draft:
        cmd.append("--draft")
    if label:
        cmd += ["--label", label]
    out = run(cmd, cwd=root).stdout.strip()
    print(f"opened: {out}")
    report["pr"] = out
    Path(args.report).write_text(json.dumps(report, indent=2))
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


def gh_bin():
    return os.environ.get("KEEL_REPIN_GH", "gh").split()


def compare_status(old, new, token):
    """How `new` stands against `old` in the Keel repository: ahead, behind, diverged or identical."""
    env = {**os.environ, "GH_TOKEN": token} if token else None
    p = run([*gh_bin(), "api", f"repos/SylphxAI/keel/compare/{old}...{new}", "--jq", ".status"], env=env, check=False)
    if p.returncode != 0:
        raise Refused(f"could not compare the Keel pin {old[:9]} with {new[:9]}: {(p.stderr or p.stdout).strip()[-200:]}")
    return p.stdout.strip()


def unmatched_commits(remote, tag_sha, pin):
    """Commits on `pin` whose change the tag does not already hold (`git cherry`: '+' lines, patch-id based, as `git rebase` decides), and the branches whose tip is the pin."""
    with tempfile.TemporaryDirectory() as d:
        run(["git", "init", "-q", "--bare", d])
        run(["git", "-C", d, "fetch", "-q", "--no-tags", remote, tag_sha, pin])
        out = run(["git", "-C", d, "cherry", tag_sha, pin]).stdout
        heads = run(["git", "ls-remote", "--heads", remote]).stdout
    unmatched = [l.split()[1][:9] for l in out.splitlines() if l.startswith("+")]
    branches = [l.split()[1].removeprefix("refs/heads/") for l in heads.splitlines() if l.split()[0] == pin]
    return unmatched, branches


def pr_exists(branch):
    """A pull request from this branch exists in any state (open, closed or merged)."""
    p = run([*gh_bin(), "pr", "list", "--head", branch, "--state", "all", "--json", "number", "--jq", "length"], check=False)
    return p.returncode == 0 and p.stdout.strip() not in ("", "0")


def write_outputs(pairs):
    path = os.environ.get("GITHUB_OUTPUT")
    for k, v in pairs.items():
        print(f"{k}={v}")
        if path:
            with open(path, "a") as f:
                f.write(f"{k}={v}\n")


def cmd_poll(args):
    root = Path(args.root).resolve()
    tag = newest_verified_tag(args.remote) if args.tag in ("", "latest") else args.tag
    sha = resolve_tag(tag, args.remote)
    branch = BRANCH_PREFIX + tag
    out = {"tag": tag, "sha": sha, "needed": "false", "reason": ""}
    old = find_pins(root)
    if not old and not (root / HOOK).exists() and not has_tag_pins(root):
        raise Refused("no Keel pin found in any tracked Cargo.toml or deps/keel.rev; nothing to repin")
    if old == [sha]:
        out["reason"] = "already pinned"
    elif pr_exists(branch) or remote_branch_exists(root, branch):
        out["reason"] = "a pull request or branch for this tag exists"
    else:
        token = os.environ.get("KEEL_API_TOKEN", "")
        behind = [(o, compare_status(o, sha, token)) for o in old if o != sha]
        bad = []
        for o, st in behind:
            if st == "ahead":
                continue
            if st == "diverged":
                unmatched, branches = unmatched_commits(args.remote, sha, o)
                if unmatched:
                    raise Refused(
                        f"the Keel pin {o[:9]} (branch {', '.join(branches) or 'unknown'}) has commits the tag {tag} does not hold: "
                        + ", ".join(unmatched))
                continue  # every commit of the pin is already on the tag: the tag is ahead
            bad.append(f"{o[:9]} is {st}")
        if bad:
            out["reason"] = "the tag is not ahead of the pin (" + ", ".join(bad) + ")"
        else:
            out["needed"] = "true"
    print(f"::notice::keel-repin poll: {tag} needed={out['needed']} {out['reason']}")
    write_outputs(out)
    return 0


def cmd_dispatch(args):
    report = json.loads(Path(args.report).read_text())
    if report["status"] != "ok":
        print(f"::notice::CI is not started ({report['status']})")
        return 0
    branch, head = report["branch"], report.get("head_sha", "")
    workflows = args.workflows.split()
    gh = gh_bin()
    for wf in workflows:
        if args.dry_run:
            print(f"DRY RUN: would run: gh workflow run {wf} --ref {branch}")
            continue
        run([*gh, "workflow", "run", wf, "--ref", branch])
        deadline = time.time() + args.wait_seconds
        while True:
            p = run([*gh, "run", "list", "--workflow", wf, "--branch", branch, "--event", "workflow_dispatch",
                     "--json", "headSha,databaseId", "--jq", ".[] | \"\\(.headSha) \\(.databaseId)\""], check=False)
            if any(line.split()[0] == head for line in p.stdout.splitlines() if line.strip()):
                print(f"started {wf} on {branch} ({head[:9]})")
                break
            if time.time() > deadline:
                raise Refused(f"{wf} was dispatched on {branch} but no run for {head[:9]} appeared in {args.wait_seconds}s")
            time.sleep(args.interval)
    return 0


def classify(check_runs, required):
    """Judge one head commit from its check runs: ('green'|'red'|'pending', [what, ...]).

    Only runs of the GitHub Actions app count (an App cannot satisfy a required check by posting one
    under the same name). Of several runs with one name (a re-run) the newest decides. Any failed check
    on the commit makes it red, required or not; a required check that has not appeared, or has not
    finished, keeps it pending.
    """
    latest = {}
    for r in check_runs:
        if r.get("app") != "github-actions":
            continue
        if r["name"] not in latest or r["id"] > latest[r["name"]]["id"]:
            latest[r["name"]] = r
    red = sorted(n for n, r in latest.items() if r["status"] == "completed" and r["conclusion"] in RED_CONCLUSIONS)
    if red:
        return "red", [f"`{n}` {latest[n]['conclusion']}" for n in red]
    pending = []
    for name in required:
        r = latest.get(name)
        if r is None:
            pending.append(f"`{name}` has not run")
        elif r["status"] != "completed":
            pending.append(f"`{name}` is {r['status']}")
        elif r["conclusion"] != "success":
            return "red", [f"`{name}` ended {r['conclusion']}, not success"]
    for n, r in latest.items():
        if r["status"] != "completed":
            pending.append(f"`{n}` is {r['status']}")
    return ("pending", pending) if pending else ("green", [])


def gh_json(args, jq):
    p = run([*gh_bin(), *args, "--jq", jq], check=False)
    if p.returncode != 0:
        raise Refused(f"gh {' '.join(args[:3])} failed: {(p.stderr or p.stdout).strip()[-300:]}")
    return p.stdout


def repo_name():
    return os.environ.get("GITHUB_REPOSITORY") or os.environ.get("KEEL_REPIN_REPO") or "{owner}/{repo}"


def head_check_runs(repo, sha):
    out = gh_json(["api", "--paginate", f"repos/{repo}/commits/{sha}/check-runs?per_page=100"],
                  '.check_runs[] | [.id, .name, .status, (.conclusion // ""), (.app.slug // ""), .html_url] | @tsv')
    runs = []
    for line in out.splitlines():
        if line.strip():
            i, name, status, conclusion, app, url = line.split("\t")
            runs.append({"id": int(i), "name": name, "status": status, "conclusion": conclusion, "app": app, "url": url})
    return runs


def commit_emails(repo, number):
    out = gh_json(["api", "--paginate", f"repos/{repo}/pulls/{number}/commits?per_page=100"], ".[].commit.author.email")
    return [e for e in out.split() if e]


def already_told(number, sha):
    out = gh_json(["pr", "view", str(number), "--json", "comments"], ".comments[].body")
    return f"{RED_MARKER}{sha} " in out


def tell_red(number, tag, sha, owner, problems, draft, dry):
    who = f"{owner} " if owner else ""
    if draft:
        text = (f"{who}The Keel repin to `{tag}` is a draft: the build does not pass on the new tag. "
                "The bot leaves the branch alone; fix it here, mark it ready, and merge it once CI and the web smoke pass on the new head.")
    else:
        text = (f"{who}The Keel repin to `{tag}` is not merged: " + "; ".join(problems) + f" on `{sha[:9]}`. "
                "Fix it on this branch, or close the pull request and delete the branch so the next poll rebuilds it. "
                "The bot merges only when CI and the web smoke are green on the exact head.")
    text += f"\n\n{RED_MARKER}{sha} -->"
    if dry:
        print(f"DRY RUN: would comment on #{number}:\n{text}")
        return
    run([*gh_bin(), "pr", "comment", str(number), "--body", text])


def cmd_settle(args):
    repo = repo_name()
    required = args.required.split()
    prs = gh_json(["pr", "list", "--state", "open", "--limit", "50", "--json", "number,headRefName,headRefOid,isDraft,createdAt"],
                  '.[] | select(.headRefName | startswith("' + BRANCH_PREFIX + '")) | [.number, .headRefName, .headRefOid, .isDraft, .createdAt] | @tsv')
    now = time.time() if args.now is None else args.now
    for line in prs.splitlines():
        if not line.strip():
            continue
        number, branch, sha, draft, created = line.split("\t")
        tag = branch[len(BRANCH_PREFIX):]
        draft = draft == "true"
        age_min = (now - calendar.timegm(time.strptime(created, "%Y-%m-%dT%H:%M:%SZ"))) / 60
        if draft:
            if not already_told(number, sha):
                tell_red(number, tag, sha, args.owner, [], True, args.dry_run)
            print(f"#{number} {tag}: draft, left for the owner")
            continue
        strangers = sorted(set(commit_emails(repo, number)) - {BOT_EMAIL})
        if strangers:
            print(f"#{number} {tag}: has commits that are not the bot's; a person merges it")
            continue
        verdict, why = classify(head_check_runs(repo, sha), required)
        if verdict == "pending" and age_min > args.max_wait_minutes:
            verdict, why = "red", [f"still waiting after {int(age_min)} minutes ({'; '.join(why)})"]
        print(f"#{number} {tag} {sha[:9]}: {verdict} {'; '.join(why)}")
        if verdict == "pending":
            continue
        if verdict == "red":
            if not already_told(number, sha):
                tell_red(number, tag, sha, args.owner, why, False, args.dry_run)
            continue
        if args.dry_run:
            print(f"DRY RUN: would merge #{number} at {sha}")
            continue
        cmd = [*gh_bin(), "pr", "merge", number, "--squash", "--match-head-commit", sha]
        p = run(cmd, check=False)
        if p.returncode != 0 and re.search(r"--auto|merge queue", p.stderr + p.stdout):
            p = run([*cmd, "--auto"], check=False)
        if p.returncode != 0:
            tell_red(number, tag, sha, args.owner, [f"the merge was refused: {(p.stderr or p.stdout).strip()[-200:]}"], False, False)
            continue
        print(f"merged (or queued) #{number} at {sha[:9]}")
        state = gh_json(["pr", "view", number, "--json", "state"], ".state").strip()
        if state == "MERGED":
            for wf in args.after_merge.split():
                run([*gh_bin(), "workflow", "run", wf, "--ref", args.base], check=False)
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
    q = sub.add_parser("poll")
    q.add_argument("--tag", default="latest")
    q.add_argument("--root", default=".")
    q.add_argument("--remote", default=os.environ.get("KEEL_REMOTE", DEFAULT_REMOTE))
    d = sub.add_parser("dispatch")
    d.add_argument("--report", required=True)
    d.add_argument("--workflows", default="ci.yml", help="workflow files, space separated, started in order")
    d.add_argument("--wait-seconds", type=int, default=180)
    d.add_argument("--interval", type=int, default=5)
    d.add_argument("--dry-run", action="store_true")
    t = sub.add_parser("settle")
    t.add_argument("--required", default=DEFAULT_REQUIRED, help="check names that must have succeeded on the head commit")
    t.add_argument("--owner", default="", help="who the red comment addresses, e.g. @Cubeage/studio")
    t.add_argument("--max-wait-minutes", type=int, default=DEFAULT_MAX_WAIT_MINUTES)
    t.add_argument("--after-merge", default="", help="workflow files started on the base branch after a merge (a merge by the workflow token starts no push run)")
    t.add_argument("--base", default="main")
    t.add_argument("--now", type=float, default=None, help=argparse.SUPPRESS)
    t.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("pr")
    p.add_argument("--root", default=".")
    p.add_argument("--report", required=True)
    p.add_argument("--base", default="main")
    p.add_argument("--label", default="")
    p.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    try:
        return {"run": cmd_run, "pr": cmd_pr, "poll": cmd_poll, "dispatch": cmd_dispatch, "settle": cmd_settle}[args.cmd](args)
    except Refused as e:
        print(f"::error::{e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
