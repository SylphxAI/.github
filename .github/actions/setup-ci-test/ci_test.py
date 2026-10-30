#!/usr/bin/env python3
"""Run a test command and require fresh evidence of executed tests (stdlib only)."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Iterable
import xml.etree.ElementTree as ET


class EvidenceError(ValueError):
    """Missing, incomplete or contradictory runner evidence."""


@dataclass(frozen=True)
class Evidence:
    executed: int
    failed: int = 0


# Rust's stable pretty/terse formatters use the same terminal summary. Never
# count discovery, ignored, measured or filtered tests as executed assertions.
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
START = re.compile(r"running (\d+) tests?(?: \(shuffle seed: \d+\))?")
FINISH = re.compile(
    r"test result: (ok|FAILED)\. (\d+) passed; (\d+) failed; (\d+) ignored; "
    r"(\d+) measured; (\d+) filtered out(?:; finished in \d+(?:\.\d+)?s)?"
)


def libtest_evidence(lines: Iterable[str]) -> Evidence:
    pending = None
    summaries = executed = failed = 0
    for raw in lines:
        line = ANSI.sub("", raw).rstrip("\r\n")
        start = START.fullmatch(line)
        if start:
            if pending is not None:
                raise EvidenceError("libtest run has no terminal summary")
            pending = int(start[1])
        elif line.startswith("test result:"):
            finish = FINISH.fullmatch(line)
            if not finish or pending is None:
                raise EvidenceError("invalid or unpaired libtest summary")
            passed, failures, ignored, measured, _filtered = map(int, finish.groups()[1:])
            if passed + failures + ignored + measured != pending:
                raise EvidenceError("libtest summary disagrees with announced test count")
            if (finish[1] == "ok") != (failures == 0):
                raise EvidenceError("libtest summary contradicts failure count")
            executed += passed + failures
            failed += failures
            summaries += 1
            pending = None
    if pending is not None or not summaries:
        raise EvidenceError("missing or incomplete libtest run")
    return Evidence(executed, failed)


def junit_evidence(text: str) -> Evidence:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as error:
        raise EvidenceError(f"invalid JUnit XML: {error}") from error
    tag = lambda node: node.tag.rsplit("}", 1)[-1]
    if tag(root) not in {"testsuite", "testsuites"}:
        raise EvidenceError("JUnit root must be testsuite or testsuites")
    cases = [node for node in root.iter() if tag(node) == "testcase"]
    for parent in root.iter():
        if any(tag(child) == "testcase" for child in parent) and tag(parent) != "testsuite":
            raise EvidenceError("JUnit testcase must belong to a testsuite")
    for suite in root.iter():
        if tag(suite) not in {"testsuite", "testsuites"}:
            continue
        descendants = [node for node in suite.iter() if tag(node) == "testcase"]
        counts = {"tests": len(descendants)}
        for attribute, child_tag in (("skipped", "skipped"), ("failures", "failure"), ("errors", "error")):
            counts[attribute] = sum(any(tag(child) == child_tag for child in case) for case in descendants)
        for attribute, actual in counts.items():
            declared = suite.get(attribute)
            if declared is not None and (not re.fullmatch(r"[0-9]+", declared) or int(declared) != actual):
                raise EvidenceError(f"JUnit {attribute} count disagrees with actual testcase nodes")
    # Suite-level errors (e.g. collection failures) must also fail a zero-exit
    # command, even when another suite has passing cases.
    failed = sum(tag(node) in {"failure", "error"} for node in root.iter())
    executed = 0
    for case in cases:
        children = {tag(node) for node in case}
        if not case.get("name"):
            raise EvidenceError("JUnit testcase has no name")
        if "skipped" in children and children & {"failure", "error"}:
            raise EvidenceError("JUnit testcase is both skipped and failed")
        if "skipped" not in children:
            executed += 1
    return Evidence(executed, failed)


def vitest_evidence(text: str) -> Evidence:
    try:
        report = json.loads(text)
    except ValueError as error:
        raise EvidenceError(f"invalid Vitest JSON: {error}") from error
    if not isinstance(report, dict) or type(report.get("success")) is not bool:
        raise EvidenceError("Vitest report needs a boolean success verdict")
    suites = report.get("testResults")
    if not isinstance(suites, list):
        raise EvidenceError("Vitest report needs testResults")
    executed = failed = 0
    for suite in suites:
        if not isinstance(suite, dict) or suite.get("status") not in ("passed", "failed"):
            raise EvidenceError("invalid Vitest suite verdict")
        assertions = suite.get("assertionResults")
        if not isinstance(assertions, list):
            raise EvidenceError("Vitest suite needs assertionResults")
        failed += suite["status"] == "failed"
        for assertion in assertions:
            if not isinstance(assertion, dict):
                raise EvidenceError("invalid Vitest assertion")
            status = assertion.get("status")
            if status not in ("passed", "failed", "skipped", "pending", "todo", "disabled"):
                raise EvidenceError("unknown Vitest assertion status")
            # Pending includes queued/still-running tests, never execution.
            executed += status in {"passed", "failed"}
            failed += status == "failed"
    failed += not report["success"]
    return Evidence(executed, failed)


def empty_reason(text: str, format: str, shard: str, argv: list[str]) -> str | None:
    """An exception is an exact invocation, never a selector or blanket flag."""
    try:
        entries = json.loads(text)
    except ValueError as error:
        raise EvidenceError(f"invalid exceptions JSON: {error}") from error
    if not isinstance(entries, list):
        raise EvidenceError("exceptions must be a JSON array")
    reason = None
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"format", "shard", "argv", "reason"}:
            raise EvidenceError("each exception needs exactly format, shard, argv and reason")
        if entry["format"] not in ("libtest", "junit", "vitest"):
            raise EvidenceError("invalid exception format")
        if not isinstance(entry["shard"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", entry["shard"]):
            raise EvidenceError("exception shard must be an exact identifier")
        if not isinstance(entry["argv"], list) or not entry["argv"] or any(
            not isinstance(arg, str) or not arg for arg in entry["argv"]
        ):
            raise EvidenceError("exception argv must be a nonempty array of exact arguments")
        if not isinstance(entry["reason"], str) or not entry["reason"].strip():
            raise EvidenceError("exception reason must not be blank")
        if (entry["format"], entry["shard"], entry["argv"]) == (format, shard, argv):
            reason = entry["reason"].strip()
    return reason


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", required=True, choices=("libtest", "junit", "vitest"))
    parser.add_argument("--report", type=Path)
    parser.add_argument("--shard", required=True)
    parser.add_argument("--exceptions", type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if not args.command or args.command[0] != "--" or len(args.command) < 2:
        parser.error("expected -- COMMAND ARGS")
    command = args.command[1:]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", args.shard):
        parser.error("--shard must be an exact identifier")
    if (args.format == "libtest") == (args.report is not None):
        parser.error("--report is required for junit/vitest and unused for libtest")
    try:
        reason = empty_reason(args.exceptions.read_text(), args.format, args.shard, command) if args.exceptions else None
        if args.report:
            # Delete, rather than trusting mtime/resolution or a previous run's
            # valid file. A missing fresh report always fails, even if excepted.
            args.report.unlink(missing_ok=True)
        with tempfile.TemporaryFile() as log:
            try:
                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            except FileNotFoundError:
                print(f"ci-test [{args.shard}]: command not found: {command[0]}", file=sys.stderr)
                return 127
            except PermissionError:
                print(f"ci-test [{args.shard}]: command not executable: {command[0]}", file=sys.stderr)
                return 126
            with process.stdout:
                while chunk := process.stdout.read1(65536):
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()
                    log.write(chunk)
            status = process.wait()
            if status:
                print(f"ci-test [{args.shard}]: runner exit {status}", file=sys.stderr)
                return status if status > 0 else 128 - status
            if args.format == "libtest":
                log.seek(0)
                evidence = libtest_evidence(line.decode("utf-8", errors="replace") for line in log)
            elif args.format == "junit":
                evidence = junit_evidence(args.report.read_text())
            else:
                evidence = vitest_evidence(args.report.read_text())
        if evidence.failed:
            raise EvidenceError("report contains failures despite runner exit 0")
        if not evidence.executed:
            if not reason:
                raise EvidenceError("zero executed tests; declare this exact invocation if intentionally empty")
            print(f"ci-test [{args.shard}]: intentional empty invocation: {reason}")
        else:
            print(f"ci-test [{args.shard}]: {evidence.executed} executed tests")
        return 0
    except (EvidenceError, OSError, UnicodeError) as error:
        print(f"ci-test [{args.shard}]: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
