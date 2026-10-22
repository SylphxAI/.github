import os
import sys

prev_conclusion, prev_path, cur_path = sys.argv[1:4]
aggregate = os.environ.get("VERIFY_CHECK_NAME") or "verified"

def read(path):
    rows = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) == 3 and parts[2] != aggregate:
                    rows.append(tuple(parts))
    except OSError:
        pass
    return rows

if prev_conclusion not in ("failure", "timed_out"):
    print(f"no the previous completed run was '{prev_conclusion or 'not found'}'")
    raise SystemExit(0)
prev, cur = read(prev_path), read(cur_path)

def units(rows, kind):
    return {(name, job) for k, name, job in rows if k == kind}

def jobs(rows):
    return {job for _kind, _name, job in rows}

common_tests = units(prev, "test") & units(cur, "test")
common_lanes = units(prev, "lane") & units(cur, "lane")
# The units of THIS run that the previous run confirms: the same test,
# or the same job failing the same way; the trace reads them from the
# optional fourth argument.
if len(sys.argv) > 4:
    confirmed = [(kind, name, job) for kind, name, job in cur
                 if (name, job) in units(prev, kind)]
    with open(sys.argv[4], "w", encoding="utf-8") as fh:
        for row in confirmed:
            fh.write("\t".join(row) + "\n")
if common_tests:
    name, job = sorted(common_tests)[0]
    print(f"yes the same unit failed on both runs: {name} ({job})")
elif common_lanes:
    name, job = sorted(common_lanes)[0]
    print(f"yes the same job failed the same way on both runs: {job} ({name})")
elif jobs(prev) & jobs(cur):
    job = sorted(jobs(prev) & jobs(cur))[0]
    before = next(name for _kind, name, j in prev if j == job)
    now = next(name for _kind, name, j in cur if j == job)
    print(f"no {job} failed on both runs, but not the same way (previous run: {before}; this run: {now})")
else:
    print("no the previous completed run failed a different unit")
