#!/usr/bin/env python3
"""Report every repository against the Rust compile-cache rows C1-C3.

Contract: docs/adr/0004-fleet-rust-compile-cache.md and docs/rust-ci.md.
Policy: policy/rust-cache.json (the pin floor and the named exemptions).
Read-only: GraphQL queries and compare reads, never a write. The workflow
reading and pin comparison are the optimistic-merge audit's
(scripts/audit_optimistic_merge.py), reused here.

Rows, per non-archived repository of the policy's organizations whose
workflows compile Rust on a Sylphx Linux runner (a "Rust job"):

  C1   every Rust job reaches the shared cache: it calls rust-ci.yml or
       rust-check.yml, or runs the rust-sccache action pinned (full SHA) at or
       after the policy floor
  C2   the namespace is `rustc`: no key-prefix other than `rustc` on a
       rust-sccache step or a rust-ci.yml / rust-check.yml call, and no unset
       key-prefix on a pin that predates the `rustc` default
  C3   every workflow with a Rust job that runs on pull_request or merge_group
       also runs on push to the default branch (directly or through a local
       workflow call), so main warms the cache pull requests read

A row is PASS, FAIL, EXEMPT (waived by a named, unexpired exemption) or SKIP
(nothing to check). Anything the audit could not read is a FAIL, never a pass.
Exit 1 on any FAIL; the JSON report (--json) lists every repository and row.

Out of scope (ADR 0004): jobs on GitHub-hosted, macOS or Windows runners (the
action does nothing there), Rust compiled inside container image builds, and
Rust reached only through a wrapper the workflow text does not show (a script,
`make`, a local composite action). Hands-off and exempt repositories are listed
from their name only: their files are never read.
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import audit_optimistic_merge as om  # noqa: E402  (same reader, same conventions)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "policy" / "rust-cache.json"
ROWS = ("C1", "C2", "C3")
NAMESPACE = "rustc"
SHARED_WORKFLOW = re.compile(r"^SylphxAI/\.github/\.github/workflows/rust-(?:ci|check)\.yml@\S+$")
SCCACHE_ACTION = re.compile(r"^SylphxAI/\.github/\.github/actions/rust-sccache@(\S+)$")
LOCAL_SCCACHE = "./.github/actions/rust-sccache"
COMPILE = re.compile(
    r"\bcargo(?:\s+\+\S+)?\s+(?:build|b|test|t|check|c|clippy|nextest|run|r|bench|doc|rustc|install|llvm-cov|"
    r"tarpaulin|ndk|zigbuild|xwin|chef|xtask|hack|miri|package|publish)\b")
CACHE_ACTIONS = re.compile(r"Swatinem/rust-cache|actions/rust-sccache\b|mozilla-actions/sccache-action")
GH_HOSTED = re.compile(r"\b(?:ubuntu|macos|windows)[-\w.]*", re.I)


# --- policy -----------------------------------------------------------------------------


def load_policy(path: Path) -> dict:
    policy = om.load_policy(path)  # floor, personal owners, exemption fields and dates
    default = policy.get("rustc_default_sha", "")
    if default and not om.SHA40.match(default):
        raise ValueError("policy rustc_default_sha must be empty or a full 40-hex commit SHA")
    return policy


# --- workflow reading --------------------------------------------------------------------


def _clean(value: str) -> str:
    return value.split(" #", 1)[0].strip().strip("'\"")


def job_key(body: list[str], key: str) -> tuple[str | None, int]:
    """The value and line index of a job-level key (a key at the job body's own indent)."""
    if not body:
        return None, -1
    base = min(om._indent(line) for line in body)
    for i, line in enumerate(body):
        if om._indent(line) == base:
            match = re.match(rf"^\s*{re.escape(key)}\s*:\s*(.*?)\s*$", line)
            if match:
                return _clean(match.group(1)), i
    return None, -1


def key_block(body: list[str], key: str) -> str:
    value, i = job_key(body, key)
    if value is None:
        return ""
    return " ".join([value, *(line.strip() for line in om._sub_block(body, i))])


def steps(body: list[str]) -> list[list[str]]:
    _, i = job_key(body, "steps")
    block = om._sub_block(body, i) if i >= 0 else []
    if not block:
        return []
    item = om._indent(block[0])
    out: list[list[str]] = []
    for line in block:
        if om._indent(line) == item and line.lstrip().startswith("-"):
            out.append([])
        if out:
            out[-1].append(line)
    return out


def step_uses(step: list[str]) -> str:
    for line in step:
        match = re.match(r"^\s*(?:-\s+)?uses\s*:\s*(\S+)", line)
        if match and om._indent(line) <= om._indent(step[0]) + 2:
            return match.group(1)
    return ""


def key_prefix_of(lines: list[str]) -> str | None:
    for line in lines:
        match = re.match(r"^\s*(?:-\s+)?key-prefix\s*:\s*(.*?)\s*$", line)
        if match:
            return _clean(match.group(1))
    return None


def input_default(text: str, name: str) -> str | None:
    lines = om._code_lines(text)
    for i, line in enumerate(lines):
        if re.match(rf"^\s+{re.escape(name)}\s*:\s*$", line):
            for sub in om._sub_block(lines, i):
                match = re.match(r"^\s*default\s*:\s*(.*?)\s*$", sub)
                if match:
                    return _clean(match.group(1))
    return None


def resolve_prefix(raw: str, text: str) -> str:
    """A literal, or `${{ inputs.x }}` resolved to that input's default in the same workflow."""
    match = re.fullmatch(r"\$\{\{\s*inputs\.([\w-]+)\s*\}\}", raw)
    if match:
        default = input_default(text, match.group(1))
        return default if default is not None else raw
    return raw


def sylphx_linux(body: list[str]) -> bool:
    """Does the job run on a Sylphx Linux runner (or one we cannot tell apart from it)?"""
    runs_on = key_block(body, "runs-on")
    if "matrix." in runs_on:
        runs_on += " " + key_block(body, "strategy")
    low = runs_on.lower()
    if "macos" in low or "windows" in low:
        return False
    if "sylphx" in low:
        return True
    return not GH_HOSTED.search(runs_on)  # only GitHub-hosted labels are out; doubt is in


def local_calls(text: str) -> set[str]:
    return set(re.findall(r"uses\s*:\s*\./\.github/workflows/([\w.-]+)", "\n".join(om._code_lines(text))))


def closure(workflows: dict[str, str], roots: set[str]) -> set[str]:
    """The roots and every local workflow they call, transitively."""
    seen, todo = set(), list(roots)
    while todo:
        name = todo.pop()
        if name in seen or name not in workflows:
            continue
        seen.add(name)
        todo.extend(local_calls(workflows[name]))
    return seen


# --- evaluate: pure, tested on fixtures ----------------------------------------------------


def _row(status: str, detail: str = "") -> dict:
    return {"status": status, "detail": detail}


def rust_jobs(workflows: dict[str, str]) -> list[dict]:
    """Every Rust job on a Sylphx Linux runner: {workflow, job, body, text, call, steps}."""
    out = []
    for name, text in sorted(workflows.items()):
        for job_id, body in om.jobs(text).items():
            uses, _ = job_key(body, "uses")
            call = bool(uses and SHARED_WORKFLOW.match(uses))
            joined = "\n".join(body)
            if not (call or COMPILE.search(joined) or CACHE_ACTIONS.search(joined)):
                continue
            if not call and not sylphx_linux(body):
                continue
            out.append({"workflow": name, "job": job_id, "body": body, "text": text, "call": call})
    return out


def evaluate_rows(facts: dict, policy: dict, compare: om.Comparer, compare_default: om.Comparer | None) -> dict[str, dict]:
    """The three rows of one repository from its facts (no network)."""
    workflows = facts.get("workflows")
    errors = facts.get("errors") or []
    if workflows is None or errors:
        why = "; ".join(errors[:2]) or "workflow files unreadable"
        return {name: _row("FAIL", "unreadable: " + why) for name in ROWS}
    jobs = rust_jobs(workflows)
    if not jobs:
        return {name: _row("SKIP", "no Rust job on a Sylphx Linux runner") for name in ROWS}
    branch = facts["branch"]
    local_ok = facts["repo"] == policy["pin_floor"]["repo"]

    unshared, bad_pin, bad_prefix = [], [], []
    for job in jobs:
        label = f"{job['workflow']}:{job['job']}"
        if job["call"]:
            raw = key_prefix_of(job["body"])
            if raw is not None and resolve_prefix(raw, job["text"]) != NAMESPACE:
                bad_prefix.append(f"{label} passes key-prefix `{raw}`")
            continue
        found = False
        for step in steps(job["body"]):
            uses = step_uses(step)
            pin = None
            if uses == LOCAL_SCCACHE and local_ok:
                pin = "local"
            else:
                match = SCCACHE_ACTION.match(uses)
                if match:
                    pin = match.group(1)
            if pin is None:
                continue
            found = True
            if pin != "local":
                ok, why = compare.at_or_after(pin)
                if ok is not True:
                    bad_pin.append(f"{label}: {why}")
            raw = key_prefix_of(step)
            if raw is not None:
                value = resolve_prefix(raw, job["text"])
                if value != NAMESPACE:
                    bad_prefix.append(f"{label} sets key-prefix `{raw}`")
            elif pin != "local":
                ok, why = (compare_default.at_or_after(pin) if compare_default else
                           (False, "no rustc-default commit in the policy yet"))
                if ok is not True:
                    bad_prefix.append(f"{label} leaves key-prefix unset on pin {pin[:9]} ({why or 'unreadable'}); set `key-prefix: rustc`")
        if not found:
            unshared.append(label)

    rows: dict[str, dict] = {}
    c1 = ([f"{label} compiles Rust without rust-sccache or a rust-ci.yml / rust-check.yml call" for label in unshared]
          + bad_pin)
    rows["C1"] = _row("FAIL", "; ".join(c1)) if c1 else _row("PASS", f"{len(jobs)} Rust job(s) on the shared cache")
    rows["C2"] = _row("FAIL", "; ".join(bad_prefix)) if bad_prefix else _row("PASS", f"namespace {NAMESPACE}")

    gate = closure(workflows, {n for n, t in workflows.items()
                               if {"pull_request", "merge_group"} & set(om.triggers(t))})
    warm = closure(workflows, {n for n, t in workflows.items() if om.push_covers(t, branch)})
    cold = sorted({j["workflow"] for j in jobs if j["workflow"] in gate and j["workflow"] not in warm})
    rows["C3"] = (_row("FAIL", f"{', '.join(cold)} runs Rust on pull requests but never on push to {branch}, so nothing warms the cache")
                  if cold else _row("PASS", f"every gate workflow with Rust also runs on push to {branch}"))
    return rows


def evaluate(fleet: dict, policy: dict, compare: om.Comparer, today: datetime.date) -> dict:
    """The whole report from collected facts: {org: [repo facts]}."""
    default = policy.get("rustc_default_sha") or ""
    compare_default = om.Comparer(default, compare.read) if default else None
    repos = []
    for org, items in fleet.items():
        for facts in items:
            entry, lapsed = om.exemption_for(policy, facts["repo"], today)
            record = {"repo": facts["repo"], "branch": facts.get("branch"), "exemption": None}
            if lapsed:
                record["note"] = lapsed
            if facts.get("unlisted"):
                record.update(status="FAIL", rows={}, failing=[], errors=facts["errors"])
            elif entry:
                record.update(status="EXEMPT", rows={n: _row("EXEMPT", f"{entry['class']}: {entry['reason']}") for n in ROWS},
                              failing=[], exemption={k: entry[k] for k in ("class", "reason", "owner", "review")})
            elif facts.get("fork"):
                record.update(status="EXEMPT", rows={}, failing=[],
                              exemption={"class": "fork", "reason": "a fork follows its upstream"})
            else:
                rows = evaluate_rows(facts, policy, compare, compare_default)
                failed = [n for n in ROWS if rows[n]["status"] == "FAIL"]
                skipped = all(rows[n]["status"] == "SKIP" for n in ROWS)
                record.update(rows=rows, failing=failed, status="FAIL" if failed else ("SKIP" if skipped else "PASS"))
            repos.append(record)
    counts = {s: sum(1 for r in repos if r["status"] == s) for s in ("PASS", "FAIL", "EXEMPT", "SKIP")}
    return {"floor": policy["pin_floor"]["sha"], "repos": repos, "summary": {"repos": len(repos), **counts}}


# --- collect: GraphQL, batched like the optimistic-merge audit -------------------------------


def _selection(org: str, name: str, alias: str) -> str:
    wf = ('wf: object(expression:"HEAD:.github/workflows"){... on Tree{entries{name object{... on Blob{text isTruncated}}}}}')
    return f"{alias}: repository(owner:{om._quote(org)},name:{om._quote(name)}){{defaultBranchRef{{name}} {wf}}}"


def _facts(org: str, base: dict, node: dict | None, errors: list[str]) -> dict:
    facts = {"org": org, "repo": f"{org}/{base['name']}", "name": base["name"], "fork": base.get("isFork", False),
             "private": base.get("isPrivate", False),
             "branch": ((node or {}).get("defaultBranchRef") or base.get("defaultBranchRef") or {}).get("name") or "main",
             "errors": list(errors), "workflows": None}
    if node is None:
        facts["errors"] = facts["errors"] or ["repository unreadable"]
        return facts
    tree = node.get("wf")
    workflows: dict[str, str] = {}
    for entry in (tree or {}).get("entries") or []:
        if not entry["name"].endswith((".yml", ".yaml")):
            continue
        blob = entry.get("object") or {}
        if blob.get("text") is None or blob.get("isTruncated"):
            facts["errors"].append(f"{entry['name']} unreadable")
        else:
            workflows[entry["name"]] = blob["text"]
    facts["workflows"] = workflows  # no .github/workflows directory is an empty set, not an error
    return facts


def _query_batch(gh, org: str, bases: list[dict]) -> dict[str, tuple[dict | None, list[str]]]:
    selection = " ".join(_selection(org, b["name"], f"r{i}") for i, b in enumerate(bases))
    try:
        reply = gh.graphql("query{" + selection + "}")
    except Exception as err:  # noqa: BLE001 - unreadable is a FAIL, whatever the reason
        return {b["name"]: (None, [f"query failed: {err}"]) for b in bases}
    errors = reply.get("errors") or []
    data = reply.get("data") or {}
    out = {}
    for i, base in enumerate(bases):
        paths = [str(e.get("message", ""))[:120] for e in errors if f"r{i}" in (e.get("path") or [])]
        out[base["name"]] = (data.get(f"r{i}"), paths)
    return out


def collect(gh, orgs: list[str], policy: dict, today: datetime.date, batch: int = 4) -> dict[str, list[dict]]:
    """Facts for every non-archived repository. Exempt repositories and forks are not read."""
    fleet: dict[str, list[dict]] = {}
    for org in orgs:
        try:
            bases = om.list_org(gh, org)
        except Exception as err:  # noqa: BLE001
            fleet[org] = [{"org": org, "repo": f"{org}/*", "name": "*", "branch": None, "unlisted": True,
                           "errors": [f"{org}: repository list unreadable ({err})"]}]
            continue
        items, wanted = [], []
        for base in bases:
            entry, _ = om.exemption_for(policy, f"{org}/{base['name']}", today)
            if entry or base.get("isFork"):
                items.append(_facts(org, base, None, []) | {"errors": []})
            else:
                wanted.append(base)
        for start in range(0, len(wanted), batch):
            chunk = wanted[start:start + batch]
            result = _query_batch(gh, org, chunk)
            items.extend(_facts(org, base, *result[base["name"]]) for base in chunk)
        fleet[org] = sorted(items, key=lambda f: f["name"].lower())
    return fleet


# --- output -----------------------------------------------------------------------------


def render(report: dict) -> str:
    lines = []
    for repo in report["repos"]:
        if repo["status"] == "FAIL":
            detail = repo.get("errors") or [f"{n}: {repo['rows'][n]['detail']}" for n in repo.get("failing", [])]
            lines.append(f"FAIL   {repo['repo']}  " + " | ".join(detail))
        elif repo.get("note"):
            lines.append(f"NOTE   {repo['repo']}  {repo['note']}")
    s = report["summary"]
    lines.append(f"{s['repos']} repositories: {s['PASS']} PASS, {s['SKIP']} SKIP, {s['EXEMPT']} EXEMPT, {s['FAIL']} FAIL "
                 f"(floor {report['floor'][:9]})")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--policy", default=str(DEFAULT_POLICY))
    parser.add_argument("--org", action="append", help="limit to these organizations (must be in the policy)")
    parser.add_argument("--json", dest="json_out", help="write the full report here")
    parser.add_argument("--facts", help="evaluate previously saved facts instead of reading GitHub")
    parser.add_argument("--save-facts", help="save the facts read from GitHub here")
    parser.add_argument("--today", help="override today's date (YYYY-MM-DD), for tests")
    args = parser.parse_args(argv)

    policy = load_policy(Path(args.policy))
    today = datetime.date.fromisoformat(args.today) if args.today else datetime.datetime.now(datetime.timezone.utc).date()
    orgs = args.org or policy["orgs"]
    outside = [o for o in orgs if o not in policy["orgs"]]
    if outside:
        print("organization not in the policy: " + ", ".join(outside), file=sys.stderr)
        return 2
    gh = om.Gh()
    if args.facts:
        fleet = json.loads(Path(args.facts).read_text())
        fleet = {org: items for org, items in fleet.items() if org in orgs}
    else:
        fleet = collect(gh, orgs, policy, today)
        if args.save_facts:
            Path(args.save_facts).write_text(json.dumps(fleet, indent=1))
    report = evaluate(fleet, policy, om.Comparer(policy["pin_floor"]["sha"], om.compare_reader(gh, policy)), today)
    report["generated_at"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    report["policy"] = str(Path(args.policy).name)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=1))
    print(render(report))
    return 1 if report["summary"]["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
