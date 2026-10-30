#!/usr/bin/env python3
"""Discover exact JUnit keys from authenticated producer steps or job logs."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import sys
import tempfile
import time

LOG_LIMIT = 16 * 1024 * 1024


def token_env():
    return dict(os.environ, GH_TOKEN=os.environ.get("ACTIONS_TOKEN") or os.environ.get("GH_TOKEN", ""))


def api(path):
    return json.loads(subprocess.check_output(["gh", "api", path], env=token_env(), text=True, timeout=60))


def job_log(repo, job_id):
    """Read one authenticated job log with bounded bytes, time and no log echo."""
    if not re.fullmatch(r"[0-9]{1,20}", str(job_id)):
        raise ValueError("producer job has no numeric identity")
    command = ["gh", "api", "--allow-escape-sequences", f"repos/{repo}/actions/jobs/{job_id}/logs"]
    with subprocess.Popen(command, env=token_env(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        try:
            chunks, size, deadline = [], 0, time.monotonic() + 60
            with selectors.DefaultSelector() as ready:
                ready.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not ready.select(remaining):
                        raise ValueError(f"producer job {job_id} log discovery timed out")
                    chunk = os.read(process.stdout.fileno(), min(65536, LOG_LIMIT + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > LOG_LIMIT:
                        raise ValueError(f"producer job {job_id} log exceeds discovery byte limit")
            if process.wait(timeout=max(0.01, deadline - time.monotonic())) != 0:
                raise ValueError(f"producer job {job_id} log discovery unavailable")
            return b"".join(chunks).decode("utf-8", errors="replace")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


def log_entries(text, repository_id, run_id):
    """Only completed store receipts identify keys; commands and packed bytes do not."""
    prefix = r"^\d{4}-\d{2}-\d{2}T[0-9:.]+Z run-store put (?P<name>junit-[A-Za-z0-9._-]{1,94}): "
    stored = re.compile(prefix + r"[0-9]+ bytes as run-store\.v1\." + re.escape(str(repository_id))
                        + r"\." + re.escape(str(run_id)) + r"\.(?P=name)$", re.MULTILINE)
    retained = re.compile(prefix + r"an entry already exists; it is kept$", re.MULTILINE)
    return sorted({match["name"] for pattern in (stored, retained) for match in pattern.finditer(text)})


def discover(rows, repo, repository_id, run_id):
    # Validate the complete job population before making fallback requests.
    entries(rows, run_id)

    def names_for(job):
        names = entries([job], run_id)
        if not job.get("steps") and job.get("conclusion") != "skipped":
            if job.get("status") != "completed":
                raise ValueError("producer job step discovery unavailable before completion")
            names = log_entries(job_log(repo, job.get("id")), repository_id, run_id)
        return job, names

    lanes = {}
    with ThreadPoolExecutor(max_workers=4) as executor:
        for job, names in executor.map(names_for, rows):
            for name in names:
                lane = job.get("name")
                if not isinstance(lane, str) or not lane.strip():
                    raise ValueError("producer job has no lane name")
                lane = lane.replace(",", ";")
                if name in lanes and lanes[name] != lane:
                    raise ValueError("JUnit key is shared by different jobs")
                lanes[name] = lane
    if not lanes:
        raise ValueError("no JUnit producer keys discovered from authenticated job steps or completed store receipts")
    return lanes


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
    lanes = discover(rows, repo, repository_id, run_id)
    names = sorted(lanes)
    found = True
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
