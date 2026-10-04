#!/usr/bin/env python3
"""Check that a repository's Keel pin follows the consumer guide.

The guide says a title pins a weekly tag, never a moving main and never an arbitrary commit. This check
reads every Keel revision a repository pins, from the places a title keeps it:

  KEEL_PIN, keel-ref, deps/keel.rev   a commit (or a keel tag) on the first line, at any depth, vendor/ included
  Cargo.toml                          rev / tag / branch on a line (or a per-dependency table) that names
                                      the Keel git repository; a Keel git dependency with none of them
                                      follows Keel's default branch
  Cargo.lock                          git+https://github.com/SylphxAI/keel...#<sha> sources
  .cargo/config.toml                  [source."git+https://github.com/SylphxAI/keel?rev=<sha>"] tables

and judges them against a local clone of Keel (the action fetches `main` and the verified/weekly tags,
commits only):

  fail     a pin is not reachable from Keel main (an arbitrary commit, a branch, a typo), a pin names a
           branch or no revision at all (a moving main), or the files pin more than one Keel commit
  warn     a pinned commit is not the commit of a keel-verified-* / keel-weekly-* tag

Ratchet: with --base (or "auto": the first parent of a merge commit, which is a pull request's base), the
failures only stand when the pull request changes the set of Keel commits the repository pins. A pin set
the pull request leaves alone is reported as warnings, so existing forks are not turned red; they turn red
the moment someone moves a pin. Without a base (a push, a manual run) every failure stands.

Usage (from the repository):
  keel_pin_check.py check --keel-dir DIR [--keel-remote URL] [--head HEAD] [--base auto|REV]
                          [--keel-main refs/heads/main] [--summary FILE]
Exit status: 0 pass (warnings allowed), 1 a failure, 2 the check itself could not run.
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

HEX_RE = re.compile(r"[0-9a-f]{7,40}")
TAG_RE = re.compile(r"keel-(verified|weekly)-(\d{4}-\d{2}-\d{2})(?:-(\d+))?")
KEEL_URL = re.compile(r"SylphxAI/keel(?:\.git)?(?![\w-])")
REV_ATTR = re.compile(r'\brev\s*=\s*"([^"]*)"')
TAG_ATTR = re.compile(r'\btag\s*=\s*"([^"]*)"')
BRANCH_ATTR = re.compile(r'\bbranch\s*=\s*"([^"]*)"')
GIT_ATTR = re.compile(r'\bgit\s*=\s*"([^"]*)"')
LOCK_SOURCE = re.compile(r'git\+https://github\.com/SylphxAI/keel(?:\.git)?(?:\?[^#"]*)?#([0-9a-f]{40})')
CONFIG_SOURCE = re.compile(r'git\+https://github\.com/SylphxAI/keel(?:\.git)?\?(rev|tag|branch)=([^"\'#&\s\]]+)')
DEP_TABLE = re.compile(r"^\[\s*(?:target\.[^\]]+\.)?(?:workspace\.)?(?:dev-|build-)?dependencies\.[^\]]+\]\s*$")
PIN_NAMES = ("KEEL_PIN", "keel-ref")
SKIP_PARTS = ("target", "node_modules")
MAX_FILE_BYTES = 16_000_000
GIT_TIMEOUT = 300


class Refused(Exception):
    """The check could not run (a bad argument, an unreadable repository)."""


class Pin:
    __slots__ = ("file", "line", "kind", "value")

    def __init__(self, file, line, kind, value):
        self.file, self.line, self.kind, self.value = file, line, kind, value

    def where(self):
        return f"{self.file}:{self.line}"


def git(args, cwd, check=True, input_text=None, env=None):
    full = {**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0", **(env or {})}
    try:
        p = subprocess.run(["git", *args], cwd=cwd, env=full, text=True, input=input_text,
                           capture_output=True, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Refused(f"git {' '.join(args[:2])} failed: {e}")
    if check and p.returncode != 0:
        raise Refused(f"git {' '.join(args[:2])} failed ({p.returncode}): {(p.stderr or p.stdout).strip()[-400:]}")
    return p


# ---------------------------------------------------------------- reading a tree

def is_candidate(path):
    parts = path.split("/")
    if any(p in SKIP_PARTS for p in parts[:-1]):
        return False
    name = parts[-1]
    return (name in PIN_NAMES or path == "deps/keel.rev" or path.endswith("/deps/keel.rev")
            or name in ("Cargo.toml", "Cargo.lock") or (name in ("config.toml", "config") and ".cargo" in parts[:-1]))


def read_tree(root, rev):
    """{path: text} of the files in REV that can hold a Keel pin, read from git objects."""
    listing = git(["ls-tree", "-r", "-z", "--name-only", rev], root).stdout
    paths = [p for p in listing.split("\0") if p and is_candidate(p)]
    files = {}
    if not paths:
        return files
    # One cat-file process for the whole tree; a path with a newline cannot be asked for and is skipped.
    paths = [p for p in paths if "\n" not in p]
    out = subprocess.run(["git", "cat-file", "--batch"], cwd=root, input="".join(f"{rev}:{p}\n" for p in paths).encode(),
                         capture_output=True, timeout=GIT_TIMEOUT, env={**os.environ, "GIT_NO_LAZY_FETCH": "1"})
    if out.returncode != 0:
        raise Refused(f"git cat-file failed: {out.stderr.decode(errors='replace')[-300:]}")
    data, pos = out.stdout, 0
    for p in paths:
        end = data.index(b"\n", pos)
        line = data[pos:end].decode(errors="replace")
        pos = end + 1
        if line.endswith(" missing"):  # a submodule or an unreadable entry
            continue
        size = int(line.rsplit(" ", 2)[2])
        body = data[pos:pos + size]
        pos += size + 1
        if size <= MAX_FILE_BYTES:
            files[p] = body.decode(errors="replace")
    return files


# ---------------------------------------------------------------- finding pins

def pins_in_pin_file(path, text):
    for no, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if s and not s.startswith("#"):
            return [Pin(path, no, "ref", s.split()[0])]
    return []


def is_keel_git(line):
    """A `git = "<Keel repository>"` attribute (a mention of Keel's URL in a package field does not count)."""
    m = GIT_ATTR.search(line)
    return bool(m and KEEL_URL.search(m.group(1)))


def pins_in_cargo_toml(path, text):
    pins = []
    lines = text.splitlines()
    section_start, section_keel, in_dep_table = 0, False, False
    for i, raw in enumerate(lines + ["["]):  # a trailing header flushes the last section
        s = raw.strip()
        if s.startswith("["):
            if in_dep_table and section_keel:
                pins += attrs_of(path, lines, section_start, i, whole_section=True)
            section_start, section_keel, in_dep_table = i, False, bool(DEP_TABLE.match(s))
            continue
        if s.startswith("#") or not is_keel_git(raw):
            continue
        section_keel = True
        if not in_dep_table:
            pins += attrs_of(path, lines, i, i + 1, whole_section=False)
    return pins


def attrs_of(path, lines, start, stop, whole_section):
    """The pin attributes of a Keel dependency written on one line, or spread over a per-dependency table."""
    found, keel_line = [], start + 1
    for i in range(start, stop):
        raw = lines[i]
        if raw.strip().startswith("#"):
            continue
        if is_keel_git(raw):
            keel_line = i + 1
        elif whole_section and GIT_ATTR.search(raw):
            continue  # a different git source inside the same table
        for rx, kind in ((REV_ATTR, "ref"), (TAG_ATTR, "ref"), (BRANCH_ATTR, "branch")):
            for m in rx.finditer(raw):
                found.append(Pin(path, i + 1, kind, m.group(1)))
    if not found:
        # A Keel git dependency with no rev, tag or branch follows Keel's default branch.
        found.append(Pin(path, keel_line, "branch", "(default branch)"))
    return found


def pins_in_lock(path, text):
    return [Pin(path, no, "ref", m.group(1)) for no, line in enumerate(text.splitlines(), 1)
            for m in [LOCK_SOURCE.search(line)] if m]


def pins_in_config(path, text):
    pins = []
    for no, line in enumerate(text.splitlines(), 1):
        for m in CONFIG_SOURCE.finditer(line):
            pins.append(Pin(path, no, "branch" if m.group(1) == "branch" else "ref", m.group(2)))
    return pins


def find_pins(files):
    pins = []
    for path in sorted(files):
        name, text = path.rsplit("/", 1)[-1], files[path]
        if name in PIN_NAMES or path.endswith("deps/keel.rev"):
            pins += pins_in_pin_file(path, text)
        elif name == "Cargo.toml":
            pins += pins_in_cargo_toml(path, text)
        elif name == "Cargo.lock":
            pins += pins_in_lock(path, text)
        else:
            pins += pins_in_config(path, text)
    return pins


# ---------------------------------------------------------------- Keel

def tag_key(tag):
    m = TAG_RE.fullmatch(tag)
    if not m:
        return None
    kind, date, n = m.groups()
    return (date, 0 if kind == "weekly" else 1, int(n or 0))


def ensure_keel(keel_dir, remote, main_ref):
    """Fetch Keel's main and its verified/weekly tags into KEEL_DIR: every commit, no trees or blobs.

    Reachability needs the whole commit history (a depth-limited fetch answers wrongly for a pin older than
    the cut), and commits alone are small. The token, if any, travels in the environment (GIT_CONFIG_*)."""
    d = Path(keel_dir)
    if not (d / "HEAD").exists():
        d.mkdir(parents=True, exist_ok=True)
        git(["init", "-q", "--bare"], d)
        git(["remote", "add", "origin", remote], d)
    branch = main_ref.removeprefix("refs/heads/")
    git(["fetch", "-q", "--filter=tree:0", "--no-tags", "origin", f"+refs/heads/{branch}:{main_ref}",
         "+refs/tags/keel-verified-*:refs/tags/keel-verified-*", "+refs/tags/keel-weekly-*:refs/tags/keel-weekly-*"], d)


class Keel:
    def __init__(self, keel_dir, main_ref):
        self.dir, self.main_ref = keel_dir, main_ref
        if git(["rev-parse", "--verify", "--quiet", main_ref + "^{commit}"], keel_dir, check=False).returncode != 0:
            raise Refused(f"{main_ref} is not in the Keel clone at {keel_dir}; fetch Keel's main first")
        self.tag_commit = {}   # tag name -> commit
        self.commit_tags = {}  # commit -> [tag names]
        out = git(["for-each-ref", "--format=%(refname:short) %(objectname) %(*objectname)",
                   "refs/tags/keel-verified-*", "refs/tags/keel-weekly-*"], keel_dir).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2 and tag_key(parts[0]):
                sha = parts[2] if len(parts) > 2 else parts[1]  # the peeled commit of an annotated tag
                self.tag_commit[parts[0]] = sha
                self.commit_tags.setdefault(sha, []).append(parts[0])
        self._cache = {}

    def newest_tag(self):
        return max(self.tag_commit, key=tag_key) if self.tag_commit else None

    def resolve(self, value):
        """(full sha or None, why not). A tag name resolves through Keel's tags, a hex string as a commit."""
        if value in self._cache:
            return self._cache[value]
        if TAG_RE.fullmatch(value):
            sha = self.tag_commit.get(value)
            res = (sha, None) if sha else (None, "not a keel-verified-* or keel-weekly-* tag Keel has")
        elif HEX_RE.fullmatch(value):
            p = git(["rev-parse", "--verify", "--quiet", value + "^{commit}"], self.dir, check=False)
            sha = p.stdout.strip()
            res = (sha, None) if p.returncode == 0 and sha else (None, "not on Keel main (it is in no history of main or of a verified or weekly tag)")
        else:
            res = (None, "neither a commit nor a keel tag")
        self._cache[value] = res
        return res

    def on_main(self, sha):
        return git(["merge-base", "--is-ancestor", sha, self.main_ref], self.dir, check=False).returncode == 0


# ---------------------------------------------------------------- judging

def commit_key(keel, pin):
    """What a pin stands for: its full commit, or its raw text when it cannot be resolved."""
    if pin.kind == "branch":
        return f"branch:{pin.value}"
    sha, _ = keel.resolve(pin.value)
    return sha or f"unresolved:{pin.value}"


def where_of(group, limit=3):
    places = sorted({p.where() for p in group})
    return ", ".join(places[:limit]) + (f" and {len(places) - limit} more" if len(places) > limit else "")


def judge(pins, keel):
    """[(level, pin or None, message)] for one tree's pins; level is "error" or "warning"."""
    findings, by_commit, unresolved = [], {}, {}
    for pin in pins:
        if pin.kind == "branch":
            what = "follows Keel's default branch" if pin.value == "(default branch)" else f"follows the branch {pin.value}"
            findings.append(("error", pin, f"{pin.where()} {what}; pin a keel-verified-* tag's commit, never a moving branch"))
            continue
        sha, why = keel.resolve(pin.value)
        if sha is None:
            unresolved.setdefault((pin.value, why), []).append(pin)
        else:
            by_commit.setdefault(sha, []).append(pin)
    for (value, why), group in unresolved.items():
        findings.append(("error", group[0], f"{where_of(group)} pins {value}, which is {why}"))
    for sha, group in by_commit.items():
        short = sha[:9]
        where = where_of(group)
        if not keel.on_main(sha):
            findings.append(("error", group[0], f"commit {sha} ({where}) is not on Keel main; pin a commit that is, ideally a keel-verified-* tag's"))
        elif sha not in keel.commit_tags:
            newest = keel.newest_tag()
            hint = f"; the newest is {newest} at {keel.tag_commit[newest][:9]}" if newest else ""
            findings.append(("warning", group[0], f"commit {short} ({where}) is on Keel main but is not a keel-verified-* or keel-weekly-* tag{hint}"))
    distinct = {**by_commit, **{value: group for (value, _), group in unresolved.items()}}
    if len(distinct) > 1:
        parts = "; ".join(f"{key[:9]} in {', '.join(sorted({p.file for p in g})[:3])}" for key, g in sorted(distinct.items()))
        findings.append(("error", None, f"{len(distinct)} different Keel commits are pinned ({parts}); every pin must be one commit"))
    return findings


def check(root, head, base, keel):
    """Returns (findings, notes, changed). Failures stand only when the pin set moved (the ratchet)."""
    head_pins = find_pins(read_tree(root, head))
    notes = []
    if not head_pins:
        return [], ["no Keel pin found in KEEL_PIN, keel-ref, deps/keel.rev, Cargo.toml, Cargo.lock or .cargo/config.toml"], True
    head_keys = {commit_key(keel, p) for p in head_pins}
    changed = True
    base_rev = resolve_base(root, head, base, notes)
    if base_rev:
        base_keys = {commit_key(keel, p) for p in find_pins(read_tree(root, base_rev))}
        changed = base_keys != head_keys
        if not changed:
            notes.append("the pull request does not move the Keel pin, so failures are shown as warnings (a ratchet: existing forks stay green until a pin moves)")
    findings = judge(head_pins, keel)
    if not changed:
        findings = [("warning", pin, msg + " [existing]") for _, pin, msg in findings]
    return findings, notes, changed


def resolve_base(root, head, base, notes):
    """The git revision to compare pins against, or "" to enforce (no base, or it cannot be read)."""
    if not base:
        return ""
    if base == "auto":
        parents = git(["rev-list", "--parents", "-n", "1", head], root).stdout.split()[1:]
        if len(parents) < 2:
            return ""  # not a merge commit: a push or a manual run
        base = parents[0]
    if git(["cat-file", "-e", base + "^{commit}"], root, check=False).returncode != 0:
        notes.append(f"base {base[:12]} is not in this checkout (use fetch-depth: 2); enforcing every rule")
        return ""
    return base


# ---------------------------------------------------------------- output

def esc(text):
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def report(findings, notes, out=None):
    out = out or sys.stdout
    for note in notes:
        print(f"::notice::{esc(note)}", file=out)
    for level, pin, msg in findings:
        loc = f" file={pin.file},line={pin.line}" if pin else ""
        print(f"::{level}{loc}::{esc(msg)}", file=out)
    errors = sum(1 for f in findings if f[0] == "error")
    warnings = sum(1 for f in findings if f[0] == "warning")
    print(f"keel pin check: {errors} failure(s), {warnings} warning(s)", file=out)


def summary_md(findings, notes):
    lines = ["### Keel pin check", ""]
    if not findings and not notes:
        lines.append("Every Keel pin is one commit, on Keel main, at a verified or weekly tag.")
    for note in notes:
        lines.append(f"- {note}")
    for level, _, msg in findings:
        lines.append(f"- **{level}** {msg}")
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("command", choices=["check"])
    ap.add_argument("--root", default=".", help="the repository being checked")
    ap.add_argument("--head", default="HEAD", help="revision whose pins are judged")
    ap.add_argument("--base", default="", help='revision to compare pins with: "auto" (first parent of a merge commit), a revision, or empty to enforce')
    ap.add_argument("--keel-dir", required=True, help="bare clone of Keel (created by --keel-remote when missing)")
    ap.add_argument("--keel-remote", default="", help="fetch Keel main and its tags from here into --keel-dir")
    ap.add_argument("--keel-main", default="refs/heads/main", help="ref in --keel-dir that is Keel main")
    ap.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY", ""), help="file to append a markdown summary to")
    args = ap.parse_args(argv)
    try:
        if args.keel_remote:
            ensure_keel(args.keel_dir, args.keel_remote, args.keel_main)
        keel = Keel(args.keel_dir, args.keel_main)
        findings, notes, _ = check(args.root, args.head, args.base, keel)
    except Refused as e:
        print(f"::error::keel pin check could not run: {esc(str(e))}")
        return 2
    report(findings, notes)
    if args.summary:
        with open(args.summary, "a") as f:
            f.write(summary_md(findings, notes))
    return 1 if any(f[0] == "error" for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
