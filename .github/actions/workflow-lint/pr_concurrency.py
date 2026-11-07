"""Prove PR supersession without sharing non-PR pending slots.

Only event predicates, format(), and interpolation are admitted. Identity values
are symbolic, not sampled IDs: comparisons/functions over IDs are refused. This
keeps unknown selectors fail-closed instead of mistaking examples for a proof.
"""
from __future__ import annotations

import re

import yaml

from merge_lane import Expression, UNKNOWN, events, expression

PR_EVENTS = {"pull_request", "pull_request_target"}
EXEMPT = re.compile(r"^#\s*pr-concurrency:\s*exempt\b(.*)$", re.M)
WORKFLOW = "\x00workflow\x00"
PR = "\x00pr\x00"
RUN = "\x00run\x00"


def evaluate(value, event):
    """Evaluate a restricted expression/template with opaque identity markers."""
    if not isinstance(value, str):
        return value
    context = {"github.event_name": {event}, "github.workflow": {WORKFLOW},
               "github.event.pull_request.number": {PR if event in PR_EVENTS else None},
               "github.run_id": {RUN}}

    def replace(match):
        source = match[1]
        parser = Expression(source, context)
        tokens = parser.tokens
        for i, token in enumerate(tokens):
            if token in ("==", "!="):
                # No tests against sampled PR numbers/run IDs or unknown data.
                operands = tokens[i - 1:i + 2:2]
                if len(operands) != 2 or "github.event_name" not in operands or not any(
                        operand.startswith("'") for operand in operands):
                    raise ValueError("only event-name comparisons are supported")
            if i + 1 < len(tokens) and tokens[i + 1] == "(" and token not in (
                    "format", "&&", "||", "!", "("):
                raise ValueError("only format() is supported")
        errors = []
        values = expression("${{" + source + "}}", context, errors=errors)
        if errors or UNKNOWN in values or len(values) != 1:
            raise ValueError("unresolved concurrency expression")
        result = next(iter(values))
        if not isinstance(result, (str, bool, int, float)):
            raise ValueError("non-scalar concurrency expression")
        return result

    matches = list(re.finditer(r"\$\{\{(.*?)\}\}", value, re.S))
    if len(matches) == 1 and matches[0].span() == (0, len(value)):
        return replace(matches[0])
    return re.sub(r"\$\{\{(.*?)\}\}", lambda m: str(replace(m)), value, flags=re.S)


def problems(text):
    """Return (line, problem) pairs; workflow_call-only runs inherit the caller."""
    document = yaml.compose(text, Loader=yaml.BaseLoader)
    fields = {k.value: v for k, v in document.value} if isinstance(document, yaml.MappingNode) else {}
    decoded = yaml.load(text, Loader=yaml.BaseLoader) or {}
    triggered = events(decoded)
    if not triggered & PR_EVENTS:
        return []
    # Only a column-zero header declaration counts, never a run-body comment.
    header = text[:fields.get("jobs", document).start_mark.index] if document else ""
    declaration = EXEMPT.search(header)
    if declaration:
        reason = declaration[1].strip(" \t:-—")
        if reason:
            return []
        return [(header[:declaration.start()].count("\n") + 1,
                 "pr-concurrency exemption must include a specific reason")]
    concurrency = decoded.get("concurrency")
    line = fields.get("concurrency", fields.get("on", document)).start_mark.line + 1
    error = ("PR concurrency must supersede per PR and isolate every non-PR run: "
             "use workflow-pr-<number> / workflow-run-<run_id> groups and PR-only "
             "cancel-in-progress, or # pr-concurrency: exempt — <specific reason>")
    if not isinstance(concurrency, dict):
        return [(line, error)]
    try:
        checked_events = triggered - {"workflow_call"}
        if "workflow_call" in triggered:
            # A reusable call inherits its caller's event, not workflow_call.
            # Supported predicates compare only event-name string constants;
            # each constant plus one other name exhausts those partitions.
            for value in concurrency.values():
                for source in re.findall(r"\$\{\{(.*?)\}\}", str(value), re.S):
                    tokens = Expression(source, {}).tokens
                    for i, token in enumerate(tokens):
                        if token in ("==", "!=") and i and i + 1 < len(tokens):
                            left, right = tokens[i - 1], tokens[i + 1]
                            literal = right if left == "github.event_name" else left if right == "github.event_name" else ""
                            if literal.startswith("'"):
                                checked_events.add(literal[1:-1].replace("''", "'"))
            other = "__other_caller_event__"
            while other in checked_events:
                other += "_"
            checked_events.add(other)
            checked_events.update(PR_EVENTS)
        for event in checked_events:
            group = evaluate(concurrency.get("group"), event)
            cancel = evaluate(concurrency.get("cancel-in-progress"), event)
            if cancel in ("true", "false"):
                cancel = cancel == "true"
            expected_pr = event in PR_EVENTS
            # Full identity markers survive format/interpolation unchanged. The
            # disjoint literal namespaces prevent PR/run numeric-ID collisions.
            identity = "-pr-" + PR if expected_pr else "-run-" + RUN
            forbidden = RUN if expected_pr else PR
            if (not isinstance(group, str) or WORKFLOW not in group or identity not in group
                    or forbidden in group or type(cancel) is not bool or cancel != expected_pr):
                return [(line, error)]
    except (ValueError, TypeError, IndexError):
        return [(line, error)]
    return []
