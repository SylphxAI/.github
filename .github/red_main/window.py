"""The regression window of a red trunk, per failing unit (Findit-style).

A unit that newly fails inside an already-red trunk is traced from the newest
earlier completed verify run in which that unit did not fail, not from the last
commit where everything passed. Units are `kind<TAB>name<TAB>job` rows, the same
as junit.py writes: a `test` row is a failing test of a job, a `lane` row is a
job with no junit (it stands for every test of that job).

  window.py window <repo> <branch> <workflow> <created> <aggregate> <confirmed.tsv> <targets.tsv>
      Prints `base <sha> <run id> <unit count>` and writes the units whose
      baseline that is, or prints `none <why>`. Walks the verify runs on the
      branch, newest first, from the run before the failing one; the first run
      that clears any confirmed unit decides, so the window is the narrowest
      one. A run clears a unit when its job succeeded, or when the job failed
      but its junit shows the test did not (a producer that lists passing tests
      must list this one). A job that was skipped, cancelled or absent clears
      nothing. The aggregate job (`verified`) is not a unit.
  window.py judge <targets.tsv> <failing.tsv>
      Prints `fail <why>` when a unit of targets failed in a candidate run
      whose failing units are failing.tsv: the same test of the same job, or
      the same job failing the same way (a lane row's name is how it failed:
      its failed steps and error lines). A target's job that failed some
      other way may hide the target (an earlier step failed, so the target's
      step never ran), so it prints `inconclusive <why>`; else `pass <why>`.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ElementTree
from datetime import datetime

WALK_RUNS = 100
SETTLED = ("success", "failure", "timed_out")
FAILED = ("failure", "timed_out")


def timestamp(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("workflow history timestamp lacks timezone")
    return parsed


def read_units(path):
    rows = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) == 3 and all(parts):
                    rows.append(tuple(parts))
    except OSError:
        pass
    return rows


def earlier_runs(rows, created, is_eligible):
    """Completed, settled, genuine push runs created before `created`, newest first."""
    before = timestamp(created)
    runs = [
        row for row in rows
        if isinstance(row, dict) and row.get("status") == "completed"
        and row.get("conclusion") in SETTLED and timestamp(row["created_at"]) < before
        and is_eligible(row)
    ]
    runs.sort(key=lambda row: (timestamp(row["created_at"]), row["id"]), reverse=True)
    return runs


def read_junit(root):
    """(failing, passing, covered) from junit-<job>/*.xml under root: sets of
    (classname::name, job) pairs, and the jobs that published any testcase."""
    failing, passing, covered = set(), set(), set()
    if not os.path.isdir(root):
        return failing, passing, covered
    lane_map = {}
    mapping = os.path.join(root, ".run-store-lanes.json")
    if os.path.isfile(mapping):
        with open(mapping) as fh:
            lane_map = json.load(fh)
    for dirpath, _dirs, files in os.walk(root):
        lane = os.path.basename(dirpath)
        lane = lane[len("junit-"):] if lane.startswith("junit-") else lane
        entry = os.path.relpath(dirpath, root).split(os.sep)[0]
        lane = lane_map.get(entry, lane)
        for filename in files:
            if not filename.endswith(".xml"):
                continue
            try:
                tree = ElementTree.parse(os.path.join(dirpath, filename))
            except Exception:
                continue
            for case in tree.iter("testcase"):
                name = " ".join((case.get("name") or "").split())[:300]
                klass = " ".join((case.get("classname") or "").split())[:300]
                full = f"{klass}::{name}" if klass else name
                if not full:
                    continue
                kinds = {child.tag for child in case}
                covered.add(lane)
                if kinds & {"failure", "error"}:
                    failing.add((full, lane))
                elif "skipped" not in kinds:
                    passing.add((full, lane))
    return failing, passing, covered


class Evidence:
    """What one earlier run shows: its job conclusions, and (read only when a
    unit needs it) its junit."""

    def __init__(self, jobs, junit):
        self.jobs = jobs
        self._junit = junit
        self._cache = None

    def junit(self):
        if self._cache is None:
            self._cache = self._junit()
        return self._cache


def clears(unit, evidence):
    kind, name, job = unit
    conclusion = evidence.jobs.get(job)
    if conclusion == "success":
        return True
    if conclusion not in FAILED or kind != "test":
        return False
    failing, passing, covered = evidence.junit()
    if job not in covered or (name, job) in failing:
        return False
    if any(lane == job for _name, lane in passing):
        return (name, job) in passing
    # The producer lists failures only: a test it does not list did not fail.
    return True


def find_window(units, runs, load, limit=WALK_RUNS):
    """(run, cleared units, runs read) for the newest run that clears any unit."""
    read = 0
    for run in runs[:limit]:
        read += 1
        evidence = load(run)
        cleared = [unit for unit in units if clears(unit, evidence)]
        if cleared:
            return run, cleared, read
    return None, [], read


def judge(targets, failing):
    seen = {(kind, name, job) for kind, name, job in failing}
    jobs = {job for _kind, _name, job in failing}
    # A job that failed without junit (a build error, an earlier step)
    # can hide any test of it; junit lists a job's failing tests, so
    # another failing test of the job hides nothing.
    lanes = {job for kind, _name, job in failing if kind == "lane"}
    hidden = None
    for kind, name, job in sorted(targets):
        if (kind, name, job) in seen:
            what = f"{name} ({job})" if kind == "test" else f"job {job} ({name})"
            return f"fail the target {what} failed the same way in the candidate"
        if hidden is None and job in (lanes if kind == "test" else jobs):
            hidden = job
    if hidden is not None:
        return (f"inconclusive the target job {hidden} failed in the candidate, "
                "but not the same way, so the target may not have run")
    return "pass none of the target units failed in the candidate"


def live_loader(proof, repo):
    def load(run):
        try:
            rows = proof.paged(f"repos/{repo}/actions/runs/{run['id']}/jobs?filter=latest", "jobs")
        except Exception:
            rows = []
        jobs = {}
        for row in rows:
            name = row.get("name")
            if (isinstance(name, str) and name.strip() and row.get("run_id") == run["id"]
                    and row.get("status") == "completed"):
                jobs[name.replace(",", ";")] = row.get("conclusion")

        def junit():
            work = tempfile.mkdtemp(prefix="junit-window-")
            try:
                env = dict(os.environ)
                env["GH_TOKEN"] = env.get("ACTIONS_TOKEN") or env.get("GH_TOKEN", "")
                subprocess.run(["python3", env["RUN_STORE_JUNIT"], repo, str(run["id"]), work],
                               env=env, capture_output=True, timeout=600)
                return read_junit(work)
            except Exception:
                return set(), set(), set()
            finally:
                shutil.rmtree(work, ignore_errors=True)

        return Evidence(jobs, junit)
    return load


def main():
    command = sys.argv[1]
    if command == "judge":
        print(judge(read_units(sys.argv[2]), read_units(sys.argv[3])))
        return
    repo, branch, workflow, created, aggregate, confirmed, out = sys.argv[2:9]
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import proof
    units = [u for u in read_units(confirmed) if u[2] != aggregate]
    if not units:
        print("none no confirmed unit besides the aggregate job")
        return
    runs = earlier_runs(proof.branch_history(repo, branch, workflow), created,
                        lambda row: proof.producer_origin(row, repo, branch, workflow) == "eligible")
    run, cleared, read = find_window(units, runs, live_loader(proof, repo))
    if run is None:
        print(f"none no earlier completed run clears a failing unit (read {read} of {len(runs)})")
        return
    with open(out, "w", encoding="utf-8") as fh:
        for kind, name, job in cleared:
            fh.write(f"{kind}\t{name}\t{job}\n")
    print(f"base {run['head_sha']} {run['id']} {len(cleared)} (read {read} run(s))")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"none the per-unit window is unavailable: {error}")
