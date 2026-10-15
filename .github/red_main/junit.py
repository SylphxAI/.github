import json
import os
import sys
import xml.etree.ElementTree as ElementTree

root, out, lanes = sys.argv[1], sys.argv[2], [x for x in sys.argv[3].split(",") if x]
rows, seen, covered = [], set(), set()

def clean(value):
    return " ".join((value or "").split())[:300]

signatures = {}
if len(sys.argv) > 4:
    with open(sys.argv[4], encoding="utf-8") as fh:
        for line in fh:
            lane, _, signature = line.rstrip("\n").partition("\t")
            if lane and signature:
                signatures[clean(lane)] = clean(signature)

lane_map = {}
mapping = os.path.join(root, ".run-store-lanes.json")
if os.path.isfile(mapping):
    with open(mapping) as fh:
        lane_map = json.load(fh)
if os.path.isdir(root):
    for dirpath, dirnames, filenames in os.walk(root):
        for filename in filenames:
            if not filename.endswith(".xml"):
                continue
            lane = os.path.basename(dirpath)
            lane = lane[len("junit-"):] if lane.startswith("junit-") else lane
            entry = os.path.relpath(dirpath, root).split(os.sep)[0]
            lane = lane_map.get(entry, lane)
            try:
                tree = ElementTree.parse(os.path.join(dirpath, filename))
            except Exception:
                continue
            for case in tree.iter("testcase"):
                if not [c for c in case if c.tag in ("failure", "error")]:
                    continue
                name = clean(case.get("name"))
                klass = clean(case.get("classname"))
                full = f"{klass}::{name}" if klass else name
                if not full or (full, lane) in seen:
                    continue
                seen.add((full, lane))
                covered.add(lane)
                rows.append(("test", full, lane))
for lane in lanes:
    if lane not in covered and (lane, lane) not in seen:
        seen.add((lane, lane))
        rows.append(("lane", signatures.get(clean(lane), clean(lane)), clean(lane)))
with open(out, "w", encoding="utf-8") as fh:
    for kind, name, lane in rows:
        fh.write(f"{kind}\t{name}\t{lane}\n")
print(f"{len(rows)} failing unit(s), {len(lanes)} failed lane(s)")
