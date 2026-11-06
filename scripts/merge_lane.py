#!/usr/bin/env python3
"""The R8 merge-lane rule, shared by the fleet audit and required verdicts.

Decode YAML, propagate each caller event through local reusable workflows,
and evaluate selectors with known event, visibility, matrix and input
values. Unknown conditions never prove a job cannot run. No repository code is
executed. Requires the action's pinned PyYAML parser.
"""
from __future__ import annotations

import itertools
import json
import math
from pathlib import Path
import re
from typing import NamedTuple

import yaml

# Recorded upstream contract, including per-organization provisioning scope.
RUNNER_CONTRACT = json.loads((Path(__file__).resolve().parents[1] / 'policy/merge-runner-labels.json').read_text())
MERGE_LABELS = {label for labels in RUNNER_CONTRACT['orgs'].values() for label in labels}
SIZES = '|'.join(re.escape(label.removeprefix('sylphx-linux-').removesuffix('-merge')) for label in sorted(MERGE_LABELS))
POOL = re.compile(r'sylphx-linux-(' + SIZES + r')(-merge)?$')
TOKEN = re.compile(r"\s*(?:('(?:''|[^'])*')|(\&\&|\|\||==|!=|[!(),])|([A-Za-z_][\w-]*(?:\.(?:[\w-]+|\*))*)|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?))")
UNKNOWN = object()


class WorkflowLoader(yaml.BaseLoader):
    """GitHub's booleans without YAML 1.1 turning the `on` key into True."""


WorkflowLoader.add_implicit_resolver('tag:yaml.org,2002:bool', re.compile(r'^(?:true|false)$', re.I), list('tTfF'))
WorkflowLoader.add_constructor('tag:yaml.org,2002:bool', lambda loader, node: node.value.lower() == 'true')
JSON_NUMBER = re.compile(r'^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$')
WorkflowLoader.add_implicit_resolver('tag:yaml.org,2002:float', JSON_NUMBER, list('-0123456789'))
WorkflowLoader.add_constructor('tag:yaml.org,2002:float', lambda loader, node: float(node.value))


class JSONValue(NamedTuple):
    source: str


class Finding(NamedTuple):
    file: str
    line: int
    job: str
    problem: str


class RunnerEvaluation(NamedTuple):
    file: str
    line: int
    job: str
    event: str
    selector: object
    values: frozenset
    errors: tuple[str, ...]


def truth(value):
    return {False, True} if value is UNKNOWN else {value not in (None, False, '', 0)}


def equal_values(a, b):
    """GitHub loose equality: unlike scalar types are compared as numbers.

    Primary source: docs.github.com/en/actions/reference/workflows-and-actions/
    expressions#operators, read 2026-11-06T13:49Z. Invalid numeric strings are NaN.
    """
    if a is UNKNOWN or b is UNKNOWN or isinstance(a, JSONValue) or isinstance(b, JSONValue):
        return {False, True}
    if type(a) is type(b):
        return {(a.lower() if isinstance(a, str) else a) ==
                (b.lower() if isinstance(b, str) else b)}

    def number(value):
        if value is None or value == '':
            return 0
        if isinstance(value, (bool, int, float)):
            return float(value)
        if isinstance(value, str) and JSON_NUMBER.fullmatch(value.strip()):
            return float(value)
        return math.nan

    return {number(a) == number(b)}


class Expression:
    """Small abstract interpreter for the selector/condition expression grammar.

    Values are sets of possible scalars. Unsupported expressions remain unknown;
    they never exempt a job or hide a literal shared build-pool fallback.
    """
    def __init__(self, text, context, errors=None):
        self.tokens, self.index, self.context = [], 0, context
        self.errors = errors if errors is not None else []
        while text.strip():
            match = TOKEN.match(text)
            if not match:
                raise ValueError('unsupported expression construct: ' + text.strip()[:80])
            self.tokens.append(next(v for v in match.groups() if v is not None))
            text = text[match.end():]

    def take(self, token=None):
        value = self.tokens[self.index] if self.index < len(self.tokens) else None
        if token is not None and value != token:
            raise ValueError('unexpected expression token')
        self.index += 1
        return value

    def peek(self):
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def parse(self, level=0):
        operators = (('||',), ('&&',), ('==', '!='))
        if level == len(operators):
            return self.atom()
        left = self.parse(level + 1)
        while self.peek() in operators[level]:
            operator = self.take()
            right = self.parse(level + 1)
            result = set()
            for a, b in itertools.product(left, right):
                if operator in ('==', '!='):
                    equal = equal_values(a, b)
                    result.update(equal if operator == '==' else {not e for e in equal})
                elif operator == '&&':
                    result.update({False if a is UNKNOWN else a} if False in truth(a) else set())
                    result.update({b} if True in truth(a) else set())
                else:
                    result.update({a} if True in truth(a) else set())
                    result.update({b} if False in truth(a) else set())
            left = result
        return left

    def atom(self):
        token = self.take()
        if token == '!':
            return {not v for a in self.atom() for v in truth(a)}
        if token == '(':
            value = self.parse()
            self.take(')')
            return value
        if token is None:
            raise ValueError('incomplete expression')
        if token.startswith("'"):
            return {token[1:-1].replace("''", "'")}
        if self.peek() == '(':
            self.take('(')
            arguments = []
            if self.peek() != ')':
                arguments.append(self.parse())
                while self.peek() == ',':
                    self.take(',')
                    arguments.append(self.parse())
            self.take(')')
            if token == 'always' and not arguments:
                return {True}
            if token == 'format' and arguments:
                values = set()
                for row in itertools.product(*arguments):
                    if UNKNOWN in row or not isinstance(row[0], str):
                        values.add(UNKNOWN)
                    else:
                        try:
                            values.add(row[0].format(*row[1:]))
                        except (ValueError, IndexError, KeyError):
                            values.add(UNKNOWN)
                return values
            if token == 'fromJSON' and len(arguments) == 1:
                values = set()
                for value in arguments[0]:
                    try:
                        decoded = json.loads(value)
                        values.add(JSONValue(json.dumps(decoded, sort_keys=True)) if isinstance(decoded, (dict, list)) else decoded)
                    except (ValueError, TypeError):
                        values.add(UNKNOWN)
                return values
            if token in ('contains', 'startsWith', 'endsWith') and len(arguments) == 2:
                values = set()
                for a, b in itertools.product(*arguments):
                    if UNKNOWN in (a, b):
                        values.update((True, False))
                    elif isinstance(a, JSONValue) or isinstance(b, JSONValue):
                        decoded = json.loads(a.source) if isinstance(a, JSONValue) else a
                        if token == 'contains' and isinstance(decoded, list) and not isinstance(b, JSONValue):
                            comparisons = [equal_values(item, b) for item in decoded]
                            values.add(any(True in result for result in comparisons))
                        else:
                            self.errors.append('unsupported collection arguments: ' + token)
                            values.add(UNKNOWN)
                    else:
                        def string(value):
                            if value is None:
                                return ''
                            if isinstance(value, bool):
                                return 'true' if value else 'false'
                            return str(value).lower()
                        a, b = string(a), string(b)
                        values.add(b in a if token == 'contains' else
                                   a.startswith(b) if token == 'startsWith' else a.endswith(b))
                return values
            self.errors.append('unsupported function: ' + token)
            return {UNKNOWN}
        if token in ('true', 'false', 'null'):
            return {{'true': True, 'false': False, 'null': None}[token]}
        if re.fullmatch(r'-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?', token):
            return {float(token)}
        if token in self.context:
            return self.context[token]
        # Event-specific payloads do not exist on other triggering events.
        event = self.context.get('github.event_name', set())
        for payload, applicable in (
            ('pull_request', {'pull_request', 'pull_request_target'}),
            ('merge_group', {'merge_group'}), ('workflow_run', {'workflow_run'}),
            ('inputs', {'workflow_dispatch'}),
        ):
            if token.startswith('github.event.' + payload + '.') and event and not event & applicable:
                return {None}
        self.errors.append('unresolved context: ' + token)
        return {UNKNOWN}


def expression(text, context, *, condition=False, errors=None):
    if isinstance(text, (bool, int, float)):
        return {text}
    if not isinstance(text, str):
        return {UNKNOWN}
    text = text.strip()
    if text.startswith('${{') and text.endswith('}}'):
        text = text[3:-2].strip()
    elif not condition:
        return {text}
    try:
        parser = Expression(text, context, errors)
        result = parser.parse()
        if parser.peek() is not None:
            raise ValueError('trailing expression tokens')
        return result
    except (ValueError, TypeError) as error:
        if errors is not None:
            errors.append(str(error))
        # Retain all literal build labels on an unsupported selector. A comment
        # cannot reach here because YAML has already decoded the scalar.
        literals = set(re.findall(r'sylphx-linux-(?:' + SIZES + r')(?:-merge)?\b', text))
        return {UNKNOWN, *literals}


def runnable(job, context):
    condition = job.get('if')
    return condition is None or any(True in truth(v) for v in expression(condition, context, condition=True))


def events(document):
    value = document.get('on', {})
    return set(value) if isinstance(value, (dict, list)) else {value}


def contexts(job, context):
    matrix = (job.get('strategy') or {}).get('matrix') or {}
    if isinstance(matrix, str):
        resolved = []
        for value in expression(matrix, context):
            if isinstance(value, JSONValue):
                resolved.extend(contexts({'strategy': {'matrix': json.loads(value.source)}}, context))
        return resolved or [context]
    if not isinstance(matrix, dict):
        return [context]
    dimensions = {key: value for key, value in matrix.items()
                  if key not in ('include', 'exclude') and isinstance(value, list)}
    originals = [dict(zip(dimensions, values)) for values in itertools.product(*dimensions.values())] if dimensions else []
    rows = [dict(row) for row in originals if not any(
        isinstance(excluded, dict) and all(row.get(key) == value for key, value in excluded.items())
        for excluded in matrix.get('exclude', []))]
    for include in matrix.get('include', []):
        if not isinstance(include, dict):
            continue
        matched = False
        for row in rows:
            # Include may add/overwrite attributes introduced by an earlier
            # include, but cannot overwrite an original matrix dimension.
            if all(key not in dimensions or row.get(key) == value for key, value in include.items()):
                row.update(include)
                matched = True
        if not matched:
            rows.append(dict(include))
    result = []
    for row in rows:
        expanded = dict(context)
        for key, value in row.items():
            if isinstance(value, dict):
                for subkey, subvalue in value.items():
                    expanded[f'matrix.{key}.{subkey}'] = expression(subvalue, context)
            else:
                expanded[f'matrix.{key}'] = expression(value, context)
        result.append(expanded)
    return result or [context]


def runner_values(value, context, errors=None):
    if isinstance(value, dict):
        return runner_values(value.get('labels'), context, errors)
    if isinstance(value, list):
        return set().union(*(runner_values(item, context, errors) for item in value)) if value else set()
    if isinstance(value, str):
        values = set()
        for result in expression(value, context, errors=errors):
            if isinstance(result, JSONValue):
                values.update(runner_values(json.loads(result.source), context, errors))
            else:
                values.add(result)
        return values
    return set()


def runner_evaluations(workflows, *, private=None, repository_present=True):
    """Evaluate each triggering event, inheriting it through local calls.

    Unknown payload fields and caller-supplied inputs explore every reachable
    outcome. A reusable workflow inherits its caller's event, not workflow_call.
    """
    documents, marks = {}, {}
    for path, text in workflows.items():
        name = path.removeprefix('./').removeprefix('.github/workflows/')
        document = yaml.load(text, Loader=WorkflowLoader) or {}
        if not isinstance(document, dict):
            continue
        documents[name] = document
        node = yaml.compose(text, Loader=yaml.BaseLoader)
        fields = dict((k.value, v) for k, v in node.value) if isinstance(node, yaml.MappingNode) else {}
        jobs = fields.get('jobs')
        if isinstance(jobs, yaml.MappingNode):
            marks[name] = {k.value: v.start_mark.line + 1 for k, v in jobs.value}
    evaluations, seen = [], set()
    visibility = {'private' if private else 'public'} if private is not None else {'private', 'public'}
    context = {'github.event.repository.visibility': visibility if repository_present else {None},
               'github.event.repository.private': ({private} if private is not None else {True, False}) if repository_present else {None},
               'github.repository_visibility': visibility}

    def visit(name, inherited, ancestry):
        if name not in documents or name in ancestry:
            return
        document = documents[name]
        current = dict(inherited)
        inputs = ((document.get('on') or {}).get('workflow_call') or {}).get('inputs') or {} if ancestry and isinstance(document.get('on'), dict) else {}
        for key, properties in inputs.items():
            if isinstance(properties, dict):
                value = properties.get('default', {'boolean': False, 'number': 0}.get(properties.get('type'), ''))
                current.setdefault('inputs.' + key, expression(value, current))
        signature = (name, tuple(sorted((key, tuple(sorted((type(v).__name__, repr(v)) for v in values))) for key, values in current.items())))
        if signature in seen:
            return
        seen.add(signature)
        for job_id, job in (document.get('jobs') or {}).items():
            if not isinstance(job, dict):
                continue
            for expanded in contexts(job, current):
                if not runnable(job, expanded):
                    continue
                use = job.get('uses', '')
                if isinstance(use, str) and use.startswith('./.github/workflows/'):
                    called = {key: values for key, values in expanded.items() if not key.startswith('inputs.')}
                    for key, value in (job.get('with') or {}).items():
                        called['inputs.' + key] = expression(value, expanded)
                    visit(use.removeprefix('./.github/workflows/'), called, ancestry | {name})
                if 'runs-on' not in job:
                    continue
                errors = []
                values = runner_values(job['runs-on'], expanded, errors)
                evaluations.append(RunnerEvaluation(name, marks.get(name, {}).get(job_id, 1),
                    job_id, next(iter(current['github.event_name'])), job['runs-on'],
                    frozenset(values), tuple(sorted(set(errors)))))
    for name, document in documents.items():
        for event in sorted(events(document) - {'workflow_call'}):
            if event:
                visit(name, {**context, 'github.event_name': {event}}, set())
    return evaluations


def merge_lane_findings(workflows, *, private=None, org=None, notes=None, evaluations=None,
                        check_all_events=False):
    """Apply R8 to merge groups; workflow-lint also refuses unknown other events."""
    findings = {}
    supported = set(RUNNER_CONTRACT['orgs'].get(org, [])) if org is not None else MERGE_LABELS
    for evaluated in runner_evaluations(workflows, private=private) if evaluations is None else evaluations:
        name, line, job_id, event, selector, values, errors = evaluated
        if event != 'merge_group' and not check_all_events:
            continue
        if UNKNOWN in values or not values or any(not isinstance(v, str) or not v for v in values):
            detail = '; '.join(errors) or 'selector does not resolve to nonempty runner labels'
            findings[(name, job_id, event)] = Finding(name, line, job_id,
                f'{event} runs-on cannot be evaluated: {detail}; selector: {selector}')
            continue
        if event != 'merge_group':
            continue
        plain = sorted(value for value in values if isinstance(value, str) and
                       POOL.fullmatch(value) and not value.endswith('-merge'))
        served_plain = [value for value in plain if value + '-merge' in supported]
        unavailable_plain = [value for value in plain if value + '-merge' not in supported]
        if unavailable_plain and notes is not None:
            note = Finding(name, line, job_id,
                'no same-size merge twin is served for ' + ', '.join(unavailable_plain) +
                f' in {org}; keep the serving plain lane (capacity is a separate decision)')
            if note not in notes:
                notes.append(note)
        unavailable_merge = sorted(value for value in values if isinstance(value, str) and
            POOL.fullmatch(value) and value.endswith('-merge') and value not in supported)
        problem = ('merge_group job selects an unserved twin: ' + ', '.join(unavailable_merge)) if unavailable_merge else (
            'merge_group job selects the pull-request pool: ' + ', '.join(served_plain) if served_plain else '')
        selector = str(selector)
        classes = set(re.findall(r'sylphx-linux-(' + SIZES + r')(?![\w-])', selector))
        selected = {POOL.fullmatch(v).group(1) for v in values if isinstance(v, str) and POOL.fullmatch(v) and v.endswith('-merge')}
        if not problem and len(classes) == 1 and selected - classes:
            problem = 'merge_group selector changes runner size instead of selecting the same-size -merge twin'
        if problem:
            findings[(name, job_id, event)] = Finding(name, line, job_id, problem)
    return sorted(findings.values())


def merge_lane_misses(text, *, private=None, org=None, notes=None):
    """Compatibility entry point for one workflow; the fleet check passes all files."""
    return sorted({finding.job for finding in merge_lane_findings({'workflow.yml': text}, private=private, org=org, notes=notes)})
