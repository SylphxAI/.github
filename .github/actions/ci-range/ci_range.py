#!/usr/bin/env python3
"""The range a CI run covers, and which of the caller's lanes it selects.

One answer per event, so a repository's merge-queue gate and its post-merge
verify workflow agree on what "changed" means:

  pull_request   base = the pull request's base commit (GitHub checks out the
                 merge commit, so base..HEAD is exactly the change)
  merge_group    base = the merge group's base commit (everything the group adds)
  push, dispatch base = the newest first-parent ancestor with an authenticated
                 successful `verified` job from a full push of the approved
                 workflow on the tracked branch (200 commits at most).
                 Diagnostic, recording and merge-group runs never count.
                 One run therefore covers every commit since the last one whose
                 full suite passed, never a single commit (a base of HEAD~1
                 skips the commits a push carries).
  schedule       no base: every lane runs.

Lanes come from the caller as `name: pattern pattern ...` lines. A pattern is
an fnmatch glob over repository paths in which `*` also crosses `/`
(`crates/*` is everything under crates/); a lone `*` always selects. A lane
with no patterns always runs. No base, or a change under `.github/`, selects
every lane: a workflow change can affect any of them.

`only` (a dispatch of the verify workflow, by a person or by the red-main
handler) runs exactly the named lanes, whatever the range selected: naming a
lane is an explicit request, and a dispatch on an already verified head has an
empty range, so narrowing to it would run nothing (a signed-build dispatch on a
verified main skipped the very lanes it named). The handler names lanes by job
name, so a `caller / job` prefix and a ` (matrix)` suffix are ignored, and
names that are not lanes (such as `verified`) are dropped. If nothing named is
a lane, every selected lane runs: running more is safe, running nothing is not.

Outputs (GITHUB_OUTPUT): base, run (JSON object lane -> bool), changed (count).
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import sys
import urllib.request

VERIFY_CHECK = "verified"
WALK_LIMIT = 200


def git(*args: str, check: bool = True) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True)
    if check and result.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout.strip() if result.returncode == 0 else ""


def parse_lanes(text: str) -> dict[str, list[str]]:
    lanes: dict[str, list[str]] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        name, _, patterns = line.partition(":")
        name = name.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ValueError(f"lane name {name!r} must be letters, digits, '_' or '-' (it is a job id)")
        if name in lanes:
            raise ValueError(f"lane {name!r} is declared twice")
        lanes[name] = patterns.split()
    if not lanes:
        raise ValueError("no lanes declared")
    return lanes


def lane_name(job: str) -> str:
    """`verify / rust (linux)` -> `rust`: the handler passes job names."""
    job = job.strip().rsplit(" / ", 1)[-1]
    return re.sub(r"\s*\(.*\)$", "", job).strip()


def select(lanes: dict[str, list[str]], changed: list[str] | None, only: str = "") -> dict[str, bool]:
    """changed=None means no base: everything runs."""
    everything = changed is None or any(p.startswith(".github/") for p in changed)
    run = {}
    for name, patterns in lanes.items():
        if everything or not patterns or "*" in patterns:
            run[name] = True
        else:
            run[name] = any(fnmatch.fnmatchcase(path, pattern) for path in changed for pattern in patterns)
    wanted = {lane_name(x) for x in only.split(",") if x.strip()} & set(lanes)
    if wanted:
        run = {name: name in wanted for name in run}
    return run


def api(path: str) -> dict:
    url = f"{os.environ.get('GITHUB_API_URL', 'https://api.github.com')}/{path}"
    request = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {os.environ['TOKEN']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def is_ancestor(base: str, head: str = "HEAD") -> bool:
    return subprocess.run(["git", "merge-base", "--is-ancestor", base, head], capture_output=True).returncode == 0


def last_verified(repo: str, workflow_file: str, branch: str) -> str:
    """The last commit whose full suite passed, or '' (every lane runs)."""
    # The helper is also embedded verbatim in red-main.yml (enforced by a
    # source-equality test). Both consumers use one producer/origin contract.
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("post_main", Path(__file__).with_name("post_main.py"))
    proof = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(proof)
    try:
        return proof.last_verified(repo, branch, workflow_file)
    except Exception as error:
        print(f"::warning::authenticated post-main base unavailable ({error}); every lane runs")
        return ""


def base_for(event: str, payload: dict, repo: str, workflow_file: str, branch: str) -> str:
    if event == "pull_request" or event == "pull_request_target":
        return payload["pull_request"]["base"]["sha"]
    if event == "merge_group":
        return payload["merge_group"]["base_sha"]
    if event in ("push", "workflow_dispatch"):
        return last_verified(repo, workflow_file, branch)
    return ""


def main() -> int:
    mode = os.environ.get("PROOF_MODE", "range")
    if mode != "range":
        import importlib.util
        from pathlib import Path
        spec = importlib.util.spec_from_file_location("post_main", Path(__file__).with_name("post_main.py"))
        proof = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(proof)
        repo = os.environ["GITHUB_REPOSITORY"]
        branch = os.environ.get("PROOF_BRANCH", "main")
        workflow = os.environ.get("PROOF_WORKFLOW", "verify.yml")
        identity = os.environ.get("PROOF_IDENTITY", "")
        if mode == "run":
            try:
                if identity:
                    run = proof.api(f"repos/{repo}/actions/runs/{identity}")
                else:
                    run = max(proof.push_runs(repo, branch, workflow), key=lambda r: r["id"], default=None)
                verdict = proof.checked_verdict(run, repo, branch, workflow) if run else "unknown"
            except Exception as error:
                # A caller can keep recovery active on unknown; a failed
                # preflight job would skip the handler before it can recover.
                print(f"::warning::post-main proof unavailable: {error}", file=sys.stderr)
                verdict = "unknown"
            with open(os.environ["GITHUB_OUTPUT"], "a") as out:
                out.write(f"verdict={verdict}\n")
            return 0
        raise ValueError("unsupported proof mode")
    lanes = parse_lanes(os.environ["LANES"])
    event = os.environ["GITHUB_EVENT_NAME"]
    payload = json.load(open(os.environ["GITHUB_EVENT_PATH"])) if os.environ.get("GITHUB_EVENT_PATH") else {}
    workflow_file = os.environ.get("GITHUB_WORKFLOW_REF", "").split("@", 1)[0].rsplit("/", 1)[-1]
    default_branch = (payload.get("repository") or {}).get("default_branch") or "main"
    if event in ("push", "workflow_dispatch") and git("rev-parse", "--is-shallow-repository") == "true":
        # Finding the last verified ancestor needs history; blobs are not needed.
        git("fetch", "--quiet", "--no-tags", "--filter=blob:none", "--unshallow", "origin", check=False)
    base = base_for(event, payload, os.environ["GITHUB_REPOSITORY"], workflow_file, default_branch)
    if base and not git("cat-file", "-t", base, check=False):
        # A pull request or merge group diffs two trees: the base commit alone is enough.
        git("fetch", "--quiet", "--no-tags", "--depth=1", "--filter=blob:none", "origin", base, check=False)
    if base and not git("cat-file", "-t", base, check=False):
        print(f"::warning::base {base} is not reachable; every lane runs")
        base = ""
    changed = git("diff", "--name-only", "--no-renames", base, "HEAD").splitlines() if base else None
    run = select(lanes, changed, os.environ.get("ONLY", ""))
    with open(os.environ["GITHUB_OUTPUT"], "a") as out:
        out.write(f"base={base}\nrun={json.dumps(run, separators=(',', ':'))}\n")
        out.write(f"changed={'' if changed is None else len(changed)}\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    lines = [f"### CI range ({event})", "", f"base: `{base or 'none - every lane runs'}`  head: `{os.environ.get('GITHUB_SHA', '')}`",
             f"changed files: {'all' if changed is None else len(changed)}", "", "| lane | runs |", "| --- | --- |"]
    lines += [f"| {name} | {'yes' if selected else 'no'} |" for name, selected in run.items()]
    print("\n".join(lines))
    if summary:
        with open(summary, "a") as fh:
            fh.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
