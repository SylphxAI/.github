#!/usr/bin/env python3
"""Compile immutable workflows into the runner-independent expected-check contract.

Only enrolled repositories (those with the declaration) are checked for drift.
Run this file with --write to enroll or regenerate; runtimes consume JSON only.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import re
import sys
import subprocess
import urllib.request

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / "scripts"))
from merge_lane import WorkflowLoader, contexts, events, expression, runnable, truth
from github_paths import pattern_regex

DECLARATION = ".github/required-workflow-checks.json"
EVENTS = {"pull_request", "merge_group"}
VERIFIER_NAME = "expected-checks / verify"
VERIFIER_WORKFLOW = "expected-checks.yml"
ADVISORY = re.compile(r"^#\s*optimistic-merge:\s*advisory\b([^\n]+)", re.I | re.M)
REMOTE = re.compile(r"([\w.-]+)/([\w.-]+)/(.github/workflows/[^@]+\.ya?ml)@([a-fA-F0-9]{40})$")


class DeclarationError(ValueError):
    pass


def needs(job):
    value = job.get("needs", [])
    return [value] if isinstance(value, str) else value


def closure(jobs, roots):
    result, pending = set(), list(roots)
    while pending:
        key = pending.pop()
        if key not in jobs or key in result:
            continue
        result.add(key)
        pending.extend(needs(jobs[key]))
    return result


def roots(jobs, context):
    return [key for key, job in jobs.items() if runnable(job, context) and
            (key == "ci-ok" or job.get("name") == "ci-ok" or any(
                re.fullmatch(r"(?:SylphxAI/\.github/|\./)\.github/actions/ci-ok(?:-polling)?(?:@[a-fA-F0-9]{40})?",
                             step.get("uses", "")) and runnable(step, context)
                for step in job.get("steps", [])))]


def healthy_unconditional(job, context):
    # Status checks describe failures/cancellation, not a source-selection rule.
    # The expected healthy run must not omit a conditionally selected child.
    value = job.get("if", True)
    if isinstance(value, str):
        value = re.sub(r"\b(?:always|success)\(\)", "true", value)
        value = re.sub(r"\b(?:cancelled|failure)\(\)", "false", value)
    return {v for result in expression(value, context, condition=True) for v in truth(result)} == {True}


def literal_name(key, job, location):
    name = job.get("name", key)
    if (not isinstance(name, str) or not name.strip() or "${{" in name or
            "\n" in name or job.get("strategy") is not None):
        raise DeclarationError(f"{location}/{key}: required check needs a literal, non-matrix name")
    return name


def fetch_reusable(target):
    match = REMOTE.fullmatch(target)
    if not match:
        raise DeclarationError(f"reusable workflow must use a full immutable SHA: {target}")
    owner, repo, path, sha = match.groups()
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={sha}"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "workflow-lint"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
            return base64.b64decode(json.load(response)["content"]).decode()
    except (OSError, KeyError, ValueError) as error:
        raise DeclarationError(f"cannot resolve immutable reusable workflow {target}: {error}") from error


def compile_declaration(workflows, resolve_remote=fetch_reusable, require_verifier=True):
    """Resolve names/events/filters at lint time, never on the verdict runtime."""
    sources = {file.removeprefix("./").removeprefix(".github/workflows/"): text
               for file, text in workflows.items()}
    decoded = {file: yaml.load(text, Loader=WorkflowLoader) or {} for file, text in sources.items()}
    # This check is emitted by a SHA-pinned organization-required workflow,
    # not by replaceable YAML in the evaluated repository.
    records = [{"name": VERIFIER_NAME, "workflow": VERIFIER_WORKFLOW,
                "events": sorted(EVENTS), "paths": None, "paths_ignore": None}] if require_verifier else []
    remote_cache = {}

    def boundary(job):
        if 'always()' not in str(job.get('if', '')) or job.get('continue-on-error', False) is not False:
            return False
        for step in job.get('steps', []):
            if not re.fullmatch(r'SylphxAI/\.github/\.github/actions/(?:needs-pass|ci-ok)@[a-fA-F0-9]{40}', step.get('uses', '')):
                continue
            inputs = step.get('with', {})
            if ('if' not in step and step.get('continue-on-error', False) is False and
                    inputs.get('needs', '').replace(' ', '') == '${{toJSON(needs)}}' and not inputs.get('advisory')):
                return True
        return False

    def required_names(document, context, location, candidates=None, ancestry=(), origin=None):
        jobs = document.get("jobs", {})
        active = {key for key, job in jobs.items()
                  if any(runnable(job, expanded) for expanded in contexts(job, context))}
        gate_roots = roots(jobs, context)
        if candidates is None:
            # A reusable call produces callee checks, not a check for the call
            # job itself. Its strict aggregate is the completion boundary.
            candidates = (active - closure(jobs, gate_roots)) | set(gate_roots) if gate_roots else active
        selected = {key for key in candidates if healthy_unconditional(jobs[key], context)}
        for key in candidates - selected:
            aggregates = [other for other in selected
                          if boundary(jobs[other])
                          and key in needs(jobs[other])]
            if not aggregates:
                raise DeclarationError(f"{location}/{key}: conditional check needs an always-running required aggregate")
        result = []
        # A static always-running completion boundary represents all matrix
        # legs; no fabricated per-leg name belongs in the runtime contract.
        selected -= {key for key in selected if jobs[key].get("strategy") is not None and any(
            other != key and boundary(jobs[other]) and
            key in needs(jobs[other]) for other in selected)}
        for key in sorted(selected):
            job = jobs[key]
            name = literal_name(key, job, location)
            target = job.get("uses")
            if not target:
                result.append(name)
                continue
            if target in ancestry:
                raise DeclarationError(f"recursive reusable workflow: {target}")
            if target.startswith('./.github/workflows/') and origin is not None:
                repository, ref = origin
                target = f'{repository}/{target[2:]}@{ref}'
            nested_origin = origin
            if target.startswith("./.github/workflows/"):
                file = target.removeprefix("./.github/workflows/")
                if file not in decoded:
                    raise DeclarationError(f"missing local reusable workflow: {target}")
                called = decoded[file]
            else:
                match = REMOTE.fullmatch(target)
                if not match:
                    raise DeclarationError(f"reusable workflow must use a full immutable SHA: {target}")
                nested_origin = (f'{match[1]}/{match[2]}', match[4])
                if target not in remote_cache:
                    remote_cache[target] = yaml.load(resolve_remote(target), Loader=WorkflowLoader) or {}
                called = remote_cache[target]
            inherited = {k: v for k, v in context.items() if not k.startswith("inputs.")}
            called_events = called.get("on") or {}
            definitions = ((called_events.get("workflow_call") or {}).get("inputs", {})
                           if isinstance(called_events, dict) else {})
            for input_name, definition in definitions.items():
                value = (job.get("with") or {}).get(input_name, definition.get("default", False if definition.get("type") == "boolean" else ""))
                inherited["inputs." + input_name] = expression(value, context)
            result.extend(name + " / " + child for child in required_names(called, inherited, target, ancestry=(*ancestry, target), origin=nested_origin))
        return result

    emitted = {(VERIFIER_NAME, event): 'organization-required workflow' for event in EVENTS} if require_verifier else {}
    for file, document in sorted(decoded.items()):
        header = re.split(r"^jobs:", sources[file], maxsplit=1, flags=re.M)[0]
        advisory = ADVISORY.search(header)
        if advisory and advisory.group(1).strip(" \t:-—"):
            if file == VERIFIER_WORKFLOW and require_verifier:
                raise DeclarationError("expected-checks verifier cannot be advisory")
            continue
        for event in sorted(events(document) & EVENTS):
            context = {"github.event_name": {event}, "github.event.repository.private": {True, False}}
            jobs = document.get("jobs", {})
            active = {key for key, job in jobs.items() if any(runnable(job, c) for c in contexts(job, context))}
            independent = active - closure(jobs, roots(jobs, context))
            config = document.get("on", {})
            trigger = config.get(event) or {} if isinstance(config, dict) else {}
            paths, ignored = trigger.get("paths"), trigger.get("paths-ignore")
            for label, patterns in (("paths", paths), ("paths-ignore", ignored)):
                if patterns is not None and (not isinstance(patterns, list) or not patterns or
                        any(not isinstance(p, str) or not p or "${{" in p for p in patterns)):
                    raise DeclarationError(f"{file}: {label} must contain literal GitHub path patterns")
                for pattern in patterns or []:
                    try:
                        pattern_regex(pattern.removeprefix('!'))
                    except ValueError as error:
                        raise DeclarationError(f'{file}: {error}') from error
            if paths is not None and ignored is not None:
                raise DeclarationError(f"{file}: paths and paths-ignore cannot be combined")
            if event != 'pull_request' and (paths is not None or ignored is not None):
                raise DeclarationError(f'{file}: {event} does not support path filters')
            if paths is not None and not any(not pattern.startswith('!') for pattern in paths):
                raise DeclarationError(f'{file}: paths requires a nonnegative pattern')
            # v2 paths:null is unconditionally relevant. Normalize GitHub's
            # ignore-only trigger to include-all plus its exclusion filter.
            if ignored is not None:
                paths = ["**"]
            for name in required_names(document, context, file, independent):
                identity = name, event
                if identity in emitted:
                    raise DeclarationError(f'ambiguous check name {name!r} on {event}: {emitted[identity]} and {file}')
                emitted[identity] = file
                record = {"name": name, "workflow": file, "events": [event],
                          "paths": paths, "paths_ignore": ignored}
                existing = next((r for r in records if all(r[k] == record[k] for k in ("name", "workflow", "paths", "paths_ignore"))), None)
                if existing:
                    existing["events"] = sorted(set(existing["events"]) | {event})
                else:
                    records.append(record)
    seen = {}
    for record in records:
        for event in record["events"]:
            key = (record["name"], event)
            if key in seen:
                raise DeclarationError(f"ambiguous check name {record['name']!r} on {event}: {seen[key]} and {record['workflow']}")
            seen[key] = record["workflow"]
    return {"version": 2, "checks": sorted(records, key=lambda r: (r["workflow"], r["name"], r["events"]))}


def check_declaration(root, workflows):
    path = root / DECLARATION
    if not path.exists():
        return []
    try:
        expected = compile_declaration(workflows)
        if path.read_text() == render(expected):
            return []
        return ["expected-check declaration drift; regenerate with expected_checks.py --write"]
    except (DeclarationError, ValueError, TypeError, KeyError) as error:
        return [str(error)]


def render(declaration):
    return json.dumps(declaration, indent=2, ensure_ascii=False) + "\n"


def verify_checkout(root, expected_sha):
    if not re.fullmatch(r"[a-fA-F0-9]{40}", expected_sha or ""):
        raise DeclarationError("missing exact evaluated PR head / merge-group SHA")
    actual = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    if actual.lower() != expected_sha.lower():
        raise DeclarationError(f"checkout HEAD {actual} differs from evaluated SHA {expected_sha}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="write the resolved v2 declaration")
    parser.add_argument("--verify", action="store_true", help="require canonical declaration and exact evaluated checkout")
    parser.add_argument("--expected-sha", default=os.environ.get("EXPECTED_SHA", ""))
    parser.add_argument("--root", type=pathlib.Path, default=pathlib.Path.cwd())
    args = parser.parse_args()
    try:
        if args.verify:
            if args.write:
                parser.error('--verify cannot write')
            verify_checkout(args.root, args.expected_sha)

            def committed(path):
                return subprocess.check_output(['git', '-C', str(args.root), 'show',
                    f'{args.expected_sha}:{path}'], text=True, stderr=subprocess.DEVNULL)

            paths = subprocess.check_output(['git', '-C', str(args.root), 'ls-tree', '-r',
                '--name-only', args.expected_sha, '--', '.github/workflows'], text=True).splitlines()
            workflows = {pathlib.PurePosixPath(path).name: committed(path)
                for path in paths if pathlib.PurePosixPath(path).parent == pathlib.PurePosixPath('.github/workflows')
                and pathlib.PurePosixPath(path).suffix in {'.yml', '.yaml'}}
        else:
            folder = args.root / '.github/workflows'
            workflows = {p.name: p.read_text() for p in sorted(folder.iterdir()) if p.suffix in ('.yml', '.yaml')}
        declaration = compile_declaration(workflows)
    except (DeclarationError, yaml.YAMLError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, str(error) + "\n")
    output = render(declaration)
    if args.verify:
        try:
            if committed(DECLARATION) != output:
                raise DeclarationError("missing or drifted expected-check declaration; regenerate with --write")
        except (DeclarationError, OSError, subprocess.CalledProcessError) as error:
            parser.exit(1, str(error) + "\n")
        print(f"Verified {DECLARATION} at {args.expected_sha}")
    elif args.write:
        (args.root / DECLARATION).write_text(output)
    else:
        print(output, end="")


if __name__ == "__main__":
    main()
