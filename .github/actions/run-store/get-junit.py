#!/usr/bin/env python3
"""Discover exact JUnit keys from authenticated producer step names, not wildcards."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


def api(path):
    env = dict(os.environ, GH_TOKEN=os.environ.get("ACTIONS_TOKEN") or os.environ.get("GH_TOKEN", ""))
    return json.loads(subprocess.check_output(["gh", "api", path], env=env, text=True, timeout=60))


def entries(rows, run_id):
    names = set()
    for job in rows:
        if str(job.get("run_id")) != str(run_id):
            raise ValueError("job belongs to another run")
        for step in job.get("steps", []):
            name = step.get("name", "")
            if step.get("conclusion") == "skipped":
                continue
            if name.startswith("run-store put junit-"):
                name = name[len("run-store put "):]
                if not re.fullmatch(r"junit-[A-Za-z0-9._-]{1,94}", name):
                    raise ValueError("invalid JUnit discovery name")
                names.add(name)
    return sorted(names)


def fetch(repo, run_id, destination):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or not re.fullmatch(r"[0-9]{1,20}", str(run_id)):
        raise ValueError("invalid repository or run id")
    repository_id = str(api(f"repos/{repo}")["id"])
    rows = []
    page = 1
    while True:
        batch = api(f"repos/{repo}/actions/runs/{run_id}/jobs?filter=latest&per_page=100&page={page}")["jobs"]
        rows.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    names = entries(rows, run_id)
    lanes = {}
    for job in rows:
        for name in entries([job], run_id):
            lane = job.get("name")
            if not isinstance(lane, str) or not lane.strip():
                raise ValueError("producer job has no lane name")
            lane = lane.replace(",", ";")
            if name in lanes and lanes[name] != lane:
                raise ValueError("JUnit key is shared by different jobs")
            lanes[name] = lane
    found = bool(names)
    with tempfile.TemporaryDirectory(prefix="junit-store-") as tmp:
        stage = Path(tmp) / "entries"
        for index, name in enumerate(names):
            output = Path(tmp) / f"output-{index}"
            env = dict(os.environ, MODE="get", NAME=name, STORE_PATH=str(stage / name),
                       STORE_RUN_ID=str(run_id), REPO_ID=repository_id, REQUIRED="false", GITHUB_OUTPUT=str(output))
            subprocess.run(["bash", str(Path(__file__).with_name("run-store.sh"))], env=env, check=True, timeout=3600)
            if not output.exists() or "found=true" not in output.read_text().splitlines():
                found = False
                break
        # Never expose a partial shard set as evidence that a failed test passed.
        if found:
            Path(destination).mkdir(parents=True, exist_ok=True)
            for name in names:
                shutil.move(str(stage / name), str(Path(destination) / name))
            (Path(destination) / ".run-store-lanes.json").write_text(json.dumps(lanes))
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            output.write(f"found={str(found).lower()}\n")
    return found


if __name__ == "__main__":
    try:
        repo, run_id, destination = sys.argv[1:]
        sys.exit(0 if fetch(repo, run_id, destination) else 1)
    except Exception as error:
        print(f"::warning::JUnit run-store discovery failed: {error}", file=sys.stderr)
        sys.exit(1)
