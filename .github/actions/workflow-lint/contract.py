#!/usr/bin/env python3
"""Bound workflow jobs and forbid swallowed failures on the required needs graph.

Actionlint owns syntax and reference resolution. PyYAML's base nodes preserve
source locations and accept both block and flow YAML without implicit booleans.
Reusable-workflow call jobs cannot declare timeout-minutes in GitHub's schema;
the executable jobs in the called workflow are checked instead.
"""

from __future__ import annotations

import os
import pathlib
import shlex
import sys
from typing import NamedTuple

import yaml

from perf_gate import delivered


class Finding(NamedTuple):
    file: str
    line: int
    problem: str


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
            "/actions/needs-pass@" in action or "/actions/ci-ok@" in action for action in actions
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


def scan_workflow(file, text):
    document = yaml.compose(text, Loader=yaml.BaseLoader)
    jobs = fields(fields(document).get("jobs"))
    required = required_jobs(jobs)
    findings = []

    def add(node, problem):
        findings.append(Finding(file, node.start_mark.line + 1, problem))

    for name, node in jobs.items():
        job = fields(node)
        if "uses" not in job and "timeout-minutes" not in job:
            add(node, f"job {name}: missing timeout-minutes")
        if name not in required:
            continue
        for part in [node, *sequence(job.get("steps"))]:
            properties = fields(part)
            ignore = properties.get("continue-on-error")
            if ignore is not None and scalar(ignore).lower() != "false":
                add(ignore, f"job {name}: continue-on-error can hide a required failure")
            run = properties.get("run")
            if run is not None and masks_failure(scalar(run)):
                add(run, f"job {name}: || true hides a required failure")
    return findings


def scan_dir(root):
    folder = root / ".github/workflows"
    findings = []
    if folder.is_dir():
        for path in sorted(folder.iterdir()):
            if path.is_file() and path.suffix in (".yml", ".yaml"):
                findings.extend(scan_workflow(str(path.relative_to(root)), path.read_text(encoding="utf-8")))
    return findings


def exit_code(findings, mode):
    if mode not in ("enforce", "report-only"):
        raise ValueError(f"unknown contract mode: {mode}")
    return int(bool(findings) and mode == "enforce")


def main():
    mode = os.environ.get("CONTRACT_MODE", "enforce")
    exit_code([], mode)
    state = delivered(dict(os.environ))
    if state is True or (state is None and os.environ.get("GITHUB_ACTIONS") == "true"):
        print("workflow-contract: delivered property is delivered or unreadable; skipped")
        return 0
    findings = scan_dir(pathlib.Path.cwd())
    level = "error" if mode == "enforce" else "warning"
    for finding in findings:
        print(f"::{level} file={finding.file},line={finding.line}::{finding.problem}")
    print(f"workflow-contract: {len(findings)} violation(s), mode={mode}")
    return exit_code(findings, mode)


if __name__ == "__main__":
    sys.exit(main())
