import re
import sys

def to_regex(glob):
    out, i = "", 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif glob.startswith("**", i):
            out += ".*"
            i += 2
        elif glob[i] == "*":
            out += "[^/]*"
            i += 1
        elif glob[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(glob[i])
            i += 1
    return re.compile(out + r"\Z")

patterns = [to_regex(g.strip().lstrip("/")) for g in sys.argv[1].split(",") if g.strip()]
for line in sys.stdin:
    name = line.strip()
    if name and any(rx.match(name) for rx in patterns):
        print(f"migration {name}")
        raise SystemExit(0)
print("none")
