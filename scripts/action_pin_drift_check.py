#!/usr/bin/env python3
"""Fail shared SylphxAI/.github SHA pins without # main or more than N days behind main.

SylphxAI/.github holds the shared actions every repository pins by commit (needs-pass, ci-ok,
main-red-gate, secret-scan, ...). A repo that pins a stale commit keeps running the old logic, so a
verifier fix in .github reaches nobody until each pin moves. Renovate keeps the pins current when it can
follow .github main, but a pin it cannot reach, or one whose update was never merged, drifts silently:
the old commit still resolves, so nothing turns red. This check turns that silence into a failure.

  action_pin_drift_check.py check --dir REPO   judge REPO's workflows and actions, plus .github main
  action_pin_drift_check.py audit --org ORG    judge every non-archived repository of ORG (read-only)

A pin is `uses: SylphxAI/.github/.github/actions/<name>@<40-hex>` (or a pinned reusable workflow). The
check requires the trailing `# main` version comment that lets Renovate follow the branch, even for
the current head. It also reads each pinned commit's committer date and fails a pin whose date is more
than DRIFT_DAYS behind main's. It is read-only (GitHub reads through the `gh` CLI) and exits 1 on any
untracked or drifted pin.

DRIFT_DAYS exists so drift alerts while it is still cheap: the org bump of six stale needs-pass SHAs
(2026-10-15) had gone 11 to 16 days without a signal. A week is long enough for Renovate to
open and land its grouped pull request after a .github commit, and short enough that a broken gate is
caught within one working week rather than found by an unrelated incident.
"""
from __future__ import annotations

import argparse
import base64
import datetime
import fnmatch
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import audit_optimistic_merge as om  # noqa: E402  (same reader, same conventions)

ROOT = Path(__file__).resolve().parents[1]
SHARED_REPO = "SylphxAI/.github"
# Days a shared-action pin may lag .github main before the check fails. See the module docstring.
DRIFT_DAYS = 7
USES = re.compile(r"^\s*-?\s*uses\s*:\s*(['\"]?)(SylphxAI/\.github)((?:/[^@\s'\"]+)?)@([0-9a-fA-F]{40})\1\s*(?:#(?P<version>.*))?$")


def find_pins(text: str) -> list[dict]:
    """Every full-SHA pin of a SylphxAI/.github action or workflow in one file, with its line."""
    pins = []
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        match = USES.match(line)
        if match:
            pins.append({"line": number, "path": match.group(3).lstrip("/"), "sha": match.group(4).lower(),
                         "tracks_main": (match.group("version") or "").strip() == "main"})
    return pins


def main_marker_errors(files: dict[str, str]) -> list[dict]:
    """Missing Renovate branch markers, without network or commit-date reads."""
    return [{"file": name, "line": pin["line"], "path": pin["path"], "sha": pin["sha"], "days": None,
             "why": "missing # main version comment; append # main after the SHA "
                    "so Renovate can follow the shared-action branch"}
            for name in sorted(files) for pin in find_pins(files[name] or "") if not pin["tracks_main"]]


def _date(value: str) -> datetime.datetime:
    return datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)


class Commits:
    """A commit's committer date, one REST read per distinct (repo, sha)."""

    def __init__(self, gh):
        self.gh, self.dates = gh, {}

    def date(self, repo: str, sha: str) -> datetime.datetime:
        if sha not in self.dates:
            commit = self.gh.rest(f"repos/{repo}/commits/{sha}")
            self.dates[sha] = _date(commit["commit"]["committer"]["date"])
        return self.dates[sha]

    def main_head(self, repo: str) -> tuple[str, datetime.datetime]:
        commit = self.gh.rest(f"repos/{repo}/commits/main")
        return commit["sha"], _date(commit["commit"]["committer"]["date"])


def judge(files: dict[str, str], commits: Commits, head_sha: str, head_date: datetime.datetime,
          days: int = DRIFT_DAYS) -> list[dict]:
    """Drifted pins in `files` ({path: text}): [{file, line, path, sha, days, why}]."""
    offenders = main_marker_errors(files)
    for name in sorted(files):
        for pin in find_pins(files[name] or ""):
            if pin["sha"] == head_sha:
                continue
            try:
                pinned = commits.date(SHARED_REPO, pin["sha"])
            except Exception as err:  # noqa: BLE001 - unreadable is a failure, whatever the reason
                offenders.append({"file": name, "line": pin["line"], "path": pin["path"], "sha": pin["sha"],
                                  "days": None,
                                  "why": f"{pin['sha'][:9]} could not be read on {SHARED_REPO} ({str(err)[:80]})"})
                continue
            behind = (head_date - pinned).days
            if behind > days:
                offenders.append({"file": name, "line": pin["line"], "path": pin["path"], "sha": pin["sha"],
                                  "days": behind,
                                  "why": f"pins {SHARED_REPO}@{pin['sha'][:9]}, {behind} days behind main "
                                         f"(head {head_sha[:9]}); move it to or past {head_sha[:9]}"})
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
    """Workflows and nested composite actions on the default branch (None when unreadable)."""
    selection = " ".join(
        '%s: object(expression:%s){... on Tree{entries{name oid type object{... on Blob{text}}}}}'
        % (alias, json.dumps("HEAD:" + path))
        for alias, path in (("wf", ".github/workflows"), ("actions", ".github/actions")))
    query = 'query{repository(owner:%s,name:%s){%s}}' % (json.dumps(org), json.dumps(name), selection)
    try:
        reply = gh.graphql(query)
        if reply.get("errors"):
            raise RuntimeError(json.dumps(reply["errors"]))
        node = (reply.get("data") or {}).get("repository")
        if node is None:
            raise RuntimeError("repository could not be read")
        files = {}
        for alias, root in (("wf", ".github/workflows"), ("actions", ".github/actions")):
            for entry in (node.get(alias) or {}).get("entries") or []:
                path = f"{root}/{entry['name']}"
                if entry["type"] == "tree":
                    tree = gh.rest(f"repos/{org}/{name}/git/trees/{entry['oid']}?recursive=1")
                    if tree.get("truncated"):
                        raise RuntimeError(f"{path}: Git tree was truncated")
                    for item in tree["tree"]:
                        if item["type"] == "blob" and item["path"].endswith((".yml", ".yaml")):
                            blob = gh.rest(f"repos/{org}/{name}/git/blobs/{item['sha']}")
                            if blob.get("encoding") != "base64":
                                raise RuntimeError(f"{path}/{item['path']}: unsupported blob encoding")
                            files[f"{path}/{item['path']}"] = base64.b64decode(blob["content"]).decode("utf-8")
                elif entry["type"] == "blob" and path.endswith((".yml", ".yaml")):
                    content = (entry.get("object") or {}).get("text")
                    if content is None:
                        raise RuntimeError(f"{path}: workflow text could not be read")
                    files[path] = content
        return files, ""
    except Exception as err:  # noqa: BLE001
        return None, str(err)[:120]


def audit(gh, orgs: list[str], hands_off: list[str], days: int = DRIFT_DAYS, public_only: bool = False) -> dict:
    commits = Commits(gh)
    try:
        head_sha, head_date = commits.main_head(SHARED_REPO)
    except Exception as err:  # noqa: BLE001 - an unreadable head fails the run rather than passing every repo
        return {"head": None, "repos": [],
                "summary": {"repos": 0, "PASS": 0, "FAIL": 0, "UNREADABLE": 0, "head_error": str(err)[:120]}}
    repos = []
    for org in orgs:
        if org.lower() in om.NEVER_READ_OWNERS:
            continue
        try:
            bases = om.list_org(gh, org)
        except Exception as err:  # noqa: BLE001
            repos.append({"repo": f"{org}/*", "status": "UNREADABLE", "offenders": [], "error": str(err)[:120]})
            continue
        for base in bases:
            repo = f"{org}/{base['name']}"
            # Updating the source repository's own immutable pins would advance main again forever.
            if repo.lower() == SHARED_REPO.lower():
                continue
            if (public_only and base["isPrivate"]) or any(fnmatch.fnmatchcase(repo, pattern) for pattern in hands_off):
                continue
            files, error = read_repo(gh, org, base["name"])
            if files is None:
                repos.append({"repo": repo, "status": "UNREADABLE", "offenders": [], "error": error})
                continue
            offenders = judge(files, commits, head_sha, head_date, days)
            repos.append({"repo": repo, "status": "FAIL" if offenders else "PASS", "offenders": offenders,
                          "pins": sum(len(find_pins(t)) for t in files.values())})
    counts = {s: sum(1 for r in repos if r["status"] == s) for s in ("PASS", "FAIL", "UNREADABLE")}
    return {"head": head_sha, "scope": "public" if public_only else "visible", "repos": repos,
            "summary": {"repos": len(repos), **counts}}


def render_offenders(offenders: list[dict]) -> str:
    return "\n".join(f"{o['file']}:{o['line']}: pin {SHARED_REPO}/{o['path']}@{o['sha'][:9]}: {o['why']}"
                     if o["path"] else f"{o['file']}:{o['line']}: pin {SHARED_REPO}@{o['sha'][:9]}: {o['why']}"
                     for o in offenders)


# --- watch the watcher: a red trunk must draw a red-main run that actually repaired it ------------

VERIFY_WORKFLOW = "Verify"
RED_MAIN_WORKFLOW = "Red main"
RED_MAIN_WINDOW_MINUTES = 30
# The caller's guard (SylphxAI/.github .github/workflows/red-main.yml; keel lines 36-38, cloud 52-54):
#   github.event_name == 'workflow_dispatch'
#   || (github.event.workflow_run.conclusion == 'failure' && github.event.workflow_run.head_branch == 'main')
# So a non-skipped run means a manual dispatch or a Verify that concluded `failure` on the trunk. But a
# green conclusion is NOT a repair: the classification step can emit verdict=infra (or the handler can stop
# on an already-in-hand revert), after which the repair steps are skipped and the run still ends success.
# The check therefore keys on the REPAIR: the run must have executed a step past the classification gate.
# `REPAIR_STEP` is the last step of that chain ("Revert the culprit or report it"), which runs a revert or
# the report path (migration-hold, protected pull request, infra) - either is a real response, not an ack.
REPAIR_STEP = "Revert the culprit or report it"


def _run_jobs(gh, repo: str, run_id: int) -> list[dict]:
    """The jobs (with their steps) of one run - the evidence of how far the handler got."""
    reply = gh.rest(f"repos/{repo}/actions/runs/{run_id}/jobs")
    return reply.get("jobs", [])


def _repaired(gh, repo: str, run: dict) -> bool:
    """Did this red-main run reach the repair step (run, not skipped)? A green ack does not count."""
    if run.get("conclusion") in (None, "skipped"):
        return False
    for job in _run_jobs(gh, repo, run["id"]):
        for step in job.get("steps") or []:
            if step.get("name") == REPAIR_STEP and step.get("conclusion") not in (None, "skipped"):
                return True
    return False


def _runs(gh, repo: str, workflow: str, pages: int = 3) -> list[dict]:
    """The newest runs of a named workflow on `repo` (empty when the workflow does not exist)."""
    rows = []
    for page in range(1, pages + 1):
        reply = gh.rest(f"repos/{repo}/actions/runs?per_page=100&page={page}")
        batch = reply.get("workflow_runs", [])
        rows.extend(r for r in batch if r.get("name") == workflow)
        if len(batch) < 100:
            break
    return rows


def _started(run: dict) -> datetime.datetime:
    """When a run began: `run_started_at` (queue waits excluded) or `created_at`."""
    return _date(run.get("run_started_at") or run["created_at"])


def watcher_offenders(gh, repo: str, trunk: str, window_minutes: int = RED_MAIN_WINDOW_MINUTES) -> list[dict]:
    """Failed Verify runs on the trunk with no red-main run that repaired them inside `window_minutes`."""
    try:
        verify = _runs(gh, repo, VERIFY_WORKFLOW)
        red = _runs(gh, repo, RED_MAIN_WORKFLOW)
    except Exception as err:  # noqa: BLE001 - unreadable is a failure, whatever the reason
        return [{"verify_run": None, "verify_at": None, "why": f"runs could not be read ({str(err)[:80]})"}]
    if not verify:
        return []
    # Only a run that got past classification to the repair step counts; one call per candidate run.
    red_repaired = [_started(run) for run in red if _repaired(gh, repo, run)]
    offenders = []
    for run in verify:
        if run.get("head_branch") != trunk or run.get("conclusion") != "failure":
            continue
        when = _started(run)
        deadline = when + datetime.timedelta(minutes=window_minutes)
        if any(when <= r <= deadline for r in red_repaired):
            continue
        offenders.append({"verify_run": run.get("id"), "verify_at": when.isoformat().replace("+00:00", "Z"),
                          "why": f"trunk Verify failed ({run['created_at']}) and no {RED_MAIN_WORKFLOW} run "
                                 f"reached its repair step within {window_minutes} minutes (the red-main "
                                 f"safety net did not fire; a green run that stopped at classification is not a repair)"})
    return offenders


def watch_main(gh, args, hands_off: list[str]) -> int:
    """The `watcher` command: a red trunk with no red-main run that reached its repair step is a broken net."""
    repos = []
    for org in args.org:
        if org.lower() in om.NEVER_READ_OWNERS:
            continue
        try:
            bases = om.list_org(gh, org)
        except Exception as err:  # noqa: BLE001
            repos.append({"repo": f"{org}/*", "status": "FAIL",
                          "offenders": [{"why": f"repository list could not be read ({str(err)[:120]})"}]})
            continue
        for base in bases:
            repo = f"{org}/{base['name']}"
            if (args.public_only and base["isPrivate"]) or any(fnmatch.fnmatchcase(repo, pattern) for pattern in hands_off):
                continue
            offenders = watcher_offenders(gh, repo, (base.get("defaultBranchRef") or {}).get("name") or "main",
                                          args.window_minutes)
            repos.append({"repo": repo, "status": "FAIL" if offenders else "PASS", "offenders": offenders})
    if args.json_out:
        Path(args.json_out).write_text(json.dumps({"scope": "public" if args.public_only else "visible", "repos": repos}, indent=1))
    for repo in repos:
        if repo["status"] == "FAIL":
            print(f"FAIL   {repo['repo']}")
            for o in repo["offenders"]:
                print(f"         {o['why']}")
    failed = sum(1 for r in repos if r["status"] == "FAIL")
    print(f"{len(repos)} {'public' if args.public_only else 'visible'} repositories: "
          f"{len(repos) - failed} PASS, {failed} FAIL "
          f"(a red trunk with no {RED_MAIN_WORKFLOW} run that reached its repair step within "
          f"{args.window_minutes} minutes)")
    return 1 if failed else 0


def main(argv: list[str] | None = None, gh=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--days", type=int, default=DRIFT_DAYS, help=f"allowed days behind main (default {DRIFT_DAYS})")
    sub = parser.add_subparsers(dest="mode", required=True)
    check = sub.add_parser("check", help="judge one checked-out repository")
    check.add_argument("--dir", default=".")
    scan = sub.add_parser("audit", help="judge every repository of the organizations")
    scan.add_argument("--org", action="append", required=True)
    scan.add_argument("--json", dest="json_out")
    scan.add_argument("--public-only", action="store_true", help="audit public repositories only; excludes private coverage")
    watch = sub.add_parser("watcher", help="every repository: a red trunk must draw a non-skipped red-main run")
    watch.add_argument("--org", action="append", required=True)
    watch.add_argument("--window-minutes", type=int, default=RED_MAIN_WINDOW_MINUTES)
    watch.add_argument("--json", dest="json_out")
    watch.add_argument("--public-only", action="store_true", help="audit public repositories only; excludes private coverage")
    args = parser.parse_args(argv)
    gh = gh or om.Gh()
    if args.mode == "check":
        commits = Commits(gh)
        try:
            head_sha, head_date = commits.main_head(SHARED_REPO)
        except Exception as err:  # noqa: BLE001
            print(f"could not read {SHARED_REPO} main: {err}", file=sys.stderr)
            return 2
        offenders = judge(read_dir(Path(args.dir)), commits, head_sha, head_date, args.days)
        if offenders:
            print(render_offenders(offenders))
            for o in offenders:
                print(f"::error file={o['file']},line={o['line']}::{o['why']}")
            return 1
        print(f"shared-action pins have # main and are at most {args.days} days behind "
              f"{SHARED_REPO} main ({head_sha[:9]})")
        return 0
    policy = json.loads((ROOT / "policy" / "optimistic-merge.json").read_text())
    hands_off = [r for e in policy.get("exemptions", []) if e["class"] == "hands-off" for r in e["repos"]]
    if args.mode == "watcher":
        return watch_main(gh, args, hands_off)
    report = audit(gh, args.org, hands_off, args.days, args.public_only)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=1))
    for repo in report["repos"]:
        if repo["status"] in ("FAIL", "UNREADABLE"):
            detail = render_offenders(repo["offenders"]) if repo["status"] == "FAIL" else repo["error"]
            print(f"{repo['status'][:4]:4}   {repo['repo']}\n" + "\n".join("         " + line for line in detail.splitlines()))
    s = report["summary"]
    if "head_error" in s:
        print(f"could not read {SHARED_REPO} main: {s['head_error']}")
        return 2
    print(f"{s['repos']} {report['scope']} repositories: {s['PASS']} PASS, {s['FAIL']} FAIL, {s['UNREADABLE']} unreadable "
          f"(at most {args.days} days behind {SHARED_REPO} main {report['head'][:9]})")
    return 1 if s["FAIL"] or s["UNREADABLE"] else 0


if __name__ == "__main__":
    sys.exit(main())
