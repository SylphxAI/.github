"""Enforce bounded upload retention and nonblocking diagnostic uploads."""
from __future__ import annotations

import re

import yaml

UPLOAD_ARTIFACT = re.compile(r"^actions/upload-artifact(?:/[^@\s]*)?(?:@|\Z)", re.I)
DOWNLOAD_ARTIFACT = re.compile(r"actions/download-artifact@[^\s]+", re.I)
# Match calls, not the contents of single-quoted GitHub expression strings.
ALWAYS = re.compile(r"'(?:[^']|'')*'|(?P<call>(?<![\w.])always\s*\(\s*\))", re.I)
FAILURE_ONLY = re.compile(r"failure\s*\(\s*\)", re.I)
DELIVERY_EVENTS = frozenset({"release", "workflow_call", "workflow_dispatch"})
LOCAL_CALL = re.compile(r"\./\.github/workflows/([^/\\]+\.ya?ml)")
UPLOAD_GUIDANCE = (
    "use retention-days: 1 for cross-job handoffs, 3–7 for release binaries, "
    "or use SylphxAI/.github/.github/actions/run-store "
    "(build-cache/Ceph RGW, $0 GitHub storage) for multi-MB CI handoffs"
)


class AmbiguousMapping(ValueError):
    def __init__(self, key, problem):
        self.line = key.start_mark.line + 1
        super().__init__(problem)


def _scalar(node):
    return node.value if isinstance(node, yaml.ScalarNode) else ""


def _sequence(node):
    return node.value if isinstance(node, yaml.SequenceNode) else []


def _fields(node):
    fields = {}
    if not isinstance(node, yaml.MappingNode):
        return fields
    for key, value in node.value:
        name = _scalar(key).lower()
        if not name or name == "<<":
            raise AmbiguousMapping(key, "artifact policy cannot resolve a non-literal or YAML merge key")
        if name in fields:
            raise AmbiguousMapping(key, f"artifact policy cannot resolve duplicate '{name}' keys")
        fields[name] = value
    return fields


def _condition(step):
    text = _scalar(step.get("if")).strip()
    if text.startswith("${{") and text.endswith("}}"):
        return text[3:-2].strip()
    return text


def _protected(step):
    node = step.get("continue-on-error")
    return (_scalar(node).lower() == "true" and node.style is None
            and node.tag == "tag:yaml.org,2002:bool")


def is_failure_only_diagnostic(node):
    """A failed diagnostic upload cannot mask the failure that enabled it."""
    try:
        step = _fields(node)
        return (UPLOAD_ARTIFACT.match(_scalar(step.get("uses")).strip()) is not None
                and FAILURE_ONLY.fullmatch(_condition(step)) is not None
                and _protected(step)
                and _scalar(_fields(step.get("with")).get("retention-days")) == "3")
    except AmbiguousMapping:
        # The artifact rule reports this; do not relax the failure-masking rule.
        return False


def _artifact_name(inputs):
    name = _scalar(inputs.get("name")).strip()
    return name if "${{" not in name else ""


def _ancestors(job_id, jobs):
    pending = [job_id]
    seen = {job_id}
    while pending:
        needs = jobs[pending.pop()].get("needs")
        for need in _sequence(needs) if isinstance(needs, yaml.SequenceNode) else [needs]:
            name = _scalar(need).lower()
            if name in jobs and name not in seen:
                seen.add(name)
                pending.append(name)
                yield name


def _handoffs(jobs, steps):
    """Index named same-run downloads by upstream job and artifact name."""
    handoffs = {job_id: set() for job_id in jobs}
    for job_id, job_steps in steps.items():
        for step in job_steps:
            if not DOWNLOAD_ARTIFACT.fullmatch(_scalar(step.get("uses")).strip()):
                continue
            inputs = _fields(step.get("with"))
            name = _artifact_name(inputs)
            if not name or inputs.keys() & {"artifact-ids", "github-token", "repository", "run-id"}:
                continue
            if any(_condition(fields).lower() == "false" for fields in (jobs[job_id], step)):
                continue
            for producer in _ancestors(job_id, jobs):
                handoffs[producer].add(name)
    return handoffs


def _check_steps(steps, scope, handoff_names=frozenset(), *, diagnostic_check=True):
    for step in steps:
        uses = step.get("uses")
        if not UPLOAD_ARTIFACT.match(_scalar(uses).strip()):
            continue
        inputs = _fields(step.get("with"))
        retention = inputs.get("retention-days")
        raw = _scalar(retention)
        prefix = f"{scope}: actions/upload-artifact"
        if retention is None:
            yield (uses.start_mark.line + 1, f"{prefix} must set explicit integer retention-days from 1 to 7; "
                   f"omission uses the repository/org default (up to 90 days); {UPLOAD_GUIDANCE}")
        elif not re.fullmatch(r"[1-7]", raw):
            if raw == "0":
                reason = "retention-days: 0 uses the 90-day org default"
            elif re.fullmatch(r"[1-9][0-9]*", raw):
                reason = f"retention-days={raw} exceeds max 7"
            else:
                reason = "retention-days must be a literal integer from 1 to 7; dynamic expressions are refused"
            yield retention.start_mark.line + 1, f"{prefix} {reason}; {UPLOAD_GUIDANCE}"

        # With archive: false the uploader uses the filename, not with.name.
        named_archive = "archive" not in inputs or _scalar(inputs["archive"]).lower() == "true"
        if (not diagnostic_check or _protected(step)
                or (named_archive and _artifact_name(inputs) in handoff_names)):
            continue
        if any(match["call"] for match in ALWAYS.finditer(_condition(step))):
            yield (step["if"].start_mark.line + 1,
                   f"{prefix} diagnostic with always() lacks continue-on-error: true; "
                   f"use if: failure(), retention-days: 3 and continue-on-error: true; {UPLOAD_GUIDANCE}")


def _trigger_events(node):
    if isinstance(node, yaml.MappingNode):
        return set(_fields(node))
    if isinstance(node, yaml.SequenceNode):
        return {_scalar(event) for event in node.value}
    return {_scalar(node)}


def _workflow(document):
    root = _fields(document)
    triggers = _trigger_events(root.get("on"))
    delivery_only = bool(triggers) and triggers <= DELIVERY_EVENTS
    jobs = {name: _fields(job) for name, job in _fields(root.get("jobs")).items()}
    return delivery_only, jobs


def ci_workflows(workflows):
    """Conservatively propagate CI reachability through local reusable calls."""
    calls = {}
    strict = set()
    for name, text in workflows.items():
        try:
            delivery_only, jobs = _workflow(yaml.compose(text, Loader=yaml.SafeLoader))
        except (AmbiguousMapping, yaml.YAMLError):
            # The normal manifest scan rejects malformed source.
            continue
        if not delivery_only:
            strict.add(name)
        calls[name] = {target[1] for job in jobs.values()
                       if (target := LOCAL_CALL.fullmatch(_scalar(job.get("uses")).strip()))}
    pending = list(strict)
    while pending:
        for callee in calls.get(pending.pop(), ()):
            if callee not in strict:
                strict.add(callee)
                pending.append(callee)
    return strict


def _workflow_problems(document, ci_caller):
    delivery_only, jobs = _workflow(document)
    steps = {name: [_fields(step) for step in _sequence(job.get("steps"))] for name, job in jobs.items()}
    diagnostic_check = ci_caller or not delivery_only
    handoffs = _handoffs(jobs, steps) if diagnostic_check else {}
    for name, job_steps in steps.items():
        yield from _check_steps(job_steps, f"job {name}", handoffs.get(name, ()),
                                diagnostic_check=diagnostic_check)


def _action_problems(document):
    runs = _fields(_fields(document).get("runs"))
    if _scalar(runs.get("using")).lower() == "composite":
        yield from _check_steps((_fields(step) for step in _sequence(runs.get("steps"))), "composite action")


def _problems(text, check):
    findings = []
    try:
        findings.extend(check(yaml.compose(text, Loader=yaml.SafeLoader)))
    except AmbiguousMapping as error:
        findings.append((error.line, str(error)))
    except yaml.MarkedYAMLError as error:
        line = error.problem_mark.line + 1 if error.problem_mark else 1
        findings.append((line, f"artifact policy could not parse YAML: {error.problem}"))
    return findings


def workflow_problems(text, *, ci_caller=False):
    return _problems(text, lambda document: _workflow_problems(document, ci_caller))


def action_problems(text):
    return _problems(text, _action_problems)
