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

Standard library only: the runner may have no YAML module. The workflows have
already passed actionlint's parse, so an indentation scan of the block-style
keys GitHub workflows use is enough.

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

KEY = re.compile(r"""^(\s*)(?:-\s+)?(["']?)([A-Za-z0-9_.-]+)\2\s*:(?:\s+|$)(.*)$""")
ENV_KEY = re.compile(r"""^\s*(?:-\s+)?(["']?)PERF_ENFORCE\1\s*:\s*(.*)$""")
# Lines that label a step rather than run anything.
LABELS = ("name", "description")


class Finding(NamedTuple):
    file: str
    line: int
    where: str
    problem: str


def _strip_comment(line: str) -> str:
    if line.lstrip().startswith("#"):
        return ""
    return re.sub(r"\s+#.*$", "", line)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def triggers(lines: list[str]) -> list[str]:
    """The event names under the top-level `on:` key."""
    for i, raw in enumerate(lines):
        line = _strip_comment(raw)
        m = KEY.match(line)
        if not m or m.group(1) or m.group(3) != "on" or line.lstrip().startswith("-"):
            continue
        inline = m.group(4).strip()
        if inline.startswith("[") or inline.startswith("{"):
            body = inline.strip("[]{}")
            return [_unquote(p.split(":", 1)[0]) for p in body.split(",") if p.strip()]
        if inline:
            return [_unquote(inline)]
        found: list[str] = []
        child = None
        for raw_child in lines[i + 1:]:
            text = _strip_comment(raw_child)
            if not text.strip():
                continue
            ind = _indent(text)
            if ind == 0:
                break
            child = ind if child is None else child
            if ind != child:
                continue
            item = text.strip()
            if item.startswith("- "):
                found.append(_unquote(item[2:]))
            else:
                km = KEY.match(text)
                if km:
                    found.append(km.group(3))
        return found
    return []


def scan_workflow(file: str, text: str) -> list[Finding]:
    lines = text.splitlines()
    pre = [t for t in triggers(lines) if t in PRE_MERGE]
    if not pre:
        return []
    kind = "/".join(pre)
    out: list[Finding] = []
    section = ""
    job = ""
    job_indent: int | None = None
    for n, raw in enumerate(lines, 1):
        line = _strip_comment(raw)
        if not line.strip():
            continue
        ind = _indent(line)
        km = KEY.match(line)
        if ind == 0:
            section = km.group(3) if km else ""
            job, job_indent = "", None
            continue
        if section == "jobs" and km and not line.lstrip().startswith("-"):
            job_indent = ind if job_indent is None else job_indent
            if ind == job_indent:
                job = km.group(3)
                continue
        where = f"job {job}" if job else f"{section or 'workflow'}"
        em = ENV_KEY.match(line)
        if em:
            value = _unquote(em.group(2)).lower()
            if value not in ("0", "false"):
                out.append(Finding(file, n, where,
                                   f"PERF_ENFORCE is {json.dumps(_unquote(em.group(2)))}; "
                                   "a pre-merge workflow may only set it to '0'"))
            continue
        if km and km.group(3) in LABELS:
            continue
        for pattern, what in GATES:
            if pattern.search(line):
                out.append(Finding(file, n, where, f"runs {what} in a {kind} workflow"))
                break
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
