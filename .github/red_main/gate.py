import sys

path, has_file = sys.argv[1:3]

import re

OWNER = "owner:ops"
# Rule 1: a commit that touches these paths is never auto-reverted.
# The defaults are unanchored (any directory named migrations, migrate
# or drizzle, any atlas.sum, prisma/migrations): over-holding is the
# safe direction. `[ci] migration_paths`, a comma-separated list of
# globs (`**` crosses directories), ADDS to them.
MIGRATIONS = "**/migrations/**,**/migrate/**,**/atlas.sum,**/drizzle/**,**/prisma/migrations/**"

def out(verdict, mode, basis, owner=OWNER, paths=MIGRATIONS, hold=""):
    # An empty field would collapse in the shell's tab split, so an
    # empty hold is written `-`.
    print(f"{verdict}\t{mode}\t{owner}\t{paths}\t{hold or '-'}\t{basis}")

if has_file != "yes":
    # Silence is never consent: a repository without a manifest has
    # declared nothing, and this says how one declares.
    out(
        "off",
        "",
        'no sylphx.toml; a repository that wants verified commits adopts a minimal manifest '
        '(version = "1" and [ci] merge = "optimistic", on_red = "notify" or "revert")',
    )
    raise SystemExit(0)

try:
    import tomllib
except ImportError:
    tomllib = None

ci = None
try:
    raw = open(path, "rb").read()
    if tomllib is not None:
        ci = tomllib.loads(raw.decode("utf-8")).get("ci")
        if not isinstance(ci, dict):
            # A manifest that parses but declares no [ci] table has
            # declared nothing, which is not the same as unreadable.
            ci = {}
    else:
        # The manifest is flat key = value TOML; read the [ci] table
        # line by line when tomllib is unavailable.
        ci = {}
        table = None
        for line in raw.decode("utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            if line.startswith("["):
                table = line.strip("[]").strip()
                continue
            if table == "ci" and "=" in line:
                key, value = line.split("=", 1)
                ci[key.strip()] = value.strip().strip('"')
except Exception:
    ci = None

if not isinstance(ci, dict):
    out("off", "", "sylphx.toml could not be read")
elif ci.get("merge") != "optimistic":
    # Absent or anything else: the repository has not opted in.
    out("off", "", f"sylphx.toml [ci] merge is {ci.get('merge')!r}, not optimistic")
else:
    on_red = ci.get("on_red") or "revert"
    if not isinstance(on_red, str) or on_red not in ("notify", "revert", "revert_pr_unarmed"):
        out("error", "", f"sylphx.toml [ci] on_red must be notify, revert or revert_pr_unarmed, not {on_red!r}")
    else:
        # The label the handler's alerts carry; a repository names its
        # own owner label, anything malformed falls back to owner:ops.
        # A value that is not a string (a TOML array or number) falls
        # back to the default: never a crash.
        owner = ci.get("red_main_owner")
        if not isinstance(owner, str) or not re.fullmatch(r"owner:[a-z0-9-]+", owner):
            owner = OWNER
        extra = ci.get("migration_paths")
        paths = MIGRATIONS
        if isinstance(extra, str) and re.fullmatch(r"[A-Za-z0-9_./*,-]+", extra):
            paths = MIGRATIONS + "," + extra
        # `revert_hold_utc = "19:00-22:00"`: inside the window a revert
        # is opened but never armed (a deploy freeze).
        # A start equal to the end is an empty window.
        hold = ci.get("revert_hold_utc")
        if not isinstance(hold, str) or not re.fullmatch(
            r"([01][0-9]|2[0-3]):[0-5][0-9]-([01][0-9]|2[0-3]):[0-5][0-9]", hold, re.ASCII
        ):
            hold = ""
        out("act", on_red, f"sylphx.toml [ci] merge=optimistic on_red={on_red}", owner, paths, hold)
