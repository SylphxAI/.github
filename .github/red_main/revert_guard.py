import json
import os
import sys

pr = json.load(sys.stdin)
labels = [x.strip().lower() for x in os.environ.get("PROTECTED_LABELS", "").split(",") if x.strip()]
numbers = {x.strip().lstrip("#") for x in os.environ.get("PROTECTED_PRS", "").split(",") if x.strip()}
if str(pr.get("number", "")) in numbers:
    print(f"protected: #{pr['number']} is on the never-revert list")
    raise SystemExit(0)
for label in pr.get("labels", []):
    low = str(label).lower()
    if low in labels or low.endswith(":p0"):
        print(f"protected: #{pr.get('number')} carries the label {label}")
        raise SystemExit(0)
print("ok")
