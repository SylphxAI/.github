#!/usr/bin/env python3
"""No timing or performance budget gates a pull request or the merge queue.

Shared runners move wall-clock numbers with load, so a budget checked before
merge fails changes that touched nothing it measures. Timing gates run after
merge, as the median of several rounds against a stored baseline; before merge
a workflow may measure and print numbers, but not fail on them
(SylphxAI/owner standards/dx.md, continuous integration).

A workflow is pre-merge when it triggers on pull_request,
pull_request_target or merge_group, or is reusable (workflow_call), since a
pre-merge workflow may call it. In such a workflow this refuses:
  - PERF_ENFORCE set to anything but 0 (an env key or inline PERF_ENFORCE=1);
  - a line that runs a timing gate: Lighthouse (or lhci), hyperfine, k6 run,
    or the chat perf run (perf:web, --perf-only);
  - a package-manager vulnerability audit: its mutable advisory database can
    fail unchanged code. Launched products audit the default branch in a
    separate scheduled workflow; unlaunched products defer to the launch gate.

The action supplies a pinned YAML parser. Inspect composed BaseLoader nodes:
scalar values have already been decoded, keys such as `on` stay strings, and
source marks remain available without constructing tagged Python objects.

A delivered customer repository (organization custom property
sylphx_delivery = delivered) gets no new automated rule: the check is skipped
there, read from the event payload or, when the payload lacks the properties,
from the repository's property values.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import urllib.request
from typing import Callable, NamedTuple

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

PRE_MERGE = ("pull_request", "pull_request_target", "merge_group", "workflow_call")

GATES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:bun|npm|pnpm|yarn)\s+(?:--[^\s]+\s+)*audit\b"),
     "a mutable-registry dependency audit"),
    (re.compile(r"\bperf:web\b"), "the chat timing gate (perf:web)"),
    (re.compile(r"--perf-only\b"), "the chat timing gate (--perf-only)"),
    (re.compile(r"\bPERF_ENFORCE=(?![\"']?0\b)"), "PERF_ENFORCE set inline"),
    (re.compile(r"lighthouse|\blhci\b", re.IGNORECASE), "a Lighthouse budget"),
    (re.compile(r"\bhyperfine\b"), "a hyperfine timing run"),
    (re.compile(r"\bk6\s+run\b"), "a k6 load test"),
)

# Only executable run/uses fields are timing or audit commands.


class Finding(NamedTuple):
    file: str
    line: int
    where: str
    problem: str


def _strip_comment(line: str) -> str:
    if line.lstrip().startswith("#"):
        return ""
    return re.sub(r"\s+#.*$", "", line)


def mapping(node: Node | None) -> dict[str, Node]:
    if not isinstance(node, MappingNode):
        return {}
    return {key.value: value for key, value in node.value if isinstance(key, ScalarNode)}


def scalar(node: Node | None) -> str:
    return node.value if isinstance(node, ScalarNode) else ""


def event_names(root: Node | None) -> list[str]:
    event = mapping(root).get("on")
    if isinstance(event, MappingNode):
        return list(mapping(event))
    if isinstance(event, SequenceNode):
        return [scalar(child) for child in event.value]
    return [scalar(event)] if event is not None else []


def triggers(lines: list[str]) -> list[str]:
    """The event names under the top-level `on:` key, without bool coercion."""
    return event_names(yaml.compose("\n".join(lines), Loader=yaml.BaseLoader))


def excludes_pre_merge(condition: str) -> bool:
    """Prove a conjunction excludes every caller event; unknown forms stay gated."""
    condition = condition.strip()
    if condition.startswith("${{") and condition.endswith("}}"):
        condition = condition[3:-2].strip()
    if "||" in condition:
        return False
    terms = condition.split("&&")
    excluded = set()
    for term in terms:
        match = re.fullmatch(r"\s*github\.event_name\s*!=\s*(['\"])([a-z_]+)\1\s*", term)
        if match:
            excluded.add(match.group(2))
    return set(PRE_MERGE) - {"workflow_call"} <= excluded


def scan_workflow(file: str, text: str) -> list[Finding]:
    root = yaml.compose(text, Loader=yaml.BaseLoader)
    pre = [t for t in event_names(root) if t in PRE_MERGE]
    if not pre:
        return []
    kind = "/".join(pre)
    out: list[Finding] = []

    def inspect(fields: dict[str, Node], where: str) -> None:
        enforce = mapping(fields.get("env")).get("PERF_ENFORCE")
        if enforce is not None and scalar(enforce).strip().lower() not in ("0", "false"):
            out.append(Finding(file, enforce.start_mark.line + 1, where,
                               f"PERF_ENFORCE is {json.dumps(scalar(enforce).strip())}; "
                               "a pre-merge workflow may only set it to '0'"))
        for key in ("run", "uses"):
            node = fields.get(key)
            if not isinstance(node, ScalarNode):
                continue
            # YAML folding/escapes are resolved before matching. Shell comments
            # remain non-executable; YAML comments were removed by the parser.
            command = "\n".join(_strip_comment(line) for line in node.value.splitlines())
            for pattern, what in GATES:
                if pattern.search(command):
                    out.append(Finding(file, node.start_mark.line + 1, where,
                                       f"runs {what} in a {kind} workflow"))
                    break

    workflow = mapping(root)
    inspect({"env": workflow["env"]} if "env" in workflow else {}, "env")
    for job, node in mapping(workflow.get("jobs")).items():
        fields = mapping(node)
        # A job-level condition can appear anywhere in the mapping. Conditions
        # on steps never exempt the job or its other commands.
        if excludes_pre_merge(scalar(fields.get("if"))):
            continue
        where = f"job {job}"
        inspect(fields, where)
        steps = fields.get("steps")
        if isinstance(steps, SequenceNode):
            for step in steps.value:
                inspect(mapping(step), where)
    return out


def scan_dir(root: pathlib.Path) -> list[Finding]:
    folder = root / ".github" / "workflows"
    if not folder.is_dir():
        return []
    out: list[Finding] = []
    for path in sorted(folder.iterdir()):
        if path.suffix in (".yml", ".yaml") and path.is_file():
            out += scan_workflow(f".github/workflows/{path.name}", path.read_text(encoding="utf-8"))
    return out


def _api_properties(repo: str, token: str, api: str) -> list[dict]:
    req = urllib.request.Request(
        f"{api}/repos/{repo}/properties/values",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def delivered(env: dict[str, str],
              fetch: Callable[[str, str, str], list[dict]] = _api_properties) -> bool | None:
    """True when the repository is a delivered customer project, None when unreadable."""
    event_path = env.get("GITHUB_EVENT_PATH", "")
    if event_path and os.path.isfile(event_path):
        try:
            repository = json.loads(pathlib.Path(event_path).read_text()).get("repository") or {}
        except (OSError, ValueError):
            repository = {}
        props = repository.get("custom_properties")
        if isinstance(props, dict):
            return props.get("sylphx_delivery") == "delivered"
    repo, token = env.get("GITHUB_REPOSITORY", ""), env.get("GITHUB_TOKEN", "")
    if not repo or not token:
        return None
    try:
        values = fetch(repo, token, env.get("GITHUB_API_URL", "https://api.github.com"))
    except Exception:  # noqa: BLE001 - any read failure means unreadable
        return None
    return any(v.get("property_name") == "sylphx_delivery" and v.get("value") == "delivered" for v in values)


def main(env: dict[str, str] | None = None, root: pathlib.Path | None = None,
         fetch: Callable[[str, str, str], list[dict]] = _api_properties) -> int:
    env = dict(os.environ) if env is None else env
    state = delivered(env, fetch)
    if state is True:
        print("perf-gate: delivered customer repository (sylphx_delivery=delivered); skipped")
        return 0
    if state is None and env.get("GITHUB_ACTIONS") == "true":
        # Same rule as scripts/is-delivered.sh: an unreadable property is a skip.
        print("::warning::perf-gate: the sylphx_delivery property is unreadable; skipped")
        return 0
    findings = scan_dir(root or pathlib.Path.cwd())
    if not findings:
        print("perf-gate: no timing or dependency-audit gate before merge")
        return 0
    for f in findings:
        print(f"::error file={f.file},line={f.line}::{f.where}: {f.problem}")
    print(f"perf-gate: {len(findings)} non-deterministic gate(s) before merge. "
          "Judge timing after merge against a stored baseline. Dependency audits belong "
          "in a separate scheduled default-branch workflow for launched products; "
          "defer unlaunched audits to the launch gate. See docs/dependency-audits.md.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
