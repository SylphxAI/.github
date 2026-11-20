"""Require explicit bounded retention-days on actions/upload-artifact steps.

GitHub Actions artifact storage is a metered organization quota (GB-hours per
billing cycle). Omitting retention-days or setting retention-days: 0 falls back
to the repository/organization default (up to 90 days) and can exhaust the
organization quota. Prefer run-store for job hand-overs; when an
actions/upload-artifact step is used, require an explicit literal integer
retention-days from 1 to 3 (default 1), or up to 30 with a named, reasoned
header exception for release/proof artifacts.
"""
from __future__ import annotations

import re

import yaml

DEFAULT_RETENTION_DAYS = 1
MAX_RETENTION_DAYS = 3
MAX_EXCEPTION_RETENTION_DAYS = 30
ALLOWED_EXCEPTIONS = frozenset({"release-proof"})

UPLOAD_ARTIFACT = re.compile(r"^actions/upload-artifact(?:/[^@\s]*)?(?:@|\Z)", re.I)
POSITIVE_INT = re.compile(r"^[1-9][0-9]*\Z")
HEADER_MARKER = re.compile(r"^#[ \t]*artifact-retention:[ \t]*(.*)$", re.M | re.I)
HEADER_EXCEPTION = re.compile(
    r"^#[ \t]*artifact-retention:[ \t]*exception[ \t]+([\w-]+)(?:[ \t]+—[ \t]*|[ \t]*:[ \t]*)(.*)$"
)
RUN_STORE_GUIDANCE = (
    "use SylphxAI/.github/.github/actions/run-store "
    "(build-cache/Ceph RGW, $0 GitHub storage) for multi-MB CI handoffs"
)


def _mapping_items(node):
    return list(node.value) if isinstance(node, yaml.MappingNode) else []


def _scalar(node):
    return node.value if isinstance(node, yaml.ScalarNode) else ""


def _sequence(node):
    return list(node.value) if isinstance(node, yaml.SequenceNode) else []


def _field(node, name):
    for key, value in _mapping_items(node):
        if isinstance(key, yaml.ScalarNode) and key.value == name:
            return value
    return None


def _header_exception(text, document, body_key):
    """Parse column-zero header exceptions strictly preceding jobs:/runs:."""
    cutoff = len(text)
    if isinstance(document, yaml.MappingNode):
        for key, _ in document.value:
            if isinstance(key, yaml.ScalarNode) and key.value == body_key:
                cutoff = key.start_mark.index
                break
    header = text[:cutoff]
    findings = []
    has_release_proof = False
    invalid = False
    for match in HEADER_MARKER.finditer(header):
        line_no = header[:match.start()].count("\n") + 1
        parsed = HEADER_EXCEPTION.fullmatch(match.group(0))
        if (
            not parsed
            or parsed.group(1) not in ALLOWED_EXCEPTIONS
            or not parsed.group(2).strip(" \t:-—")
        ):
            findings.append((
                line_no,
                "artifact-retention exception must use "
                "'# artifact-retention: exception release-proof — <reason>' with a specific reason",
            ))
            invalid = True
        else:
            has_release_proof = True
    limit = MAX_EXCEPTION_RETENTION_DAYS if (has_release_proof and not invalid) else MAX_RETENTION_DAYS
    return limit, findings


def _check_steps(steps_node, scope, limit):
    findings = []
    for step in _sequence(steps_node):
        items = _mapping_items(step)
        uses_nodes = [
            value
            for key, value in items
            if isinstance(key, yaml.ScalarNode)
            and key.value == "uses"
            and isinstance(value, yaml.ScalarNode)
            and UPLOAD_ARTIFACT.match(value.value.strip())
        ]
        if not uses_nodes:
            continue
        uses_node = uses_nodes[0]
        with_entries = [
            (key, value)
            for key, value in items
            if isinstance(key, yaml.ScalarNode) and key.value == "with"
        ]
        if not with_entries:
            findings.append((
                uses_node.start_mark.line + 1,
                f"{scope}: actions/upload-artifact must set explicit integer retention-days "
                f"(default {DEFAULT_RETENTION_DAYS}, max {MAX_RETENTION_DAYS}); {RUN_STORE_GUIDANCE}",
            ))
            continue
        if len(with_entries) > 1:
            findings.append((
                with_entries[1][0].start_mark.line + 1,
                f"{scope}: actions/upload-artifact declares duplicate 'with' mappings",
            ))
            continue
        with_value = with_entries[0][1]
        if not isinstance(with_value, yaml.MappingNode):
            findings.append((
                with_value.start_mark.line + 1,
                f"{scope}: actions/upload-artifact must set explicit integer retention-days "
                f"(default {DEFAULT_RETENTION_DAYS}, max {MAX_RETENTION_DAYS}); {RUN_STORE_GUIDANCE}",
            ))
            continue
        retention_entries = [
            (key, value)
            for key, value in _mapping_items(with_value)
            if isinstance(key, yaml.ScalarNode) and key.value.lower() == "retention-days"
        ]
        if not retention_entries:
            findings.append((
                uses_node.start_mark.line + 1,
                f"{scope}: actions/upload-artifact must set explicit integer retention-days "
                f"(default {DEFAULT_RETENTION_DAYS}, max {MAX_RETENTION_DAYS}); {RUN_STORE_GUIDANCE}",
            ))
            continue
        if len(retention_entries) > 1:
            findings.append((
                retention_entries[1][0].start_mark.line + 1,
                f"{scope}: actions/upload-artifact declares duplicate retention-days inputs",
            ))
            continue
        ret_node = retention_entries[0][1]
        ret_line = ret_node.start_mark.line + 1
        if not isinstance(ret_node, yaml.ScalarNode):
            findings.append((
                ret_line,
                f"{scope}: actions/upload-artifact retention-days must be a literal integer "
                f"from 1 to {limit} (default {DEFAULT_RETENTION_DAYS}); {RUN_STORE_GUIDANCE}",
            ))
            continue
        raw = ret_node.value
        if "${{" in raw:
            findings.append((
                ret_line,
                f"{scope}: actions/upload-artifact retention-days must be a literal integer "
                f"from 1 to {limit} (default {DEFAULT_RETENTION_DAYS}); dynamic expressions are refused; "
                f"{RUN_STORE_GUIDANCE}",
            ))
            continue
        if raw == "0":
            findings.append((
                ret_line,
                f"{scope}: actions/upload-artifact retention-days: 0 uses the 90-day org default; "
                f"set an explicit literal integer from 1 to {limit} (default {DEFAULT_RETENTION_DAYS}) "
                f"or {RUN_STORE_GUIDANCE}",
            ))
            continue
        if not POSITIVE_INT.fullmatch(raw):
            findings.append((
                ret_line,
                f"{scope}: actions/upload-artifact retention-days must be a literal integer "
                f"from 1 to {limit} (default {DEFAULT_RETENTION_DAYS}); {RUN_STORE_GUIDANCE}",
            ))
            continue
        days = int(raw)
        if days > limit:
            if limit == MAX_EXCEPTION_RETENTION_DAYS:
                findings.append((
                    ret_line,
                    f"{scope}: actions/upload-artifact retention-days={days} exceeds max "
                    f"{MAX_EXCEPTION_RETENTION_DAYS} even with release-proof exception; "
                    f"{RUN_STORE_GUIDANCE}",
                ))
            else:
                findings.append((
                    ret_line,
                    f"{scope}: actions/upload-artifact retention-days={days} exceeds max "
                    f"{MAX_RETENTION_DAYS} (default {DEFAULT_RETENTION_DAYS}); lower to 1-{MAX_RETENTION_DAYS}, "
                    f"{RUN_STORE_GUIDANCE}, or declare "
                    f"'# artifact-retention: exception release-proof — <reason>' "
                    f"(max {MAX_EXCEPTION_RETENTION_DAYS})",
                ))
    return findings


def workflow_problems(text):
    """Return (line, problem) pairs for a workflow file."""
    document = yaml.compose(text, Loader=yaml.BaseLoader)
    limit, findings = _header_exception(text, document, "jobs")
    for top_key, top_value in _mapping_items(document):
        if _scalar(top_key) != "jobs":
            continue
        for job_key, job_node in _mapping_items(top_value):
            name = _scalar(job_key)
            for field_key, field_value in _mapping_items(job_node):
                if _scalar(field_key) == "steps":
                    findings.extend(_check_steps(field_value, f"job {name}", limit))
    return findings


def action_problems(text):
    """Return (line, problem) pairs for a composite action manifest."""
    document = yaml.compose(text, Loader=yaml.BaseLoader)
    limit, findings = _header_exception(text, document, "runs")
    for top_key, top_value in _mapping_items(document):
        if _scalar(top_key) != "runs":
            continue
        if _scalar(_field(top_value, "using")).strip().lower() != "composite":
            continue
        for field_key, field_value in _mapping_items(top_value):
            if _scalar(field_key) == "steps":
                findings.extend(_check_steps(field_value, "composite action", limit))
    return findings
