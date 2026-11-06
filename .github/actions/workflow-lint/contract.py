#!/usr/bin/env python3
"""Bound jobs, forbid manual gates and keep required failures visible.

Actionlint owns syntax and reference resolution. PyYAML's base nodes preserve
source locations and accept both block and flow YAML without implicit booleans.
Reusable-workflow call jobs cannot declare timeout-minutes in GitHub's schema;
the executable jobs in the called workflow are checked instead.
"""

from __future__ import annotations

import datetime
import os
import pathlib
import posixpath
import re
import shlex
import sys
from typing import NamedTuple

import yaml

from perf_gate import delivered

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / "scripts"))
from merge_lane import merge_lane_findings, runner_evaluations
from merge_lane_gate import exempt


class Finding(NamedTuple):
    file: str
    line: int
    problem: str
    blocking: bool = False


def fields(node):
    return {key.value: value for key, value in node.value} if isinstance(node, yaml.MappingNode) else {}


def scalar(node):
    return node.value if isinstance(node, yaml.ScalarNode) else ""


def sequence(node):
    return node.value if isinstance(node, yaml.SequenceNode) else []


def dependencies(job):
    node = fields(job).get("needs")
    return [scalar(n) for n in sequence(node)] if isinstance(node, yaml.SequenceNode) else [scalar(node)]


def masks_failure(command):
    lexer = shlex.shlex(command.replace("\\\n", ""), posix=False, punctuation_chars="|&;")
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        # Non-shell run bodies can contain unmatched shell quotes. Still check
        # the explicit failure-swallowing idiom without making shell syntax a gate.
        return "|| true" in command or "||true" in command
    return any(a == "||" and b.strip("\"'") == "true" for a, b in zip(tokens, tokens[1:]))


def required_jobs(jobs):
    roots = []
    for name, node in jobs.items():
        job = fields(node)
        actions = [scalar(fields(step).get("uses")) for step in sequence(job.get("steps"))]
        if name == "ci-ok" or scalar(job.get("name")) == "ci-ok" or any(
            any(f"/actions/{aggregate}@" in action for aggregate in ("needs-pass", "ci-ok", "ci-ok-polling"))
            for action in actions
        ):
            roots.append(name)
    # ci-ok can aggregate all checks via the API, with no local needs graph.
    if not roots or any(not scalar(fields(jobs[root]).get("needs")) and
                        not sequence(fields(jobs[root]).get("needs")) for root in roots):
        return set(jobs)
    required = set()
    pending = roots[:]
    while pending:
        name = pending.pop()
        if name in required or name not in jobs:
            continue
        required.add(name)
        pending.extend(dependencies(jobs[name]))
    return required


def scan_workflow(file, text, evaluations=None):
    document = yaml.compose(text, Loader=yaml.BaseLoader)
    jobs = fields(fields(document).get("jobs"))
    required = required_jobs(jobs)
    findings = []
    if evaluations is None:
        evaluations = runner_evaluations({file: text})
        for finding in merge_lane_findings({}, evaluations=evaluations, check_all_events=True):
            findings.append(Finding(file, finding.line,
                f"job {finding.job}: {finding.problem}", True))

    def add(node, problem, *, blocking=False):
        findings.append(Finding(file, node.start_mark.line + 1, problem, blocking))

    for name, node in jobs.items():
        job = fields(node)
        routes = [e for e in evaluations if e.job == name and
                  e.file == file.removeprefix('.github/workflows/')]
        scan_io_job(document, name, node, add, routes)
        if "uses" not in job and "timeout-minutes" not in job:
            add(node, f"job {name}: missing timeout-minutes")
        # Manual gates are forbidden even outside the required needs graph.
        for part in [node, *sequence(job.get("steps"))]:
            properties = fields(part)
            action = properties.get("uses")
            if "manual-approval" in scalar(action).lower():
                add(action, f"job {name}: manual approval gate is forbidden; deployments and checks must run automatically", blocking=True)
            for key in ("if", "run"):
                gate = properties.get(key)
                value = scalar(gate).lower()
                if "window:booked" in value or "maintenance-window" in value:
                    add(gate, f"job {name}: manual maintenance-window booking (window:booked) is forbidden; use rolling updates or workload-scheduled automatic windows", blocking=True)
            if name not in required:
                continue
            ignore = properties.get("continue-on-error")
            if ignore is not None and scalar(ignore).lower() != "false":
                add(ignore, f"job {name}: continue-on-error can hide a required failure")
            run = properties.get("run")
            if run is not None and masks_failure(scalar(run)):
                add(run, f"job {name}: || true hides a required failure")
    return findings


IO_LABEL = "sylphx-linux-io"
SHA = re.compile(r"[0-9a-fA-F]{40}\Z")
FORK_GUARD = "github.event.workflow_run.head_repository.full_name==github.repository"


def node_text(node):
    if isinstance(node, yaml.ScalarNode):
        return node.value
    if isinstance(node, yaml.SequenceNode):
        return " ".join(node_text(n) for n in node.value)
    return " ".join(node_text(n) for n in fields(node).values())


def split_expression(value, operator):
    """Split only top-level operators, respecting strings and parentheses."""
    parts, start, depth, quote, index = [], 0, 0, False, 0
    while index < len(value):
        char = value[index]
        if char == "'":
            if quote and index + 1 < len(value) and value[index + 1] == "'":
                index += 2
                continue
            quote = not quote
        elif not quote:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            elif depth == 0 and value.startswith(operator, index):
                parts.append(value[start:index].strip())
                index += len(operator)
                start = index
                continue
        index += 1
    return parts + [value[start:].strip()]


def unwrap_expression(value):
    value = value.strip()
    if value.startswith("${{") and value.endswith("}}"):
        value = value[3:-2].strip()
    while value.startswith("(") and value.endswith(")"):
        # Only strip parentheses enclosing the entire expression.
        depth, quote, closes_early = 0, False, False
        for index, char in enumerate(value):
            if char == "'":
                quote = not quote
            elif not quote:
                depth += (char == "(") - (char == ")")
                if depth == 0 and index < len(value) - 1:
                    closes_early = True
                    break
        if closes_early:
            break
        value = value[1:-1].strip()
    return value


def requires_same_repository(value):
    value = unwrap_expression(value)
    alternatives = split_expression(value, "||")
    if len(alternatives) > 1:
        return all(requires_same_repository(part) for part in alternatives)
    terms = split_expression(value, "&&")
    if len(terms) > 1:
        return any(requires_same_repository(part) for part in terms)
    return re.sub(r"\s+", "", value) == FORK_GUARD


def guarded_io_route(value, guarded=False):
    value = unwrap_expression(value)
    alternatives = split_expression(value, "||")
    if len(alternatives) > 1:
        return all(guarded_io_route(part, guarded) for part in alternatives)
    terms = split_expression(value, "&&")
    if len(terms) > 1:
        guarded = guarded or any(requires_same_repository(part) for part in terms)
        return all(guarded_io_route(part, guarded) for part in terms)
    return IO_LABEL not in value or guarded


def scan_io_job(document, name, node, add, routes):
    job = fields(node)
    runner = job.get("runs-on")
    route = node_text(runner)
    matrix_io = "matrix." in route and IO_LABEL in node_text(job.get("strategy"))
    io_events = {e.event for e in routes if IO_LABEL in e.values}
    triggers = fields(document).get("on")
    event_names = set(fields(triggers)) or {scalar(n) for n in sequence(triggers)} or {scalar(triggers)}
    if not routes and event_names - {'workflow_call', ''}:
        return
    if routes and not io_events:
        return
    if not routes and IO_LABEL not in route and not matrix_io:
        return

    def fail(where, problem):
        add(where, f"job {name}: io pool: {problem}", blocking=True)

    steps = sequence(job.get("steps"))
    checkouts = []
    for step in steps:
        props = fields(step)
        if "run" in props:
            fail(props["run"], "inline run is forbidden; use SHA-pinned actions")
        action = props.get("uses")
        target = scalar(action)
        if not action:
            continue
        if target.startswith("./"):
            path = posixpath.normpath(target)
            providers = [(base, trusted) for base, trusted in checkouts
                         if path == base or path.startswith(base + "/") or base == "."]
            overwritten = any(not trusted and base.startswith(path + "/")
                              for base, trusted in checkouts)
            if (path.startswith("../") or path == ".." or overwritten or not providers
                    or not max(providers, key=lambda p: len(p[0]))[1]):
                fail(action, "local action requires a preceding SylphxAI/.github checkout with a 40-hex ref")
        elif not SHA.fullmatch(target.rpartition("@")[2]):
            fail(action, "remote uses must be pinned to a 40-hex SHA")
        if target.lower().startswith("actions/checkout@"):
            options = fields(props.get("with"))
            base = posixpath.normpath(scalar(options.get("path")) or ".")
            if "${{" in base or base.startswith(("../", "/")) or base == "..":
                # The destination may overlap any previously trusted checkout.
                checkouts = [(".", False)]
                continue
            trusted = (scalar(options.get("repository")) == "SylphxAI/.github"
                       and bool(SHA.fullmatch(scalar(options.get("ref"))))
                       and "if" not in props and "continue-on-error" not in props
                       and scalar(options.get("clean")) != "false"
                       and "${{" not in base and not base.startswith(("../", "/")) and base != "..")
            # A checkout also replaces provenance below its destination.
            checkouts = [(p, t) for p, t in checkouts
                         if base != "." and p != base and not p.startswith(base + "/")]
            checkouts.append((base, trusted))

    workflow_run = ("workflow_run" in fields(triggers)
                    or scalar(triggers) == "workflow_run"
                    or any(scalar(n) == "workflow_run" for n in sequence(triggers)))
    if workflow_run and (not routes or 'workflow_run' in io_events) and not requires_same_repository(scalar(job.get("if"))):
        if matrix_io or not guarded_io_route(route):
            fail(runner, "workflow_run io routing requires head_repository.full_name == github.repository")

    permissions = fields(job.get("permissions"))
    if scalar(permissions.get("contents")) != "read":
        fail(job.get("permissions") or node, "declare job-level minimal permissions with contents: read")
    reviewer = any(re.fullmatch(
        r"(?:SylphxAI/\.github/|\./(?:[^/]+/)*)\.github/actions/pr-review(?:@[0-9a-fA-F]{40})?",
        scalar(fields(step).get("uses"))) for step in steps)
    for permission, value in permissions.items():
        level = scalar(value)
        allowed = (permission in {"contents", "actions", "pull-requests", "checks", "statuses"}
                   and level in {"read", "none"})
        allowed |= (reviewer and permission in {"id-token", "checks", "statuses", "pull-requests"}
                    and level == "write")
        if not allowed:
            fail(value, f"permission {permission}: {level} is outside minimal reviewer permissions")

    allowed_secrets = {name.upper() for name in re.split(
        r"[\s,]+", os.environ.get("IO_REVIEWER_SECRETS", "")) if name}
    # Workflow env/defaults are inherited, so scan them as well as the full job.
    nodes = [node, fields(document).get("env"), fields(document).get("defaults")]
    for scope in nodes:
        text = node_text(scope)
        for expression in re.findall(r"\$\{\{(.*?)\}\}", text, re.S):
            remainder = re.sub(r"\bsecrets\s*\.\s*([A-Za-z_][A-Za-z_0-9]*)",
                lambda m: "" if reviewer and m[1].upper() in allowed_secrets else m[0],
                expression, flags=re.I)
            if re.search(r"\bsecrets\b", remainder, re.I):
                fail(scope, "repository secrets are forbidden except configured reviewer token names")
                break


def scan_dir(root):
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    entry = exempt(repository, datetime.datetime.now(datetime.timezone.utc).date()) if repository else None
    if entry and entry['class'] in ('hands-off', 'frozen-legacy', 'paused-or-stopped'):
        return []
    folder = root / ".github/workflows"
    findings = []
    workflows = {}
    if folder.is_dir():
        for path in sorted(folder.iterdir()):
            if path.is_file() and path.suffix in (".yml", ".yaml"):
                text = path.read_text(encoding="utf-8")
                workflows[path.name] = text
    # The required verdict repeats R8 against immutable source without relying
    # on checkout. Here it also fails early for workflow-lint consumers, even
    # when unrelated legacy contract findings are report-only.
    private = None
    try:
        import json
        event = json.loads(pathlib.Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        private = event.get("repository", {}).get("private")
    except (KeyError, OSError, ValueError):
        pass
    notes = []
    evaluations = runner_evaluations(workflows, private=private)
    for name, text in workflows.items():
        findings.extend(scan_workflow('.github/workflows/' + name, text, evaluations))
    if entry:
        return findings
    for finding in merge_lane_findings(workflows, private=private,
            org=repository.split("/")[0] if repository else None, notes=notes,
            evaluations=evaluations, check_all_events=True):
        findings.append(Finding(".github/workflows/" + finding.file, finding.line,
            f"job {finding.job}: {finding.problem}", True))
    for note in notes:
        print(f"::notice file=.github/workflows/{note.file},line={note.line}::job {note.job}: {note.problem}")
    return findings


def exit_code(findings, mode):
    if mode not in ("enforce", "report-only"):
        raise ValueError(f"unknown contract mode: {mode}")
    return int(any(f.blocking for f in findings) or (bool(findings) and mode == "enforce"))


def main():
    mode = os.environ.get("CONTRACT_MODE", "enforce")
    exit_code([], mode)
    state = delivered(dict(os.environ))
    if state is True or (state is None and os.environ.get("GITHUB_ACTIONS") == "true"):
        print("workflow-contract: delivered property is delivered or unreadable; skipped")
        return 0
    findings = scan_dir(pathlib.Path.cwd())
    for finding in findings:
        level = "error" if finding.blocking or mode == "enforce" else "warning"
        print(f"::{level} file={finding.file},line={finding.line}::{finding.problem}")
    print(f"workflow-contract: {len(findings)} violation(s), mode={mode}")
    return exit_code(findings, mode)


if __name__ == "__main__":
    sys.exit(main())
