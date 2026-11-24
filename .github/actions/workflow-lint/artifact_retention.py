"""Enforce bounded upload retention and nonblocking diagnostic uploads."""
from __future__ import annotations

import re

import yaml

UPLOAD_ARTIFACT = re.compile(r"^actions/upload-artifact(?:/[^@\s]*)?(?:@|\Z)", re.I)
DOWNLOAD_ARTIFACT = re.compile(r"actions/download-artifact@[^\s]+", re.I)
# Match calls, not the contents of single-quoted GitHub expression strings.
ALWAYS = re.compile(r"'(?:[^']|'')*'|(?P<call>(?<![\w.])always\s*\(\s*\))", re.I)
FAILURE_ONLY = re.compile(r"failure\s*\(\s*\)", re.I)
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


def _check_steps(steps, scope, handoff_names=frozenset()):
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
        if (named_archive and _artifact_name(inputs) in handoff_names) or _protected(step):
            continue
        if any(match["call"] for match in ALWAYS.finditer(_condition(step))):
            yield (step["if"].start_mark.line + 1,
                   f"{prefix} diagnostic with always() lacks continue-on-error: true; "
                   f"use if: failure(), retention-days: 3 and continue-on-error: true; {UPLOAD_GUIDANCE}")


def _workflow_problems(document):
    jobs = {name: _fields(job) for name, job in _fields(_fields(document).get("jobs")).items()}
    steps = {name: [_fields(step) for step in _sequence(job.get("steps"))] for name, job in jobs.items()}
    handoffs = _handoffs(jobs, steps)
    for name, job_steps in steps.items():
        yield from _check_steps(job_steps, f"job {name}", handoffs[name])


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


def workflow_problems(text):
    return _problems(text, _workflow_problems)


def action_problems(text):
    return _problems(text, _action_problems)
