#!/usr/bin/env python3
"""Fail-closed runner-selector contract for this repository's reusable library.

Only a fixed ubuntu-latest branch behind an affirmative caller visibility check
may select hosted capacity. Other selectors must prove a self-hosted label by
construction. This intentionally accepts a small grammar, not sampled inputs.
Composite actions inherit their caller's runner and cannot declare runs-on.
"""
from pathlib import Path
import re
import sys

import yaml

from merge_lane import Expression, WorkflowLoader, events

SELF_HOSTED = re.compile(r'sylphx-linux-[a-z0-9-]+', re.I)
INPUT = re.compile(r'inputs\.[\w-]+')


def tokens(text):
    text = text.strip()
    if not (text.startswith('${{') and text.endswith('}}')):
        return None
    try:
        parser = Expression(text[3:-2], {})
        parser.parse()
        return tuple(parser.tokens) if parser.peek() is None else None
    except (ValueError, TypeError, IndexError):
        return None


def literal_self_hosted(value):
    return isinstance(value, str) and bool(SELF_HOSTED.fullmatch(value))


def self_hosted_expression(parts):
    # A fixed prefix before every interpolation cannot produce a hosted label.
    if (len(parts) >= 4 and parts[:2] == ('format', '(')
            and parts[2].startswith("'sylphx-linux-") and parts[3] in (',', ')')):
        depth = 0
        for index, token in enumerate(parts[1:], 1):
            depth += (token == '(') - (token == ')')
            if depth == 0:
                return index == len(parts) - 1
        return False
    if len(parts) == 1:
        return parts[0].startswith("'") and literal_self_hosted(parts[0][1:-1])
    # Free-form inputs are constrained before they can become a runner label.
    return (len(parts) == 10 and parts[:2] == ('startsWith', '(')
            and bool(INPUT.fullmatch(parts[2]))
            and parts[3:7] == (',', "'sylphx-linux-'", ')', '&&')
            and parts[7] == parts[2] and parts[8] == '||'
            and literal_self_hosted(parts[9][1:-1]) and parts[9].startswith("'"))


def safe_selector(selector):
    if literal_self_hosted(selector):
        return True
    if isinstance(selector, list):
        # GitHub-hosted capacity cannot satisfy the literal self-hosted label.
        return 'self-hosted' in selector and all(isinstance(v, str) and '${{' not in v for v in selector)
    if not isinstance(selector, str):
        return False
    parts = tokens(selector)
    if not parts:
        return False
    if self_hosted_expression(parts):
        return True
    prefix = ('github.event.repository.visibility', '==', "'public'", '&&')
    if parts[:4] != prefix:
        return False
    parts = parts[4:]
    # The optional input chooses a label only after visibility permits hosted.
    if len(parts) >= 4 and INPUT.fullmatch(parts[0]):
        if parts[1:4] != ('==', "'ubuntu-latest'", '&&'):
            return False
        parts = parts[4:]
    return (parts[:2] == ("'ubuntu-latest'", '||')
            and self_hosted_expression(parts[2:]))


def findings(workflows, actions):
    errors = []
    for path, text in workflows.items():
        document = yaml.load(text, Loader=WorkflowLoader) or {}
        if 'workflow_call' not in events(document):
            continue
        for name, job in (document.get('jobs') or {}).items():
            if 'runs-on' in job and not safe_selector(job['runs-on']):
                errors.append(f'{path}: jobs.{name}.runs-on is not proven spend-safe')
            if 'uses' in job:
                # Do not hide runner selection behind an uninspected workflow.
                use = job['uses']
                target = (use.removeprefix('./') if use.startswith('./.github/workflows/') else
                          use.removeprefix('SylphxAI/.github/').removesuffix('@main')
                          if use.startswith('SylphxAI/.github/.github/workflows/') and use.endswith('@main') else '')
                candidates = (target, target.removeprefix('.github/workflows/'))
                called = next((workflows[k] for k in candidates if k in workflows), None)
                if called is None or 'workflow_call' not in events(yaml.load(called, Loader=WorkflowLoader) or {}):
                    errors.append(f'{path}: jobs.{name}.uses does not resolve to the inspected library')

    def check_action(path, value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key == 'runs-on':
                    errors.append(f'{path}: actions inherit the caller runner; runs-on is forbidden')
                check_action(path, child)
        elif isinstance(value, list):
            for child in value:
                check_action(path, child)

    for path, text in actions.items():
        check_action(path, yaml.load(text, Loader=WorkflowLoader) or {})
    return errors


def library_findings(root):
    return findings({str(p.relative_to(root)): p.read_text()
                     for p in (root / '.github/workflows').glob('*.y*ml')},
                    {str(p.relative_to(root)): p.read_text()
                     for p in (root / '.github/actions').rglob('action.y*ml')})


if __name__ == '__main__':
    result = library_findings(Path(__file__).resolve().parents[1])
    print('\n'.join(result) if result else 'Reusable runner spend contract: PASS')
    sys.exit(bool(result))
