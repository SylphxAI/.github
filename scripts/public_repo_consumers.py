#!/usr/bin/env python3
"""public_repo_consumers.py -- list public SylphxAI repositories with no consumer, and shut-down ones that do not say so.

We publish what we use ourselves. Each public, unarchived repository carries the
organization custom property `sylphx_consumer`: the repository path or desk
configuration that runs it. A public repository without it is a tool nobody in
the company uses, and goes to a keep, merge or archive decision
(docs/public-repo-consumers.md).

A shut-down project tells its users (TODO Group shutdown guide; npm deprecate;
pub.dev discontinued): every archived public repository carries a status line in
its description or README, and every package published from it is deprecated
(npm), discontinued (pub.dev) or yanked (crates.io).

Delivered customer projects (`sylphx_delivery` = `delivered`) are skipped: they
are hands-off.

Usage:
  scripts/public_repo_consumers.py [--org SylphxAI] [--json] [--input repos.json]
      [--packages packages.json] [--readmes readmes.json] [--fix-npm]

Reads the organization's repository list with `gh api --paginate` (each repo
object carries `custom_properties`), or a saved list with --input; the npm,
pub.dev and crates.io package lists from the registries' public APIs, or a saved
list with --packages; README text with `gh api repos/<org>/<repo>/readme`, or a
saved map with --readmes. --fix-npm runs `npm deprecate` on each listed npm
package (needs an npm token with write access) and reports what is left.

Exit: 0 nothing listed, 1 at least one repository or package is listed
(MISSING, NO-NOTICE or UNDEPRECATED lines), 2 a list or the properties could not
be read (never reported as green).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

PROPERTY = "sylphx_consumer"

# A status line: the project is archived, retired, moved or replaced.
NOTICE = re.compile(
    r"\b(archived|deprecated|discontinued|retired|unmaintained|no longer (maintained|developed|supported)"
    r"|not maintained|superseded|merged into|moved to|replaced by|end[- ]of[- ]life|sunset)\b",
    re.IGNORECASE,
)
README_HEAD = 4000  # a notice belongs at the top of the README

NPM = "https://registry.npmjs.org"
NPM_ORG = "sylphx"
NPM_MAINTAINERS = ("shtse8", "ansonsylphx")
PUB = "https://pub.dev/api"
PUB_PUBLISHER = "sylphx.com"
CRATES = "https://crates.io/api/v1"
CRATES_OWNER_IDS = (433750,)  # crates.io user shtse8, the owner of the SylphxAI crates
USER_AGENT = "SylphxAI-public-repo-check (https://github.com/SylphxAI/.github)"
GITHUB_URL = re.compile(r"github\.com[/:]([^/\s#]+)/([^/\s#]+?)(?:\.git)?(?:[/#].*)?$", re.IGNORECASE)


class Unreadable(Exception):
    """The repository list or its custom properties could not be read."""


def fetch(org: str) -> list[dict]:
    cmd = ["gh", "api", "--paginate", "--slurp", f"orgs/{org}/repos?type=all&per_page=100"]
    try:
        out = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=120).stdout
        pages = json.loads(out)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise Unreadable(f"cannot list {org} repositories: {exc}") from exc
    return [repo for page in pages for repo in page]


def missing_consumers(repos: list[dict]) -> list[dict]:
    """The public, unarchived, not delivered repositories with no `sylphx_consumer` value, by name.

    A credential that cannot read custom properties gets an empty map for every
    repository; when no public repository shows any custom property at all, the
    read is treated as failed rather than flagging everything."""
    public = [r for r in repos if r.get("visibility") == "public" and not r.get("archived") and not r.get("disabled")]
    if not public:
        raise Unreadable("no public repositories in the list")
    if not any(r.get("custom_properties") for r in public):
        raise Unreadable("no public repository shows custom properties: the credential cannot read them")
    out = []
    for r in public:
        props = r.get("custom_properties") or {}
        if props.get("sylphx_delivery") == "delivered":
            continue
        if not str(props.get(PROPERTY) or "").strip():
            out.append({"name": r["name"], "fork": bool(r.get("fork")), "pushed_at": r.get("pushed_at"),
                        "url": r.get("html_url")})
    return sorted(out, key=lambda r: r["name"].lower())


def archived_public(repos: list[dict]) -> list[dict]:
    """The archived, public, not forked, not delivered repositories: projects we shut down."""
    return [r for r in repos if r.get("visibility") == "public" and r.get("archived") and not r.get("fork")
            and (r.get("custom_properties") or {}).get("sylphx_delivery") != "delivered"]


def has_notice(text: str | None) -> bool:
    return bool(text) and bool(NOTICE.search(text))


def missing_notices(archived: list[dict], readme: Callable[[str], str | None]) -> list[dict]:
    """The archived repositories whose description and README head both lack a status line, by name.

    The README is read only when the description has no status line."""
    out = []
    for r in archived:
        if has_notice(r.get("description")):
            continue
        if has_notice((readme(r["name"]) or "")[:README_HEAD]):
            continue
        out.append({"name": r["name"], "description": r.get("description") or "", "url": r.get("html_url")})
    return sorted(out, key=lambda r: r["name"].lower())


def github_repo(url: str | None) -> tuple[str, str] | None:
    """(owner, name) of a GitHub repository URL in any of the forms registries carry."""
    m = GITHUB_URL.search(url or "")
    return (m.group(1), m.group(2)) if m else None


def undeprecated(packages: list[dict], org: str, archived: list[dict],
                 resolve: Callable[[str, str], str | None]) -> list[dict]:
    """The packages whose source is an archived repository and that are not marked deprecated.

    A package names its source in `repo_url`. A URL under another owner (a repository renamed or
    transferred into the organization) is resolved through GitHub's redirect, and only when its
    name matches an archived repository, so unrelated packages cost no API call."""
    names = {r["name"].lower(): r["name"] for r in archived}
    out = []
    for p in packages:
        if p.get("marked"):
            continue
        ref = github_repo(p.get("repo_url"))
        if not ref or ref[1].lower() not in names:
            continue
        owner, name = ref
        if owner.lower() != org.lower():
            target = resolve(owner, name)
            if not target or target.split("/")[0].lower() != org.lower():
                continue
            name = target.split("/", 1)[1]
            if name.lower() not in names:
                continue
        out.append({"registry": p["registry"], "name": p["name"], "repo": names[name.lower()]})
    return sorted(out, key=lambda p: (p["registry"], p["name"].lower()))


def http_json(url: str) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    last: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            last = exc
        except (OSError, ValueError) as exc:
            last = exc
        time.sleep(1 + attempt)
    raise Unreadable(f"cannot read {url}: {last}")


def npm_packages() -> list[dict]:
    """Every npm package in the organization scope or kept by its maintainers: name, source, deprecated."""
    org = http_json(f"{NPM}/-/org/{NPM_ORG}/package")
    if not isinstance(org, dict) or not org:
        raise Unreadable(f"npm organization {NPM_ORG} lists no packages")
    names = set(org)
    for user in NPM_MAINTAINERS:
        found = http_json(f"{NPM}/-/v1/search?text=maintainer:{user}&size=250") or {}
        names.update(o["package"]["name"] for o in found.get("objects", []))

    def one(name: str) -> dict | None:
        doc = http_json(f"{NPM}/{urllib.parse.quote(name, safe='@')}/latest")
        if not isinstance(doc, dict):
            return None
        repo = doc.get("repository")
        url = repo.get("url") if isinstance(repo, dict) else repo
        return {"registry": "npm", "name": name, "repo_url": url or doc.get("homepage"),
                "marked": bool(doc.get("deprecated"))}

    with ThreadPoolExecutor(max_workers=8) as pool:
        return [p for p in pool.map(one, sorted(names)) if p]


def pub_packages() -> list[dict]:
    """Every pub.dev package of the publisher: name, source, discontinued."""
    names, url = [], f"{PUB}/search?q=publisher:{PUB_PUBLISHER}"
    while url:
        page = http_json(url) or {}
        names += [p["package"] for p in page.get("packages", [])]
        url = page.get("next")
    out = []
    for name in names:
        doc = http_json(f"{PUB}/packages/{name}") or {}
        opts = http_json(f"{PUB}/packages/{name}/options") or {}
        spec = (doc.get("latest") or {}).get("pubspec") or {}
        out.append({"registry": "pub", "name": name, "repo_url": spec.get("repository") or spec.get("homepage"),
                    "marked": bool(opts.get("isDiscontinued"))})
    return out


def crate_packages() -> list[dict]:
    """Every crates.io crate of the owners: name, source, and whether its default version is yanked."""
    out = []
    for uid in CRATES_OWNER_IDS:
        page_no = 1
        while True:
            page = http_json(f"{CRATES}/crates?user_id={uid}&per_page=100&page={page_no}") or {}
            crates = page.get("crates", [])
            out += [{"registry": "crates", "name": c["name"], "repo_url": c.get("repository") or c.get("homepage"),
                     "marked": bool(c.get("yanked"))} for c in crates]
            if len(crates) < 100:
                break
            page_no += 1
            time.sleep(1)  # crates.io asks for at most one request per second
    return out


def gh_readme(org: str) -> Callable[[str], str | None]:
    calls = {"n": 0}

    def read(name: str) -> str | None:
        calls["n"] += 1
        if calls["n"] > 50:
            time.sleep(1)  # pace long runs of GitHub reads
        cmd = ["gh", "api", "-H", "Accept: application/vnd.github.raw", f"repos/{org}/{name}/readme"]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            raise Unreadable(f"cannot read the {name} README: {exc}") from exc
        if res.returncode == 0:
            return res.stdout
        if "404" in res.stderr:
            return None
        raise Unreadable(f"cannot read the {name} README: {res.stderr.strip()[:200]}")

    return read


def gh_resolve(owner: str, name: str) -> str | None:
    """owner/name of a repository after GitHub's rename or transfer redirect, or None."""
    try:
        res = subprocess.run(["gh", "api", f"repos/{owner}/{name}", "--jq", ".full_name"],
                             capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    return res.stdout.strip() or None


def deprecate_npm(packages: list[dict], org: str) -> list[dict]:
    """Run `npm deprecate` on each listed npm package; return the ones it could not mark."""
    left = []
    for p in packages:
        if p["registry"] != "npm":
            left.append(p)
            continue
        msg = f"No longer maintained: the source repository github.com/{org}/{p['repo']} is archived."
        res = subprocess.run(["npm", "deprecate", p["name"], msg], capture_output=True, text=True, timeout=120)
        if res.returncode == 0:
            print(f"DEPRECATED npm {p['name']}")
        else:
            print(f"public-repo-consumers: npm deprecate {p['name']} failed: {res.stderr.strip()[:200]}",
                  file=sys.stderr)
            left.append(p)
    return left


def load(path: str) -> object:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--org", default="SylphxAI")
    ap.add_argument("--input", help="a saved repository list (JSON array, or an array of pages)")
    ap.add_argument("--packages", help="a saved package list (JSON array of {registry, name, repo_url, marked})")
    ap.add_argument("--readmes", help="a saved README map (JSON object: repository name -> text)")
    ap.add_argument("--fix-npm", action="store_true", help="npm deprecate each listed npm package")
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    args = ap.parse_args(argv)
    try:
        if args.input:
            data = load(args.input)
            repos = [r for page in data for r in page] if data and isinstance(data[0], list) else data
        else:
            repos = fetch(args.org)
        missing = missing_consumers(repos)
        archived = archived_public(repos)
        if args.readmes:
            readmes = load(args.readmes)
            readme = readmes.get
        else:
            readme = gh_readme(args.org)
        no_notice = missing_notices(archived, readme)
        packages = load(args.packages) if args.packages else npm_packages() + pub_packages() + crate_packages()
        unmarked = undeprecated(packages, args.org, archived, gh_resolve)
    except (Unreadable, OSError, ValueError) as exc:
        print(f"public-repo-consumers: UNREADABLE: {exc}", file=sys.stderr)
        return 2
    if args.fix_npm and unmarked:
        unmarked = deprecate_npm(unmarked, args.org)
    if args.json:
        print(json.dumps({"org": args.org, "property": PROPERTY, "missing": missing,
                          "archived_without_notice": no_notice, "undeprecated_packages": unmarked}, indent=2))
    else:
        for r in missing:
            print(f"MISSING {args.org}/{r['name']}{' (fork)' if r['fork'] else ''} pushed {r['pushed_at']}")
        for r in no_notice:
            print(f"NO-NOTICE {args.org}/{r['name']} archived with no status line in description or README")
        for p in unmarked:
            print(f"UNDEPRECATED {p['registry']} {p['name']} from archived {args.org}/{p['repo']}")
        print(f"public-repo-consumers: {len(missing)} public {args.org} repositories with no {PROPERTY}, "
              f"{len(no_notice)} archived with no notice, {len(unmarked)} packages from archived repositories "
              f"not deprecated")
    return 1 if missing or no_notice or unmarked else 0


if __name__ == "__main__":
    sys.exit(main())
