import sys

rows = []
for line in sys.stdin:
    parts = line.rstrip("\n").split("\t")
    if parts[0]:
        rows.append((parts[0], parts[1] if len(parts) > 1 else ""))

# The first candidate that is not a success decides: a failure with
# only passing ancestors is the culprit; anything else is inconclusive
# and the oldest failing commit cannot be named with certainty.
for sha, conclusion in rows:
    if conclusion == "success":
        continue
    if conclusion == "failure":
        print(f"culprit {sha}")
    else:
        print("window")
        print(f"why {sha[:9]} concluded {conclusion or 'nothing'}, which is inconclusive, at or before the first failure")
    raise SystemExit(0)
print("none")
