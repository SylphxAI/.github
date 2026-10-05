#!/usr/bin/env python3
"""Report every repository against the optimistic-merge conformance rows R1-R8.

Contract: docs/optimistic-merge.md. Policy: policy/optimistic-merge.json (the
pin floor and the named exemptions). Read-only: GraphQL queries and compare
reads, never a write.

Rows, per non-archived repository of the policy's organizations:

  R1   sylphx.toml declares [ci] merge = "optimistic" with an explicit on_red
  R2   the gate workflow (ci.yml, else any workflow) runs on merge_group and
       defines the `ci-ok` job
  R3   verify.yml runs on push to the default branch, defines a `verified` job
       and never cancels a running trunk verify (cancel-in-progress is not true)
  R3b  every other workflow that runs on push to the default branch is called
       from verify.yml or marked `# optimistic-merge: advisory`, else its red
       never reaches the red-main handler
  R4   red-main.yml calls the shared handler pinned (full SHA) at or after the
       policy floor, or at a commit where red-main.yml is identical to the
       floor's, and its `if:` follows the repository's default branch
  R5   the gate workflow has a `main-state` job on `main-red-gate` pinned at or
       after the floor (or with the action's files identical to the floor's),
       and `ci-ok` needs it
  R6   the default branch has a merge queue, `ci-ok` is a required check
       (where R2 applies) and `verified` never is
  R7   on_red is `revert` or `revert_pr_unarmed` only where the builder App
       reaches the repository (policy writer_app_orgs), `notify` elsewhere
  R8   merge lane: in a workflow that runs on merge_group, every job on the
       `sylphx-linux-standard` or `sylphx-linux-xlarge` pool selects its
       `-merge` twin on merge_group, so a merge group never queues behind the
       pull-request backlog (verdict jobs on `sylphx-linux-control` and jobs
       that skip merge_group are out of scope)

A row is PASS, FAIL, EXEMPT (waived by a named, unexpired exemption) or SKIP
(nothing to check). Anything the audit could not read is a FAIL, never a pass.
Exit 1 on any FAIL; the JSON report (--json) lists every repository and row.

Hands-off repositories (policy class hands-off) and any repository whose every
row is waived are listed from their name only: their files are never read.
"""
from __future__ import annotations

import argparse
import datetime
import fnmatch
import json
import re
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "policy" / "optimistic-merge.json"
ROWS = ("R1", "R2", "R3", "R3b", "R4", "R5", "R6", "R7", "R8")
ON_RED = ("revert", "revert_pr_unarmed", "notify")
# Personal repositories are never read, whatever the policy says.
NEVER_READ_OWNERS = ("tsefamily", "shtse8")
ADVISORY = re.compile(r"^\s*#\s*optimistic-merge:\s*advisory\b", re.M | re.I)
SHA40 = re.compile(r"^[0-9a-f]{40}$")
WORKFLOW_KEYS = ("ci.yml", "verify.yml", "red-main.yml")


# --- policy ---------------------------------------------------------------------------


def load_policy(path: Path) -> dict:
    policy = json.loads(Path(path).read_text())
    floor = policy.get("pin_floor", {}).get("sha", "")
    if not SHA40.match(floor):
        raise ValueError("policy pin_floor.sha must be a full 40-hex commit SHA")
    for org in policy.get("orgs", []):
        if org.lower() in NEVER_READ_OWNERS:
            raise ValueError(f"policy names a personal owner: {org}")
    for entry in policy.get("exemptions", []):
        for need in ("class", "repos", "reason", "owner", "review"):
            if not entry.get(need):
                raise ValueError(f"exemption without {need}: {entry}")
        if entry["class"] not in policy.get("classes", {}):
            raise ValueError(f"exemption class unknown: {entry['class']}")
        datetime.date.fromisoformat(entry["review"])
    return policy


def exemption_for(policy: dict, repo: str, today: datetime.date) -> tuple[dict | None, str | None]:
    """The exemption that covers `repo` today, or (None, note) when one lapsed."""
    lapsed = None
    for entry in policy.get("exemptions", []):
        if not any(fnmatch.fnmatchcase(repo, pattern) for pattern in entry["repos"]):
            continue
        if datetime.date.fromisoformat(entry["review"]) < today:
            lapsed = f"exemption {entry['class']} lapsed on {entry['review']} (owner {entry['owner']}): review it"
            continue
        return entry, None
    return None, lapsed


def waived_rows(policy: dict, entry: dict) -> set[str]:
    rows = entry.get("rows") or policy["classes"][entry["class"]]["waives"]
    return set(ROWS) if rows == ["*"] else set(rows)


# --- workflow text helpers (stdlib only; the repo's own tests parse them with PyYAML) --


def _code_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _split_inline(value: str) -> list[str]:
    value = value.split(" #", 1)[0].strip()
    if value.startswith("["):
        value = value.strip("[]")
    return [part.strip().strip("'\"") for part in value.split(",") if part.strip().strip("'\"")]


def _sub_block(lines: list[str], start: int) -> list[str]:
    base = _indent(lines[start])
    out = []
    for line in lines[start + 1:]:
        if _indent(line) <= base:
            break
        out.append(line)
    return out


def triggers(text: str) -> dict[str, dict]:
    """The `on:` triggers of a workflow: {event: {"branches": [...] | None, "tags": bool, "ignore": [...]}}."""
    lines = _code_lines(text)
    for i, line in enumerate(lines):
        match = re.match(r"""^(?:on|"on"|'on')\s*:\s*(.*)$""", line)
        if match:
            break
    else:
        return {}
    rest = match.group(1).split(" #", 1)[0].strip()
    if rest:
        return {name: {"branches": None, "tags": False, "ignore": []} for name in _split_inline(rest)}
    block = _sub_block(lines, i)
    if not block:
        return {}
    base = _indent(block[0])
    out: dict[str, dict] = {}
    for j, line in enumerate(block):
        if _indent(line) != base:
            continue
        item = re.match(r"^\s*-\s+(\S+)\s*$", line)
        if item:
            out[item.group(1).strip("'\"")] = {"branches": None, "tags": False, "ignore": []}
            continue
        key = re.match(r"^\s*['\"]?([\w-]+)['\"]?\s*:\s*(.*)$", line)
        if not key:
            continue
        spec = {"branches": None, "tags": False, "ignore": []}
        sub = _sub_block(block, j)
        for k, subline in enumerate(sub):
            field = re.match(r"^\s*(branches|branches-ignore|tags|tags-ignore)\s*:\s*(.*)$", subline)
            if not field:
                continue
            name, value = field.group(1), field.group(2).strip()
            if value:
                values = _split_inline(value)
            else:
                values = []
                for entry in _sub_block(sub, k):
                    bullet = re.match(r"^\s*-\s*(.+?)\s*(?:#.*)?$", entry)
                    if bullet:
                        values.append(bullet.group(1).strip("'\""))
            if name == "branches":
                spec["branches"] = values
            elif name == "branches-ignore":
                spec["ignore"] = values
            elif name == "tags":
                spec["tags"] = True
        out[key.group(1)] = spec
    return out


def push_covers(text: str, branch: str) -> bool:
    """Does a push to `branch` start this workflow?"""
    spec = triggers(text).get("push")
    if spec is None:
        return False
    if spec["branches"] is None:
        # `tags:` alone means branch pushes do not start it.
        return not spec["tags"] and not any(fnmatch.fnmatchcase(branch, p) for p in spec["ignore"])
    return any(fnmatch.fnmatchcase(branch, p) for p in spec["branches"])


def jobs(text: str) -> dict[str, list[str]]:
    lines = _code_lines(text)
    for i, line in enumerate(lines):
        if re.match(r"^jobs\s*:\s*$", line):
            break
    else:
        return {}
    block = _sub_block(lines, i)
    if not block:
        return {}
    base = _indent(block[0])
    out: dict[str, list[str]] = {}
    current = None
    for line in block:
        if _indent(line) == base:
            match = re.match(r"^\s*['\"]?([\w.-]+)['\"]?\s*:\s*$", line)
            current = match.group(1) if match else None
            if current:
                out[current] = []
        elif current:
            out[current].append(line)
    return out


def has_job(text: str, name: str) -> bool:
    for job_id, body in jobs(text).items():
        if job_id == name:
            return True
        if any(re.match(rf"""^\s*name\s*:\s*['"]?{re.escape(name)}['"]?\s*(#.*)?$""", line) for line in body):
            return True
    return False


def job_needs(text: str, job: str) -> list[str]:
    body = jobs(text).get(job, [])
    for i, line in enumerate(body):
        match = re.match(r"^\s*needs\s*:\s*(.*)$", line)
        if not match:
            continue
        value = match.group(1).strip()
        if value:
            return _split_inline(value)
        return [m.group(1).strip("'\"") for entry in _sub_block(body, i)
                for m in [re.match(r"^\s*-\s*(\S+)", entry)] if m]
    return []


BUILD_POOL = re.compile(r"sylphx-linux-(?:standard|xlarge)(?![\w-])")
MERGE_LANE = re.compile(r"sylphx-linux-(?:standard|xlarge)-merge(?![\w-])")


def job_runs_on(body: list[str]) -> str | None:
    """The `runs-on:` of one job as text (an inline value or its list items), None if it has none."""
    for i, line in enumerate(body):
        match = re.match(r"^\s*runs-on\s*:\s*(.*)$", line)
        if not match:
            continue
        value = match.group(1).strip()
        if value:
            return value
        return " ".join(entry.strip() for entry in _sub_block(body, i))
    return None


def job_skips_merge_group(body: list[str]) -> bool:
    """A single-line `if:` that only lets pull_request (or anything but merge_group) run the job."""
    for line in body:
        match = re.match(r"^\s*if\s*:\s*(.*)$", line)
        if match:
            cond = match.group(1)
            return bool(re.search(r"event_name\s*!=\s*'merge_group'|event_name\s*==\s*'(?:pull_request|push|schedule|workflow_dispatch)'", cond)
                        and not re.search(r"event_name\s*==\s*'merge_group'", cond))
    return False


def merge_lane_misses(text: str) -> list[str]:
    """Jobs of a merge_group workflow on the shared build pool without the `-merge` selector."""
    if "merge_group" not in triggers(text):
        return []
    misses = []
    for job_id, body in jobs(text).items():
        runs_on = job_runs_on(body)
        if runs_on is None or not BUILD_POOL.search(runs_on) or MERGE_LANE.search(runs_on):
            continue
        if re.search(r"\$\{\{[^}]*(?:matrix|inputs)\.", runs_on) or job_skips_merge_group(body):
            continue  # decided at run time, or never a merge-group job
        misses.append(job_id)
    return misses


def uses_refs(text: str, path_pattern: str) -> list[str]:
    """Refs after `@` for every `uses: SylphxAI/.github/<path_pattern>@ref`."""
    found = []
    for line in _code_lines(text):
        match = re.match(rf"^\s*-?\s*uses\s*:\s*SylphxAI/\.github/{path_pattern}@(\S+)", line)
        if match:
            found.append(match.group(1))
    return found


def toml_ci(text: str | None) -> dict:
    """The `merge` and `on_red` values of the [ci] table."""
    out: dict[str, str] = {}
    in_ci = False
    for line in (text or "").splitlines():
        line = line.split("#", 1)[0].strip()
        header = re.match(r"^\[([^\]]+)\]$", line)
        if header:
            in_ci = header.group(1).strip() == "ci"
            continue
        match = re.match(r"""^(merge|on_red)\s*=\s*["']([^"']*)["']$""", line)
        if in_ci and match:
            out[match.group(1)] = match.group(2)
    return out


# --- rules --------------------------------------------------------------------------


def _applies_to_default(include: list[str], exclude: list[str], branch: str) -> bool:
    def match(pattern: str) -> bool:
        if pattern in ("~DEFAULT_BRANCH", "~ALL"):
            return True
        return fnmatch.fnmatchcase(f"refs/heads/{branch}", pattern)
    return any(match(p) for p in include) and not any(
        fnmatch.fnmatchcase(f"refs/heads/{branch}", p) for p in exclude if p not in ("~DEFAULT_BRANCH", "~ALL"))


def queue_and_checks(facts: dict) -> tuple[bool, list[str]]:
    """Is there a merge queue on the default branch, and which contexts does it require."""
    branch = facts["branch"]
    queue = bool(facts.get("merge_queue"))
    contexts: list[str] = []
    for ruleset in facts.get("rulesets") or []:
        if ruleset.get("enforcement") != "ACTIVE" or ruleset.get("target") != "BRANCH":
            continue
        if not _applies_to_default(ruleset.get("include") or [], ruleset.get("exclude") or [], branch):
            continue
        for rule in ruleset.get("rules") or []:
            if rule.get("type") == "MERGE_QUEUE":
                queue = True
            elif rule.get("type") == "REQUIRED_STATUS_CHECKS":
                contexts.extend(rule.get("contexts") or [])
    for rule in facts.get("protection") or []:
        if fnmatch.fnmatchcase(branch, rule.get("pattern", "")):
            contexts.extend(rule.get("contexts") or [])
    return queue, contexts


def is_check(context: str, name: str) -> bool:
    return context == name or context.rsplit(" / ", 1)[-1] == name


# --- evaluate: pure, tested on fixtures ---------------------------------------------


class Comparer:
    """`pin >= floor`, one read per distinct pin. An unreadable pin is None.

    `read(floor, pin)` returns the compare status, or a dict with `status` and
    the changed `files` between the two commits. A pin behind the floor is still
    conformant for a component whose files did not change in between."""

    def __init__(self, floor: str, read):
        self.floor, self.read, self.cache = floor, read, {}

    def _compare(self, pin: str) -> dict:
        if pin not in self.cache:
            try:
                got = self.read(self.floor, pin)
                self.cache[pin] = got if isinstance(got, dict) else {"status": got, "files": None}
            except Exception as err:  # noqa: BLE001 - unreadable is a FAIL, whatever the reason
                self.cache[pin] = {"status": f"error: {err}", "files": None}
        return self.cache[pin]

    def at_or_after(self, pin: str, component=None) -> tuple[bool | None, str]:
        """`component(path) -> bool` selects the files that matter for this pin."""
        if not SHA40.match(pin):
            return False, f"pin `{pin[:20]}` is not a full 40-hex commit SHA"
        if pin == self.floor:
            return True, ""
        got = self._compare(pin)
        status = got["status"]
        if status in ("ahead", "identical"):
            return True, ""
        if status in ("behind", "diverged"):
            files = got.get("files")
            if component is not None and files is not None and not any(component(f) for f in files):
                return True, ""
            return False, f"pin {pin[:9]} is {status} the floor {self.floor[:9]}"
        return None, f"pin {pin[:9]} could not be compared with the floor ({status})"


R4_COMPONENT = lambda path: path == ".github/workflows/red-main.yml"  # noqa: E731
R5_COMPONENT = lambda path: path.startswith(".github/actions/main-red-gate/")  # noqa: E731


def gate_workflow(facts: dict) -> tuple[str | None, str | None]:
    """The workflow that runs on merge_group and defines `ci-ok`: ci.yml first, else any such one.
    Returns (name, text); with none qualifying, ci.yml (or None) so the row says what is missing."""
    files = facts.get("files") or {}
    texts = {name: text for name, text in (facts.get("all_workflows") or {}).items() if text}
    for name, text in files.items():
        if text:
            texts.setdefault(name, text)
    order = (["ci.yml"] if "ci.yml" in texts else []) + sorted(n for n in texts if n != "ci.yml")
    for name in order:
        if "merge_group" in triggers(texts[name]) and has_job(texts[name], "ci-ok"):
            return name, texts[name]
    return ("ci.yml", texts["ci.yml"]) if "ci.yml" in texts else (None, None)


def _row(status: str, detail: str = "") -> dict:
    return {"status": status, "detail": detail}


def evaluate_rows(facts: dict, policy: dict, compare: Comparer) -> dict[str, dict]:
    """The rows of one repository from its facts (no network)."""
    org, branch = facts["org"], facts["branch"]
    unreadable = facts.get("errors") or []
    files = facts.get("files") or {}
    ci_name, ci = gate_workflow(facts)
    verify, redmain = files.get("verify.yml"), files.get("red-main.yml")
    declared = toml_ci(facts.get("toml"))
    rows: dict[str, dict] = {}

    def need(cond: bool, ok: str, bad: str) -> dict:
        return _row("PASS", ok) if cond else _row("FAIL", bad)

    # R1
    if declared.get("merge") != "optimistic":
        rows["R1"] = _row("FAIL", "sylphx.toml has no [ci] merge = \"optimistic\"" if facts.get("toml") is not None
                          else "no sylphx.toml")
    elif declared.get("on_red") not in ON_RED:
        rows["R1"] = _row("FAIL", "[ci] on_red is not explicit (revert, revert_pr_unarmed or notify)")
    else:
        rows["R1"] = _row("PASS", f"on_red = {declared['on_red']}")

    # R2
    if ci is None:
        rows["R2"] = _row("FAIL", "no .github/workflows/ci.yml")
    else:
        missing = [what for what, ok in (("merge_group trigger", "merge_group" in triggers(ci)),
                                         ("ci-ok job", has_job(ci, "ci-ok"))) if not ok]
        rows["R2"] = need(not missing, "merge_group and ci-ok", f"{ci_name} lacks " + " and ".join(missing))

    # R3
    if verify is None:
        rows["R3"] = _row("FAIL", "no .github/workflows/verify.yml")
    else:
        missing = []
        if not push_covers(verify, branch):
            missing.append(f"a push trigger covering {branch}")
        if not has_job(verify, "verified"):
            missing.append("a `verified` job")
        for line in _code_lines(verify):
            match = re.match(r"^\s*cancel-in-progress\s*:\s*(.+?)\s*(?:#.*)?$", line)
            # `false`, or the starter's `github.event_name == 'pull_request'`
            # (cancel a superseded pull-request run, never a trunk run).
            value = match.group(1).strip("'\"") if match else ""
            if match and value.lower() in ("true", "yes", "1"):
                missing.append("cancel-in-progress: false")
                break
        rows["R3"] = need(not missing, f"push to {branch}, verified, no cancel", "verify.yml lacks " + ", ".join(missing))

    # R3b
    if verify is None:
        rows["R3b"] = _row("SKIP", "no verify.yml")
    elif facts.get("all_workflows") is None:
        rows["R3b"] = _row("FAIL", "workflow files unreadable")
    else:
        called = set(re.findall(r"uses\s*:\s*\./\.github/workflows/([\w.-]+)", verify))
        loose = []
        for name, text in sorted(facts["all_workflows"].items()):
            if name in WORKFLOW_KEYS or name in called or ADVISORY.search(text or ""):
                continue
            if text and push_covers(text, branch):
                loose.append(name)
        rows["R3b"] = need(not loose, "every push workflow is in verify.yml or advisory",
                           "runs on push to " + branch + " outside verify.yml: " + ", ".join(loose))

    # R4
    if redmain is None:
        rows["R4"] = _row("FAIL", "no .github/workflows/red-main.yml")
    else:
        pins = uses_refs(redmain, r"\.github/workflows/red-main\.yml")
        bad = []
        if not pins:
            bad.append("no caller of the shared red-main handler")
        for pin in pins:
            ok, why = compare.at_or_after(pin, R4_COMPONENT)
            if ok is not True:
                bad.append(why)
        literal = re.findall(r"head_branch\s*==\s*'([^']+)'", "\n".join(_code_lines(redmain)))
        for name in literal:
            if name != branch:
                bad.append(f"the `if:` names branch `{name}`, the default branch is `{branch}`")
        rows["R4"] = need(not bad, "pinned at or after the floor", "; ".join(bad))

    # R5
    if ci is None:
        rows["R5"] = _row("FAIL", "no gate workflow")
    else:
        pins = uses_refs(ci, r"\.github/actions/main-red-gate")
        bad = []
        if not has_job(ci, "main-state") or not pins:
            bad.append("no main-state job on main-red-gate")
        else:
            for pin in pins:
                ok, why = compare.at_or_after(pin, R5_COMPONENT)
                if ok is not True:
                    bad.append(why)
            if "main-state" not in job_needs(ci, "ci-ok"):
                bad.append(f"ci-ok in {ci_name} does not need main-state")
        rows["R5"] = need(not bad, "main-state pinned at or after the floor", "; ".join(bad))

    # R6
    if facts.get("rules_error"):
        rows["R6"] = _row("FAIL", "rulesets unreadable: " + facts["rules_error"])
    else:
        queue, contexts = queue_and_checks(facts)
        bad = []
        if not queue:
            bad.append("no merge queue on " + branch)
        if any(is_check(c, "verified") for c in contexts):
            bad.append("`verified` is a required check")
        if facts.get("needs_ci_ok", True) and not any(is_check(c, "ci-ok") for c in contexts):
            bad.append("`ci-ok` is not a required check (required: " + (", ".join(sorted(set(contexts))) or "none") + ")")
        elif not contexts:
            bad.append("no required check")
        rows["R6"] = need(not bad, "merge queue, ci-ok required", "; ".join(bad))

    # R7
    on_red = declared.get("on_red")
    app = org in policy.get("writer_app_orgs", []) or facts["repo"] in policy.get("writer_app_repos", [])
    if on_red not in ON_RED:
        rows["R7"] = _row("FAIL", "on_red not declared")
    elif app and on_red == "notify":
        rows["R7"] = _row("FAIL", f"the builder App reaches {org}: on_red should be revert, not notify")
    elif not app and on_red != "notify":
        rows["R7"] = _row("FAIL", f"the builder App does not reach {org}: on_red = {on_red} runs as notify; declare notify")
    else:
        rows["R7"] = _row("PASS", f"on_red = {on_red}")

    # R8
    candidates = {name: text for name, text in (facts.get("all_workflows") or {}).items() if text}
    for name, text in files.items():
        if text:
            candidates.setdefault(name, text)
    if not candidates:
        rows["R8"] = _row("FAIL", "workflow files unreadable") if unreadable else _row("SKIP", "no workflows read")
    else:
        missed = {name: merge_lane_misses(text) for name, text in sorted(candidates.items())}
        missed = {name: ids for name, ids in missed.items() if ids}
        rows["R8"] = need(not missed, "merge-group jobs select the merge lane",
                          "merge_group job on the pull-request pool without a `-merge` runner: "
                          + "; ".join(f"{name} ({', '.join(ids)})" for name, ids in missed.items()))

    if unreadable:
        for name in ROWS:
            if rows[name]["status"] == "FAIL":
                rows[name]["detail"] += " (read errors: " + "; ".join(unreadable[:2]) + ")"
    return rows


def evaluate(fleet: dict, policy: dict, compare: Comparer, today: datetime.date) -> dict:
    """The whole report from collected facts: {org: [repo facts]}."""
    repos = []
    for org, items in fleet.items():
        for facts in items:
            entry, lapsed = exemption_for(policy, facts["repo"], today)
            record = {"repo": facts["repo"], "branch": facts.get("branch"), "exemption": None}
            if facts.get("fork") and not toml_ci(facts.get("toml")).get("merge"):
                record.update(status="EXEMPT", exemption={"class": "fork", "reason": "a fork follows its upstream"}, rows={})
                repos.append(record)
                continue
            if facts.get("unlisted"):
                record.update(status="FAIL", rows={}, errors=facts["errors"])
                repos.append(record)
                continue
            facts = dict(facts)
            if facts.get("errors"):
                record["errors"] = list(facts["errors"])
            waived = waived_rows(policy, entry) if entry else set()
            facts["needs_ci_ok"] = "R2" not in waived
            rows = evaluate_rows(facts, policy, compare) if set(ROWS) - waived else {}
            for name in ROWS:
                if name in waived:
                    rows[name] = _row("EXEMPT", f"{entry['class']}: {entry['reason']}")
            if entry:
                record["exemption"] = {k: entry[k] for k in ("class", "reason", "owner", "review")}
            if lapsed:
                record["note"] = lapsed
            failed = [n for n in ROWS if rows[n]["status"] == "FAIL"]
            record["rows"] = rows
            record["status"] = "FAIL" if failed else ("EXEMPT" if len(waived) == len(ROWS) else "PASS")
            record["failing"] = failed
            repos.append(record)
    counts = {s: sum(1 for r in repos if r["status"] == s) for s in ("PASS", "FAIL", "EXEMPT")}
    return {"floor": policy["pin_floor"]["sha"], "repos": repos, "summary": {"repos": len(repos), **counts}}


# --- collect: GraphQL, a few queries per organization ---------------------------------


CAP_MARKER = "desk-gh-graphql-cap"


def _env_seconds(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ[name]))
    except (KeyError, ValueError):
        return default


class Gh:
    """Read-only GitHub access through the `gh` CLI (the desk login, or GH_TOKEN in CI).

    The desk `gh` wrapper refuses live GraphQL reads past a per-lane cap in a rolling
    window (30 per 10 minutes). A refusal is waited out: sleep in `step` seconds and
    ask again, at most AUDIT_CAP_WAIT_S (default 720) per call and AUDIT_CAP_WAIT_TOTAL_S
    (default 5400) over the whole run. Any other failure surfaces at once.
    """

    def __init__(self, sleep=time.sleep, per_call_s: float | None = None, total_s: float | None = None,
                 step_s: float = 45.0):
        self.sleep = sleep
        self.per_call_s = _env_seconds("AUDIT_CAP_WAIT_S", 720.0) if per_call_s is None else per_call_s
        self.total_s = _env_seconds("AUDIT_CAP_WAIT_TOTAL_S", 5400.0) if total_s is None else total_s
        self.step_s = step_s
        self.waited_s = 0.0

    def _graphql_once(self, query: str) -> dict:
        result = subprocess.run(["gh", "api", "graphql", "-f", "query=" + query], capture_output=True, text=True, timeout=180)
        if not result.stdout.strip():
            raise RuntimeError((result.stderr or "empty reply").strip()[:200])
        return json.loads(result.stdout)

    def graphql(self, query: str) -> dict:
        waited = 0.0
        while True:
            try:
                return self._graphql_once(query)
            except RuntimeError as err:
                if CAP_MARKER not in str(err):
                    raise
                pause = min(self.step_s, self.per_call_s - waited, self.total_s - self.waited_s)
                if pause <= 0:
                    raise
                print(f"audit: live GraphQL cap reached; waiting {pause:.0f}s "
                      f"({waited + pause:.0f}s of {self.per_call_s:.0f}s for this read, "
                      f"{self.waited_s + pause:.0f}s of {self.total_s:.0f}s for the run)", file=sys.stderr, flush=True)
                self.sleep(pause)
                waited += pause
                self.waited_s += pause

    def rest(self, path: str) -> dict:
        result = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise RuntimeError((result.stderr or "failed").strip()[:200])
        return json.loads(result.stdout)


def _blob(name: str, expression: str) -> str:
    return f'{name}: object(expression:"{expression}"){{... on Blob{{text}}}}'


def _quote(value: str) -> str:
    return json.dumps(value)


def list_org(gh, org: str) -> list[dict]:
    """Every non-archived repository of `org`, names and default branches only."""
    repos, after = [], None
    while True:
        cursor = f",after:{_quote(after)}" if after else ""
        data = gh.graphql('query{organization(login:%s){repositories(first:100,isArchived:false%s){'
                          'pageInfo{hasNextPage endCursor} nodes{name isFork isPrivate defaultBranchRef{name}}}}}'
                          % (_quote(org), cursor))
        page = (data.get("data") or {}).get("organization")
        if not page:
            raise RuntimeError(f"organization {org} unreadable: {json.dumps(data.get('errors'))[:160]}")
        connection = page["repositories"]
        repos.extend(connection["nodes"])
        if not connection["pageInfo"]["hasNextPage"]:
            return repos
        after = connection["pageInfo"]["endCursor"]


def _repo_selection(org: str, name: str, alias: str, with_workflows: bool) -> str:
    wf = ("wf: object(expression:\"HEAD:.github/workflows\"){... on Tree{entries{name %s}}}"
          % ("object{... on Blob{text}}" if with_workflows else ""))
    fields = " ".join([
        "defaultBranchRef{name}", "mergeQueue{id}", _blob("toml", "HEAD:sylphx.toml"),
        _blob("ci", "HEAD:.github/workflows/ci.yml"), _blob("verify", "HEAD:.github/workflows/verify.yml"),
        _blob("redmain", "HEAD:.github/workflows/red-main.yml"), wf,
        "rulesets(first:30,includeParents:true){nodes{name enforcement target conditions{refName{include exclude}} "
        "rules(first:30){nodes{type parameters{... on RequiredStatusChecksParameters{requiredStatusChecks{context}}}}}}}",
        "branchProtectionRules(first:30){nodes{pattern requiredStatusCheckContexts}}",
    ])
    return f"{alias}: repository(owner:{_quote(org)},name:{_quote(name)}){{{fields}}}"


def _facts(org: str, name: str, base: dict, node: dict | None, errors: list[str]) -> dict:
    facts = {"org": org, "repo": f"{org}/{name}", "name": name, "fork": base.get("isFork", False),
             "private": base.get("isPrivate", False), "branch": (base.get("defaultBranchRef") or {}).get("name") or "main",
             "errors": errors, "files": {}, "toml": None, "all_workflows": None, "merge_queue": False,
             "rulesets": None, "protection": None}
    if node is None:
        facts["errors"] = errors or ["repository unreadable"]
        facts["rules_error"] = "repository unreadable"
        return facts
    facts["toml"] = (node.get("toml") or {}).get("text")
    facts["files"] = {key: (node.get(alias) or {}).get("text") for key, alias in
                      (("ci.yml", "ci"), ("verify.yml", "verify"), ("red-main.yml", "redmain"))}
    facts["merge_queue"] = bool(node.get("mergeQueue"))
    if node.get("rulesets") is None or node.get("branchProtectionRules") is None:
        facts["rules_error"] = "rulesets or branch protection not returned"
    else:
        facts["rulesets"] = [{
            "enforcement": r["enforcement"], "target": r["target"],
            "include": ((r.get("conditions") or {}).get("refName") or {}).get("include") or [],
            "exclude": ((r.get("conditions") or {}).get("refName") or {}).get("exclude") or [],
            "rules": [{"type": rule["type"],
                       "contexts": [c["context"] for c in ((rule.get("parameters") or {}).get("requiredStatusChecks") or [])]}
                      for rule in (r.get("rules") or {}).get("nodes") or []]}
            for r in node["rulesets"]["nodes"]]
        facts["protection"] = [{"pattern": r["pattern"], "contexts": r.get("requiredStatusCheckContexts") or []}
                               for r in node["branchProtectionRules"]["nodes"]]
    tree = node.get("wf")
    if tree is not None and "entries" in tree:
        names = [e["name"] for e in tree["entries"] if e["name"].endswith((".yml", ".yaml"))]
        facts["workflow_names"] = names
        if tree["entries"] and "object" in tree["entries"][0]:
            facts["all_workflows"] = {e["name"]: (e.get("object") or {}).get("text") or "" for e in tree["entries"]
                                      if e["name"] in names}
    else:
        facts["workflow_names"] = []
    return facts


def _query_batch(gh, org: str, bases: list[dict], with_workflows: bool) -> dict[str, tuple[dict | None, list[str]]]:
    selection = " ".join(_repo_selection(org, b["name"], f"r{i}", with_workflows) for i, b in enumerate(bases))
    out: dict[str, tuple[dict | None, list[str]]] = {}
    try:
        reply = gh.graphql("query{" + selection + "}")
    except Exception as err:  # noqa: BLE001
        return {b["name"]: (None, [f"query failed: {err}"]) for b in bases}
    errors = reply.get("errors") or []
    data = reply.get("data") or {}
    for i, base in enumerate(bases):
        paths = [str(e.get("message", ""))[:120] for e in errors if f"r{i}" in (e.get("path") or [])]
        out[base["name"]] = (data.get(f"r{i}"), paths)
    return out


def collect(gh, orgs: list[str], policy: dict, today: datetime.date, batch: int = 8) -> dict[str, list[dict]]:
    """Facts for every non-archived repository. Hands-off and fully waived repositories are not read."""
    fleet: dict[str, list[dict]] = {}
    for org in orgs:
        items: list[dict] = []
        try:
            bases = list_org(gh, org)
        except Exception as err:  # noqa: BLE001
            fleet[org] = [{"org": org, "repo": f"{org}/*", "name": "*", "branch": None, "unlisted": True,
                           "errors": [f"{org}: repository list unreadable ({err})"]}]
            continue
        wanted = []
        for base in bases:
            repo = f"{org}/{base['name']}"
            entry, _ = exemption_for(policy, repo, today)
            if entry and (entry["class"] == "hands-off" or set(ROWS) <= waived_rows(policy, entry)):
                items.append(_facts(org, base["name"], base, None, []) | {"errors": [], "rules_error": None})
            else:
                wanted.append(base)
        by_name: dict[str, dict] = {}
        for start in range(0, len(wanted), batch):
            chunk = wanted[start:start + batch]
            result = _query_batch(gh, org, chunk, False)
            for base in chunk:
                node, errors = result[base["name"]]
                by_name[base["name"]] = _facts(org, base["name"], base, node, errors)
        # second pass: every workflow's text, only where a verify.yml exists (R3b)
        need_all = [b for b in wanted if by_name[b["name"]]["files"].get("verify.yml") is not None]
        for start in range(0, len(need_all), 3):
            chunk = need_all[start:start + 3]
            result = _query_batch(gh, org, chunk, True)
            for base in chunk:
                node, errors = result[base["name"]]
                if node is not None:
                    by_name[base["name"]]["all_workflows"] = _facts(org, base["name"], base, node, errors)["all_workflows"]
        items.extend(by_name[b["name"]] for b in wanted)
        for facts in items:
            facts.pop("workflow_names", None)
        fleet[org] = sorted(items, key=lambda f: f["name"].lower())
    return fleet


def compare_reader(gh, policy: dict):
    repo = policy["pin_floor"]["repo"]

    def read(floor: str, pin: str) -> str:
        got = gh.rest(f"repos/{repo}/compare/{floor}...{pin}")
        files = [f["filename"] for f in got.get("files") or []]
        # the compare reply lists at most 300 files: a longer change is not a complete list
        return {"status": got["status"], "files": files if len(files) < 300 else None}
    return read


# --- output -----------------------------------------------------------------------------


def render(report: dict) -> str:
    lines = []
    for repo in report["repos"]:
        if repo["status"] == "FAIL":
            detail = [f"{n}: {repo['rows'][n]['detail']}" for n in repo.get("failing", [])] or repo.get("errors") or []
            lines.append(f"FAIL   {repo['repo']}  " + " | ".join(detail))
    s = report["summary"]
    lines.append(f"{s['repos']} repositories: {s['PASS']} PASS, {s['EXEMPT']} EXEMPT, {s['FAIL']} FAIL "
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
    gh = Gh()
    if args.facts:
        fleet = json.loads(Path(args.facts).read_text())
        fleet = {org: items for org, items in fleet.items() if org in orgs}
    else:
        fleet = collect(gh, orgs, policy, today)
        if args.save_facts:
            Path(args.save_facts).write_text(json.dumps(fleet, indent=1))
    report = evaluate(fleet, policy, Comparer(policy["pin_floor"]["sha"], compare_reader(gh, policy)), today)
    report["generated_at"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    report["policy"] = str(Path(args.policy).name)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=1))
    print(render(report))
    return 1 if report["summary"]["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
