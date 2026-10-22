#!/usr/bin/env python3
"""Fail a workflow that pins another repository's action or reusable workflow to a commit
that is not on that repository's default branch.

A pin to the head of an unmerged pull request works until the pull request is squash-merged
and its branch deleted: the commit becomes unreachable, and the next `merge_group` run fails
at startup with no job and no annotation. This check catches the pin while the pull request
that adds it is open, and names the file and line.

  workflow_pin_check.py check --dir REPO   judge REPO/.github/workflows and REPO/.github/actions
  workflow_pin_check.py audit --org ORG    judge every non-archived repository of ORG (read-only)

A pin is `uses: owner/repo[/path]@<40-hex>` where `owner` is one of the organizations in
policy/optimistic-merge.json (override with --owner). Tags and branches are not judged here
(the conformance audits and the Keel pin check cover them); a local `./` call has no pin.
`owner/repo@sha` passes when GitHub's compare of `default...sha` says the pin is identical to
the default branch or behind it; ahead, diverged, unknown (404) and unreadable all fail.

Read-only: REST compare reads and GraphQL queries through the `gh` CLI (GH_TOKEN in CI).
Exit 1 on any offender.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import audit_optimistic_merge as om  # noqa: E402  (same reader, same conventions)

ROOT = Path(__file__).resolve().parents[1]
USES = re.compile(r"^\s*-?\s*uses\s*:\s*['\"]?([\w.-]+)/([\w.-]+)((?:/[^@\s'\"]+)?)@([0-9a-fA-F]{40})['\"]?\s*(?:#.*)?$")
ANCESTOR = ("identical", "behind")


def default_owners() -> list[str]:
    return list(json.loads((ROOT / "policy" / "optimistic-merge.json").read_text())["orgs"])


def find_pins(text: str, owners: list[str]) -> list[dict]:
    """Every full-SHA pin of an owned repository in one workflow or action file, with its line."""
    wanted = {o.lower() for o in owners}
    pins = []
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        match = USES.match(line)
        if match and match.group(1).lower() in wanted:
            pins.append({"line": number, "repo": f"{match.group(1)}/{match.group(2)}",
                         "path": match.group(3).lstrip("/"), "sha": match.group(4).lower()})
    return pins


class Reachability:
    """`pin is on the default branch of repo`, one default-branch read per repo and one compare per pin."""

    def __init__(self, gh):
        self.gh, self.branches, self.status = gh, {}, {}

    def check(self, repo: str, sha: str) -> tuple[bool, str]:
        try:
            if repo not in self.branches:
                self.branches[repo] = self.gh.rest(f"repos/{repo}")["default_branch"]
            branch = self.branches[repo]
            if (repo, sha) not in self.status:
                self.status[(repo, sha)] = self.gh.rest(f"repos/{repo}/compare/{branch}...{sha}")["status"]
        except Exception as err:  # noqa: BLE001 - unreadable is a failure, whatever the reason
            return False, f"{sha[:9]} could not be read on {repo} ({str(err)[:80]}): not on its default branch, or no access"
        status = self.status[(repo, sha)]
        if status in ANCESTOR:
            return True, ""
        return False, f"{sha[:9]} is not on {repo} {self.branches[repo]} (compare says {status}): merge the pull request that holds it, then pin the merge commit"


def judge(files: dict[str, str], owners: list[str], reach: Reachability) -> list[dict]:
    """Offenders in `files` ({path: text}): [{file, line, repo, sha, why}]."""
    offenders = []
    for name in sorted(files):
        for pin in find_pins(files[name] or "", owners):
            ok, why = reach.check(pin["repo"], pin["sha"])
            if not ok:
                offenders.append({"file": name, "line": pin["line"], "repo": pin["repo"], "sha": pin["sha"], "why": why})
    return offenders


def read_dir(root: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for sub in (".github/workflows", ".github/actions"):
        base = root / sub
        if base.is_dir():
            for path in sorted(base.rglob("*")):
                if path.is_file() and path.suffix in (".yml", ".yaml"):
                    files[str(path.relative_to(root))] = path.read_text(errors="replace")
    return files


def read_repo(gh, org: str, name: str) -> tuple[dict[str, str] | None, str]:
    """Workflow texts of one repository's default branch (None when unreadable)."""
    query = ('query{repository(owner:%s,name:%s){wf: object(expression:"HEAD:.github/workflows"){... on Tree{entries{name object{... on Blob{text}}}}}}}'
             % (json.dumps(org), json.dumps(name)))
    try:
        reply = gh.graphql(query)
    except Exception as err:  # noqa: BLE001
        return None, str(err)[:120]
    node = (reply.get("data") or {}).get("repository")
    if node is None:
        return None, json.dumps(reply.get("errors"))[:120]
    tree = node.get("wf")
    entries = (tree or {}).get("entries") or []
    return {f".github/workflows/{e['name']}": (e.get("object") or {}).get("text") or ""
            for e in entries if e["name"].endswith((".yml", ".yaml"))}, ""


def audit(gh, orgs: list[str], owners: list[str], hands_off: list[str]) -> dict:
    reach = Reachability(gh)
    repos = []
    for org in orgs:
        for base in om.list_org(gh, org):
            repo = f"{org}/{base['name']}"
            if org.lower() in om.NEVER_READ_OWNERS or any(fnmatch.fnmatchcase(repo, pattern) for pattern in hands_off):
                continue
            files, error = read_repo(gh, org, base["name"])
            if files is None:
                repos.append({"repo": repo, "status": "UNREADABLE", "offenders": [], "error": error})
                continue
            offenders = judge(files, owners, reach)
            repos.append({"repo": repo, "status": "FAIL" if offenders else "PASS", "offenders": offenders,
                          "pins": sum(len(find_pins(t, owners)) for t in files.values())})
    counts = {s: sum(1 for r in repos if r["status"] == s) for s in ("PASS", "FAIL", "UNREADABLE")}
    return {"repos": repos, "summary": {"repos": len(repos), **counts}}


def render_offenders(offenders: list[dict]) -> str:
    return "\n".join(f"{o['file']}:{o['line']}: pin {o['repo']}@{o['sha'][:9]}: {o['why']}" for o in offenders)


def main(argv: list[str] | None = None, gh=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--owner", action="append", help="owner whose pins are judged (default: the policy organizations)")
    sub = parser.add_subparsers(dest="mode", required=True)
    check = sub.add_parser("check", help="judge one checked-out repository")
    check.add_argument("--dir", default=".")
    scan = sub.add_parser("audit", help="judge every repository of the organizations")
    scan.add_argument("--org", action="append", required=True)
    scan.add_argument("--json", dest="json_out")
    args = parser.parse_args(argv)
    owners = args.owner or default_owners()
    gh = gh or om.Gh()
    if args.mode == "check":
        offenders = judge(read_dir(Path(args.dir)), owners, Reachability(gh))
        if offenders:
            print(render_offenders(offenders))
            for o in offenders:
                print(f"::error file={o['file']},line={o['line']}::{o['why']}")
            return 1
        print("workflow pins ok")
        return 0
    policy = json.loads((ROOT / "policy" / "optimistic-merge.json").read_text())
    hands_off = [r for e in policy.get("exemptions", []) if e["class"] == "hands-off" for r in e["repos"]]
    report = audit(gh, args.org, owners, hands_off)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=1))
    for repo in report["repos"]:
        if repo["status"] == "FAIL":
            print(f"FAIL   {repo['repo']}\n" + "\n".join("         " + line for line in render_offenders(repo["offenders"]).splitlines()))
        elif repo["status"] == "UNREADABLE":
            print(f"UNREAD {repo['repo']}  {repo['error']}")
    s = report["summary"]
    print(f"{s['repos']} repositories: {s['PASS']} PASS, {s['FAIL']} FAIL, {s['UNREADABLE']} unreadable")
    return 1 if s["FAIL"] or s["UNREADABLE"] else 0


if __name__ == "__main__":
    sys.exit(main())
