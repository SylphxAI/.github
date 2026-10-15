import sys

window, now = sys.argv[1:3]
if not window:
    print("out")
    raise SystemExit(0)
start, end = window.split("-")
if start <= end:
    inside = start <= now < end
else:
    inside = now >= start or now < end
print("in" if inside else "out")
