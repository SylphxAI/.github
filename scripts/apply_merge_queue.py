#!/usr/bin/env python3
"""Converge the merge queue rule of every repository ruleset on policy/merge-queue.json.

GitHub does not accept a `merge_queue` rule in an organization or enterprise
ruleset (REST: only the repository rule schema carries it; docs: "Require merge
queue ... is not available for rulesets created at the organization level"), so
the queue is configured once per repository. This file's policy is the one
source, and this tool is how it reaches every repository.

Policy: docs/merge-queue-settings.md. Dry-run is the default; nothing is written
without --apply.

  scripts/apply_merge_queue.py                     # dry-run diff of every repository
  scripts/apply_merge_queue.py --dry-run --json    # the same, machine-readable
  scripts/apply_merge_queue.py --repo SylphxAI/desk-tools
  scripts/apply_merge_queue.py --apply --backup-dir DIR [--repo ORG/NAME ...]
  scripts/apply_merge_queue.py --rollback DIR      # put the saved merge_queue parameters back
  scripts/apply_merge_queue.py --check             # exit 1 when any ruleset drifts

What it manages: the parameters of the `merge_queue` rule of every ruleset that
has one. Everything else in a ruleset is carried through unchanged on a write.
What it only reports: whether the fast gate (`ci-ok`) is a required check of the
same ruleset, and the strict flag. It never adds a required check, because a
check no workflow reports would stop the queue.

Reads are one GraphQL query per 100 repositories of an organization. Writes are
paced one second apart, are read back, and stop at the first HTTP 403. --apply refuses a
backup directory that already holds a backup of a ruleset it would change; --rollback puts
back only the merge_queue parameters, onto the ruleset as it is then.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "policy" / "merge-queue.json"
HANDS_OFF_POLICY = ROOT / "policy" / "optimistic-merge.json"

# REST (snake_case) <-> GraphQL (camelCase) names of the merge queue parameters.
PARAMS = {
    "merge_method": "mergeMethod",
    "grouping_strategy": "groupingStrategy",
    "max_entries_to_build": "maxEntriesToBuild",
    "min_entries_to_merge": "minEntriesToMerge",
    "max_entries_to_merge": "maxEntriesToMerge",
    "min_entries_to_merge_wait_minutes": "minEntriesToMergeWaitMinutes",
    "check_response_timeout_minutes": "checkResponseTimeoutMinutes",
}
ENUMS = {"merge_method": {"MERGE", "SQUASH", "REBASE"}, "grouping_strategy": {"ALLGREEN", "HEADGREEN"}}
PUT_KEYS = ("name", "target", "enforcement", "bypass_actors", "conditions", "rules")
WRITE_PAUSE_SECONDS = 1.0


class Forbidden(RuntimeError):
    """GitHub answered 403: stop, never retry."""


# --- policy ---------------------------------------------------------------------------


def load_policy(path: Path) -> dict:
    policy = json.loads(Path(path).read_text())
    settings = policy.get("settings")
    if not isinstance(settings, dict) or set(settings) != set(PARAMS):
        raise ValueError(f"settings must carry exactly {sorted(PARAMS)}")
    check_settings(settings, "settings")
    for name, allowed in ENUMS.items():
        if settings[name] not in allowed:
            raise ValueError(f"settings.{name} must be one of {sorted(allowed)}")
    if not policy.get("orgs"):
        raise ValueError("orgs is empty")
    gate = policy.get("fast_gate", {})
    if not gate.get("context") or not isinstance(gate.get("integration_id"), int):
        raise ValueError("fast_gate needs a context and an integer integration_id")
    for key, fields in (("exclude", ("repo", "reason")), ("overrides", ("repo", "ruleset", "settings", "reason")),
                        ("fast_gate_overrides", ("repo", "ruleset", "context", "reason"))):
        for entry in policy.get(key, []):
            missing = [f for f in fields if not entry.get(f)]
            if missing:
                raise ValueError(f"{key} entry {entry.get('repo')!r} lacks {missing}")
    for entry in policy.get("overrides", []):
        check_settings(entry["settings"], f"override {entry['repo']}")
    return policy


def check_settings(values: dict, where: str) -> None:
    for key, value in values.items():
        if key not in PARAMS:
            raise ValueError(f"{where}: unknown setting {key}")
        if key in ENUMS:
            if value not in ENUMS[key]:
                raise ValueError(f"{where}.{key} must be one of {sorted(ENUMS[key])}")
        elif not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"{where}.{key} must be a non-negative integer")


def hands_off_repos(path: Path = HANDS_OFF_POLICY) -> set[str]:
    """Repositories the optimistic-merge policy marks hands-off: never read or written."""
    if not Path(path).exists():
        return set()
    out: set[str] = set()
    for entry in json.loads(Path(path).read_text()).get("exemptions", []):
        if entry.get("class") == "hands-off":
            out.update(entry.get("repos", []))
    return out


def excluded(policy: dict, hands_off: set[str]) -> dict[str, str]:
    out = {r: "hands-off in policy/optimistic-merge.json" for r in hands_off}
    out.update({e["repo"]: e["reason"] for e in policy.get("exclude", [])})
    return out


def desired_settings(policy: dict, repo: str, ruleset: str) -> dict:
    out = dict(policy["settings"])
    for entry in policy.get("overrides", []):
        if entry["repo"] == repo and entry["ruleset"] == ruleset:
            out.update(entry["settings"])
    return out


def expected_gate(policy: dict, repo: str, ruleset: str) -> str:
    for entry in policy.get("fast_gate_overrides", []):
        if entry["repo"] == repo and entry["ruleset"] == ruleset:
            return entry["context"]
    return policy["fast_gate"]["context"]


# --- GitHub (the `gh` CLI; the desk login) --------------------------------------------


class Gh:
    def graphql(self, query: str) -> dict:
        result = subprocess.run(["gh", "api", "graphql", "-f", "query=" + query], capture_output=True, text=True, timeout=180)
        if result.returncode != 0:
            raise _failure("graphql", result)
        data = json.loads(result.stdout)
        if data.get("errors"):
            raise RuntimeError("graphql: " + "; ".join(str(e.get("message")) for e in data["errors"][:3]))
        return data["data"]

    def rest(self, path: str, method: str = "GET", body: dict | None = None) -> dict:
        cmd = ["gh", "api", "-X", method, path] + (["--input", "-"] if body is not None else [])
        result = subprocess.run(cmd, input=json.dumps(body) if body is not None else None, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise _failure(f"{method} {path}", result)
        return json.loads(result.stdout)


def _failure(what: str, result: subprocess.CompletedProcess) -> Exception:
    text = (result.stderr or result.stdout or "").strip().splitlines()
    first = text[0] if text else "no output"
    if "403" in first or "HTTP 403" in (result.stderr or ""):
        return Forbidden(f"{what}: {first}")
    return RuntimeError(f"{what}: {first}")


QUERY = (
    'query{organization(login:"%s"){repositories(first:100,isArchived:false%s){pageInfo{hasNextPage endCursor} nodes{'
    "name isFork defaultBranchRef{name} rulesets(first:30){nodes{databaseId name enforcement target "
    "conditions{refName{include exclude}} rules(first:40){nodes{type parameters{"
    "... on MergeQueueParameters{mergeMethod groupingStrategy maxEntriesToBuild minEntriesToMerge maxEntriesToMerge "
    "minEntriesToMergeWaitMinutes checkResponseTimeoutMinutes} "
    "... on RequiredStatusChecksParameters{strictRequiredStatusChecksPolicy requiredStatusChecks{context integrationId}}"
    "}}}}}}}}}"
)


def read_org(gh, org: str) -> list[dict]:
    """Every ruleset that carries a merge_queue rule, one entry each: the live state."""
    out: list[dict] = []
    after = ""
    while True:
        data = gh.graphql(QUERY % (org, f',after:"{after}"' if after else ""))["organization"]["repositories"]
        for node in data["nodes"]:
            for rs in node["rulesets"]["nodes"]:
                rules = rs["rules"]["nodes"]
                queue = next((r for r in rules if r["type"] == "MERGE_QUEUE"), None)
                if queue is None:
                    continue
                checks = next((r for r in rules if r["type"] == "REQUIRED_STATUS_CHECKS"), None)
                cparams = (checks or {}).get("parameters") or {}
                out.append({
                    "repo": f"{org}/{node['name']}",
                    "id": rs["databaseId"],
                    "ruleset": rs["name"],
                    "enforcement": rs["enforcement"],
                    "refs": (rs.get("conditions") or {}).get("refName", {}).get("include", []),
                    "live": {k: queue["parameters"][g] for k, g in PARAMS.items()},
                    "required": [c["context"] for c in cparams.get("requiredStatusChecks", [])] if checks else None,
                    "strict": cparams.get("strictRequiredStatusChecksPolicy") if checks else None,
                })
        if not data["pageInfo"]["hasNextPage"]:
            return out
        after = data["pageInfo"]["endCursor"]


# --- plan -----------------------------------------------------------------------------


def plan(fleet: list[dict], policy: dict, skip: dict[str, str], only: set[str] | None = None) -> list[dict]:
    rows = []
    for item in sorted(fleet, key=lambda i: (i["repo"], i["ruleset"])):
        if only and item["repo"] not in only:
            continue
        row = {k: item[k] for k in ("repo", "id", "ruleset", "enforcement", "refs")}
        if item["repo"] in skip:
            row.update(status="EXCLUDED", reason=skip[item["repo"]], changes={})
            rows.append(row)
            continue
        want = desired_settings(policy, item["repo"], item["ruleset"])
        changes = {k: [item["live"][k], want[k]] for k in PARAMS if item["live"][k] != want[k]}
        gate = expected_gate(policy, item["repo"], item["ruleset"])
        notes = []
        if item["required"] is None:
            notes.append(f"no required_status_checks rule: fast gate {gate} is not required")
        elif gate not in item["required"]:
            notes.append(f"fast gate {gate} is not a required check (required: {', '.join(item['required']) or 'none'})")
        elif len(item["required"]) > 1:
            notes.append("also required: " + ", ".join(c for c in item["required"] if c != gate))
        if item["strict"]:
            notes.append("strict_required_status_checks_policy is on (not managed here)")
        row.update(status="DRIFT" if changes else "OK", changes=changes, notes=notes)
        rows.append(row)
    return rows


def render(rows: list[dict], skipped_orgs: list[str] | None = None) -> str:
    lines = []
    for r in rows:
        head = f"{r['status']:<8} {r['repo']} / {r['ruleset']} (#{r['id']}, {','.join(r['refs'])})"
        lines.append(head)
        if r["status"] == "EXCLUDED":
            lines.append(f"           excluded: {r['reason']}")
        for key, (old, new) in r["changes"].items():
            lines.append(f"           {key}: {old} -> {new}")
        for note in r.get("notes", []):
            lines.append(f"           note: {note}")
    count = lambda s: sum(1 for r in rows if r["status"] == s)
    lines.append(f"rulesets {len(rows)}: drift {count('DRIFT')}, ok {count('OK')}, excluded {count('EXCLUDED')}")
    return "\n".join(lines)


# --- write ----------------------------------------------------------------------------


def put_body(ruleset: dict) -> dict:
    return {k: ruleset[k] for k in PUT_KEYS if k in ruleset}


def with_queue(ruleset: dict, settings: dict) -> dict:
    body = put_body(ruleset)
    rules = []
    for rule in body["rules"]:
        if rule["type"] == "merge_queue":
            rule = {"type": "merge_queue", "parameters": {**rule["parameters"], **settings}}
        rules.append(rule)
    body["rules"] = rules
    return body


def queue_params(ruleset: dict) -> dict:
    rule = next(r for r in ruleset["rules"] if r["type"] == "merge_queue")
    return {k: rule["parameters"][k] for k in PARAMS}


def backup_path(backup_dir: Path, row: dict) -> Path:
    return backup_dir / f"{row['repo'].replace('/', '__')}__{row['id']}.json"


def apply(gh, rows: list[dict], policy: dict, backup_dir: Path, pause: float | None = None) -> list[dict]:
    """Write every DRIFT row; the pre-change ruleset is saved first and the write is read back."""
    drifted = [r for r in rows if r["status"] == "DRIFT"]
    taken = [backup_path(backup_dir, r).name for r in drifted if backup_path(backup_dir, r).exists()]
    if taken:
        # The first backup is the true "before"; a second apply must not replace it with a half-changed ruleset.
        raise ValueError(f"{backup_dir} already holds a backup for {', '.join(taken)}: use a new --backup-dir")
    backup_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for row in drifted:
        path = f"repos/{row['repo']}/rulesets/{row['id']}"
        outcome = {"repo": row["repo"], "id": row["id"], "ruleset": row["ruleset"]}
        try:
            before = gh.rest(path)
            backup_path(backup_dir, row).write_text(json.dumps(before, indent=2) + "\n")
            want = desired_settings(policy, row["repo"], row["ruleset"])
            after = gh.rest(path, "PUT", with_queue(before, want))
            if queue_params(after) != want:
                after = gh.rest(path)
            if queue_params(after) != want:
                outcome["result"] = "FAILED: read back differs"
            else:
                outcome["result"] = "APPLIED"
        except Forbidden as exc:
            outcome["result"] = f"STOPPED: {exc}"
            results.append(outcome)
            break
        except Exception as exc:  # one repository's failure does not hide the others
            outcome["result"] = f"FAILED: {exc}"
        results.append(outcome)
        time.sleep(WRITE_PAUSE_SECONDS if pause is None else pause)
    return results


def rollback(gh, backup_dir: Path, pause: float | None = None) -> list[dict]:
    results = []
    for file in sorted(Path(backup_dir).glob("*.json")):
        saved = json.loads(file.read_text())
        repo = file.name.rsplit("__", 1)[0].replace("__", "/")
        path = f"repos/{repo}/rulesets/{saved['id']}"
        outcome = {"repo": repo, "id": saved["id"], "ruleset": saved["name"]}
        try:
            # Only the queue parameters go back, onto the ruleset as it is now: any other edit made since --apply stays.
            current = gh.rest(path)
            if not any(r["type"] == "merge_queue" for r in current["rules"]):
                raise RuntimeError("the ruleset no longer has a merge_queue rule")
            after = gh.rest(path, "PUT", with_queue(current, queue_params(saved)))
            outcome["result"] = "RESTORED" if queue_params(after) == queue_params(saved) else "FAILED: read back differs"
        except Forbidden as exc:
            outcome["result"] = f"STOPPED: {exc}"
            results.append(outcome)
            break
        except Exception as exc:
            outcome["result"] = f"FAILED: {exc}"
        results.append(outcome)
        time.sleep(WRITE_PAUSE_SECONDS if pause is None else pause)
    return results


# --- main -----------------------------------------------------------------------------


def main(argv: list[str] | None = None, gh=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--policy", default=str(DEFAULT_POLICY))
    ap.add_argument("--repo", action="append", default=[], help="limit to ORG/NAME (repeatable)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print the diff, write nothing (the default)")
    mode.add_argument("--apply", action="store_true", help="write the drifted rulesets")
    mode.add_argument("--rollback", metavar="DIR", help="put the merge_queue parameters saved in DIR by --apply back")
    ap.add_argument("--backup-dir", help="where --apply saves each ruleset before writing it (required with --apply)")
    ap.add_argument("--check", action="store_true", help="exit 1 when any ruleset drifts")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    gh = gh or Gh()
    try:
        if args.rollback:
            results = rollback(gh, Path(args.rollback))
            print(json.dumps(results, indent=2) if args.json else "\n".join(f"{r['result']:<9} {r['repo']} / {r['ruleset']} (#{r['id']})" for r in results))
            return 1 if any(not r["result"].startswith("RESTORED") for r in results) or not results else 0
        policy = load_policy(Path(args.policy))
        skip = excluded(policy, hands_off_repos())
        fleet = [item for org in policy["orgs"] for item in read_org(gh, org)]
        rows = plan(fleet, policy, skip, set(args.repo) or None)
        if args.apply:
            if not args.backup_dir:
                ap.error("--apply needs --backup-dir")
            results = apply(gh, rows, policy, Path(args.backup_dir))
            print(json.dumps({"plan": rows, "results": results}, indent=2) if args.json else render(rows) + "\n" + "\n".join(
                f"{r['result']:<9} {r['repo']} / {r['ruleset']} (#{r['id']})" for r in results))
            return 1 if any(not r["result"].startswith("APPLIED") for r in results) else 0
        print(json.dumps(rows, indent=2) if args.json else render(rows))
        return 1 if args.check and any(r["status"] == "DRIFT" for r in rows) else 0
    except Forbidden as exc:
        print(f"stopped at the first 403: {exc}", file=sys.stderr)
        return 2
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
