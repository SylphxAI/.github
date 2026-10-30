#!/usr/bin/env python3
"""Fail invalid, unreleased, missing-target or stale SylphxAI/.github pins.

Callers require full SHAs with # main so Renovate follows the branch. Every pin
must be an ancestor of the captured shared main anchor and contain the exact
action metadata or workflow file. The source repository alone exempts the
comment and resolves @main to that anchor. Missing proof always fails.

The shared caller gate uses this validator without an age limit and follows
shared dependencies at their pinned source commits. The
standalone check and central audit additionally enforce DRIFT_DAYS: a week
allows Renovate to land a grouped update while exposing stale shared logic
within a working week. GitHub reads use gh; --shared-source explicitly selects
a complete offline origin/main snapshot, reported with its full anchor SHA.
"""
from __future__ import annotations

import argparse
import base64
import datetime
import fnmatch
import json
import os
import re
import sys
import subprocess
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import audit_optimistic_merge as om  # noqa: E402 - reason: the shared reader lives beside this standalone script

ROOT = Path(__file__).resolve().parents[1]
SHARED_REPO = "SylphxAI/.github"
# Days a shared-action pin may lag .github main before the check fails. See the module docstring.
DRIFT_DAYS = 7
USES = re.compile(r"^\s*-?\s*uses\s*:\s*(?P<value>'[^']*'|\"[^\"]*\"|[^\s'\"]+)(?:\s+#(?P<version>.*))?\s*$")
SHARED_USE = re.compile(r"SylphxAI/\.github(?P<path>/[^@\s]+)?(?:@(?P<ref>.*))?", re.IGNORECASE)
FULL_SHA = re.compile(r"[0-9a-fA-F]{40}")


def find_pins(text: str) -> list[dict]:
    """Every shared-source reference, including invalid branch/tag/short-SHA refs."""
    pins = []
    for number, line in enumerate(text.splitlines(), 1):
        match = USES.match(line)
        if not match:
            continue
        value = match.group("value")
        if value.startswith(("'", '\"')):
            value = value[1:-1]
        reference = SHARED_USE.fullmatch(value)
        if reference:
            ref = reference.group("ref") or ""
            full_sha = FULL_SHA.fullmatch(ref) is not None
            pins.append({"line": number, "path": (reference.group("path") or "").lstrip("/"),
                         "sha": ref.lower() if full_sha else ref, "full_sha": full_sha,
                         "tracks_main": (match.group("version") or "").strip() == "main"})
    return pins


def _offender(name, pin, why, days=None):
    return {"file": name, "line": pin["line"], "path": pin["path"], "sha": pin["sha"],
            "days": days, "why": why}


def target_paths(path: str) -> set[str]:
    """The exact regular files GitHub loads for an action or reusable workflow."""
    if path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml")):
        return {path}
    return {f"{path}/action.yml", f"{path}/action.yaml"} if path else {"action.yml", "action.yaml"}


def main_marker_errors(files: dict[str, str], require_main_marker: bool = True) -> list[dict]:
    """Pure format checks; only the source exempts its comment and may use @main."""
    errors = []
    for name in sorted(files):
        for pin in find_pins(files[name] or ""):
            if not pin["full_sha"] and not (not require_main_marker and pin["sha"] == "main"):
                errors.append(_offender(name, pin, "shared-action ref must be a full 40-hex commit SHA"))
            elif require_main_marker and not pin["tracks_main"]:
                errors.append(_offender(name, pin, "missing # main version comment; append # main after the SHA "
                                        "so Renovate can follow the shared-action branch"))
    return errors


def validate_pin_provenance(files: dict[str, str], head_sha: str, facts: dict,
                            require_main_marker: bool = True) -> list[dict]:
    """Pure validation of acquired main ancestry and exact target-file evidence."""
    errors = main_marker_errors(files, require_main_marker)
    for name in sorted(files):
        for pin in find_pins(files[name] or ""):
            source_main = not require_main_marker and pin["sha"] == "main"
            if not pin["full_sha"] and not source_main:
                continue
            sha = head_sha if source_main else pin["sha"]
            proof = facts.get(sha, {})
            if not FULL_SHA.fullmatch(head_sha or "") or "ancestor" not in proof or proof.get("error"):
                errors.append(_offender(name, pin, "pin provenance could not be read: " +
                                        proof.get("error", "main ancestry/target evidence is unavailable")))
                continue
            if proof["ancestor"] is not True:
                errors.append(_offender(name, pin, f"{pin['sha'][:9]} is not on {SHARED_REPO} main "
                                        f"(anchor {head_sha[:9]}); use a merged main commit"))
                continue
            if not isinstance(proof.get("files"), (set, frozenset, list, tuple)):
                errors.append(_offender(name, pin, "pin target evidence is unavailable"))
                continue
            targets = target_paths(pin["path"])
            if not targets.intersection(proof["files"]):
                errors.append(_offender(name, pin, f"target is absent at {sha[:9]}: " +
                                        " or ".join(sorted(targets))))
    return errors


def _date(value: str) -> datetime.datetime:
    return datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)


class Commits:
    """Public source proof, cached by immutable commit and main anchor."""
    mode = "live"

    def __init__(self, gh, repository=SHARED_REPO):
        self.gh, self.dates, self.commits, self.proofs = gh, {}, {}, {}
        self.repository = repository
        self.blobs, self.texts = {}, {}

    def commit(self, ref):
        if ref not in self.commits:
            value = self.gh.rest(f"repos/{self.repository}/commits/{ref}")
            if not FULL_SHA.fullmatch(value["sha"]):
                raise ValueError("shared main did not resolve to an immutable commit")
            if FULL_SHA.fullmatch(ref) and value["sha"].lower() != ref.lower():
                raise ValueError("source commit response does not match requested SHA")
            self.commits[ref] = self.commits[value["sha"]] = value
            self.dates[value["sha"]] = _date(value["commit"]["committer"]["date"])
        return self.commits[ref]

    def date(self, repo, sha):
        self.commit(sha)
        return self.dates[sha]

    def main_head(self, repo, branch="main"):
        value = self.commit(branch)
        return value["sha"], self.dates[value["sha"]]

    def evidence(self, sha, head):
        key = (sha, head)
        if key not in self.proofs:
            try:
                status = "identical" if sha == head else self.gh.rest(
                    f"repos/{self.repository}/compare/{head}...{sha}")["status"]
                if status not in ("identical", "behind", "ahead", "diverged"):
                    raise ValueError("unrecognized compare status: " + str(status))
                proof = {"ancestor": status in ("identical", "behind")}
                if proof["ancestor"]:
                    commit = self.commit(sha)
                    tree = self.gh.rest(f"repos/{self.repository}/git/trees/{commit['commit']['tree']['sha']}?recursive=1")
                    if tree.get("truncated"):
                        raise ValueError("source Git tree was truncated")
                    proof["files"] = {item["path"] for item in tree["tree"]
                                      if item["type"] == "blob" and item.get("mode") in ("100644", "100755")}
                    self.blobs[sha] = {item["path"]: item["sha"] for item in tree["tree"]
                                       if item["path"] in proof["files"]}
                self.proofs[key] = proof
            except Exception as error:  # noqa: BLE001 - reason: every acquisition failure must become a failed proof
                self.proofs[key] = {"error": str(error)[:200]}
        return self.proofs[key]


    def read_text(self, sha, path):
        blob = self.blobs[sha][path]
        if blob not in self.texts:
            value = self.gh.rest(f"repos/{self.repository}/git/blobs/{blob}")
            if value.get("encoding") != "base64":
                raise ValueError("source blob is not readable base64")
            self.texts[blob] = base64.b64decode(value["content"], validate=False).decode("utf-8")
        return self.texts[blob]


class GitCommits:
    """Explicit offline proof from a complete checkout's origin/main snapshot."""
    mode = "offline origin/main snapshot"

    def __init__(self, root):
        self.root, self.dates, self.proofs = str(root), {}, {}
        self.texts = {}

    def git(self, *args):
        result = subprocess.run(["git", "-C", self.root, *args], text=True, capture_output=True, timeout=60,
                                env={**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0"})
        if result.returncode:
            raise ValueError((result.stderr or "shared source Git read failed").strip()[:200])
        return result.stdout

    def date(self, repo, sha):
        if sha not in self.dates:
            self.dates[sha] = datetime.datetime.fromtimestamp(
                int(self.git("show", "-s", "--format=%ct", sha)), datetime.timezone.utc)
        return self.dates[sha]

    def main_head(self, repo):
        if self.git("rev-parse", "--is-shallow-repository").strip() != "false":
            raise ValueError("offline shared source is shallow; ancestry cannot be proved")
        head = self.git("rev-parse", "refs/remotes/origin/main^{commit}").strip()
        return head, self.date(repo, head)

    def evidence(self, sha, head):
        key = (sha, head)
        if key not in self.proofs:
            try:
                result = subprocess.run(["git", "-C", self.root, "merge-base", "--is-ancestor", sha, head],
                                        text=True, capture_output=True, timeout=60,
                                env={**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0"})
                if result.returncode not in (0, 1):
                    raise ValueError((result.stderr or "shared ancestry unavailable").strip())
                proof = {"ancestor": result.returncode == 0}
                if proof["ancestor"]:
                    proof["files"] = set()
                    for entry in self.git("ls-tree", "-r", "-z", sha).split("\0"):
                        if entry:
                            meta, path = entry.split("\t", 1)
                            mode, kind, _ = meta.split()
                            if kind == "blob" and mode in ("100644", "100755"):
                                proof["files"].add(path)
                self.proofs[key] = proof
            except Exception as error:  # noqa: BLE001 - reason: offline input failures must become failed proofs
                self.proofs[key] = {"error": str(error)[:200]}
        return self.proofs[key]


    def read_text(self, sha, path):
        if (sha, path) not in self.texts:
            self.texts[sha, path] = self.git("show", f"{sha}:{path}")
        return self.texts[sha, path]


def local_targets(text):
    """Self-repository actions and local reusable workflows resolve at the running commit.

    Ordinary ./ action paths use the caller's workspace, so they are not source dependencies.
    See GitHub's workflow syntax, jobs.<job_id>.uses and jobs.<job_id>.steps[*].uses.
    """
    for number, line in enumerate(text.splitlines(), 1):
        match = USES.match(line)
        if not match:
            continue
        value = match.group("value").strip("'\"")
        if value.startswith("$/"):
            yield number, value[2:]
        elif value.startswith("./.github/workflows/") and value.endswith((".yml", ".yaml")):
            yield number, value[2:]


def dependency_errors(files, source, head_sha):
    """Inspect the pinned source graph, attributing each broken nested reference to its caller."""
    errors = []
    for name in sorted(files):
        for root in find_pins(files[name] or ""):
            if not root["full_sha"] and root["sha"] != "main":
                continue
            sha = head_sha if root["sha"] == "main" else root["sha"]
            queue, seen = deque([(sha, root["path"], [])]), set()
            while queue:
                commit, path, chain = queue.popleft()
                proof = source.evidence(commit, head_sha)
                if proof.get("ancestor") is not True or proof.get("error"):
                    continue  # The direct or parent reference already reports this failed proof.
                targets = target_paths(path).intersection(proof.get("files", ()))
                if not targets:
                    continue
                # GitHub loads action.yml before action.yaml when both are present.
                preferred = f"{path}/action.yml" if path else "action.yml"
                target = preferred if preferred in targets else sorted(targets)[0]
                if (commit, target) in seen:
                    continue
                seen.add((commit, target))
                trail = chain + [f"{commit}:{target}"]
                try:
                    text = source.read_text(commit, target)
                except Exception as error:  # noqa: BLE001 - reason: unreadable dependency source must fail closed
                    finding = _offender(name, root, f"dependency {target}@{commit[:9]} could not be read: {str(error)[:160]}")
                    errors.append({**finding, "dependency_chain": trail})
                    continue
                nested = find_pins(text)
                facts = {pin["sha"] if pin["full_sha"] else head_sha:
                         source.evidence(pin["sha"] if pin["full_sha"] else head_sha, head_sha)
                         for pin in nested if pin["full_sha"] or pin["sha"] == "main"}
                for failure in validate_pin_provenance({target: text}, head_sha, facts, require_main_marker=False):
                    finding = _offender(name, root, f"dependency {target}@{commit[:9]}:{failure['line']}: {failure['why']}")
                    errors.append({**finding, "dependency_chain": trail})
                for pin in nested:
                    if pin["full_sha"] or pin["sha"] == "main":
                        queue.append((pin["sha"] if pin["full_sha"] else head_sha, pin["path"], trail))
                for line, local in local_targets(text):
                    if target_paths(local).intersection(proof["files"]):
                        queue.append((commit, local, trail))
                    else:
                        finding = _offender(name, root, f"dependency {target}@{commit[:9]}:{line}: local target is absent: {local}")
                        errors.append({**finding, "dependency_chain": trail})
    return errors


def judge(files: dict[str, str], commits, head_sha: str, head_date: datetime.datetime,
          days: int | None = DRIFT_DAYS, require_main_marker: bool = True) -> list[dict]:
    shas = {head_sha if pin["sha"] == "main" and not require_main_marker else pin["sha"]
            for text in files.values() for pin in find_pins(text or "")
            if pin["full_sha"] or (pin["sha"] == "main" and not require_main_marker)}
    facts = {sha: commits.evidence(sha, head_sha) for sha in sorted(shas)}
    offenders = validate_pin_provenance(files, head_sha, facts, require_main_marker)
    offenders.extend(dependency_errors(files, commits, head_sha))
    if days is not None:
        for name in sorted(files):
            for pin in find_pins(files[name] or ""):
                sha = head_sha if pin["sha"] == "main" and not require_main_marker else pin["sha"]
                if sha not in facts or not facts[sha].get("ancestor"):
                    continue
                behind = (head_date - commits.date(SHARED_REPO, sha)).days
                if behind > days:
                    offenders.append(_offender(name, pin, f"pins {SHARED_REPO}@{pin['sha'][:9]}, {behind} days behind main "
                                               f"(head {head_sha[:9]}); move it to or past {head_sha[:9]}", behind))
    return offenders


def review_pins(files, repository="", shared_source=None, gh=None):
    """Caller gate: live proof by default, explicitly anchored offline proof when requested."""
    source = GitCommits(shared_source) if shared_source else Commits(gh or om.Gh())
    if not any(find_pins(text or "") for text in files.values()):
        return {"head": None, "mode": "no shared pins", "offenders": []}
    head, date = source.main_head(SHARED_REPO)
    return {"head": head, "mode": source.mode,
            "offenders": judge(files, source, head, date, days=None,
                               require_main_marker=repository.lower() != SHARED_REPO.lower())}


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
    except Exception as err:  # noqa: BLE001 - reason: every acquisition failure must produce an audit finding
        return None, str(err)[:120]


def audit(gh, orgs: list[str], hands_off: list[str], days: int = DRIFT_DAYS, public_only: bool = False) -> dict:
    commits = Commits(gh)
    try:
        head_sha, head_date = commits.main_head(SHARED_REPO)
    except Exception as err:  # noqa: BLE001 - reason: an unreadable head must fail the entire audit
        return {"head": None, "repos": [],
                "summary": {"repos": 0, "PASS": 0, "FAIL": 0, "UNREADABLE": 0, "head_error": str(err)[:120]}}
    repos = []
    for org in orgs:
        if org.lower() in om.NEVER_READ_OWNERS:
            continue
        try:
            bases = om.list_org(gh, org)
        except Exception as err:  # noqa: BLE001 - reason: every acquisition failure must produce an audit finding
            repos.append({"repo": f"{org}/*", "status": "UNREADABLE", "offenders": [], "error": str(err)[:120]})
            continue
        for base in bases:
            repo = f"{org}/{base['name']}"
            if (public_only and base["isPrivate"]) or any(fnmatch.fnmatchcase(repo, pattern) for pattern in hands_off):
                continue
            files, error = read_repo(gh, org, base["name"])
            if files is None:
                repos.append({"repo": repo, "status": "UNREADABLE", "offenders": [], "error": error})
                continue
            offenders = judge(files, commits, head_sha, head_date, days,
                              require_main_marker=repo.lower() != SHARED_REPO.lower())
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
    except Exception as err:  # noqa: BLE001 - reason: every acquisition failure must produce an audit finding
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
        except Exception as err:  # noqa: BLE001 - reason: every acquisition failure must produce an audit finding
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
    check.add_argument("--repository", default="", help="owner/name; only the shared source exempts # main and permits @main")
    check.add_argument("--shared-source", help="explicit offline checkout with complete origin/main history")
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
        commits = GitCommits(args.shared_source) if args.shared_source else Commits(gh)
        try:
            head_sha, head_date = commits.main_head(SHARED_REPO)
        except Exception as err:  # noqa: BLE001 - reason: every acquisition failure must produce an audit finding
            print(f"could not read {SHARED_REPO} main: {err}", file=sys.stderr)
            return 2
        offenders = judge(read_dir(Path(args.dir)), commits, head_sha, head_date, args.days,
                          require_main_marker=args.repository.lower() != SHARED_REPO.lower())
        print(f"shared pin proof: {commits.mode}, main anchor {head_sha}")
        if offenders:
            print(render_offenders(offenders))
            for o in offenders:
                print(f"::error file={o['file']},line={o['line']}::{o['why']}")
            return 1
        print(f"shared-action pins have valid format, main ancestry and targets; "
              f"at most {args.days} days behind {SHARED_REPO} main ({head_sha[:9]})")
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
