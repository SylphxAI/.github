"""Admit only canonical concurrency declarations and named literal exceptions.

Do not infer event-payload absence or symbolically sample payloads. A new form
requires an explicit, tested contract here before any workflow can use it.
"""
from __future__ import annotations

import re

import yaml

from merge_lane import events

PR_EVENTS = {"pull_request", "pull_request_target"}
PR_GUARD = "github.event_name == 'pull_request' || github.event_name == 'pull_request_target'"
GROUP = "${{ (" + PR_GUARD + ") && format('{0}-pr-{1}', github.workflow, github.event.pull_request.number) || format('{0}-run-{1}', github.workflow, github.run_id) }}"
CANCEL = '${{ ' + PR_GUARD + ' }}'
SINGLE_GROUP = "${{ github.event_name == 'pull_request' && format('{0}-pr-{1}', github.workflow, github.event.pull_request.number) || format('{0}-run-{1}', github.workflow, github.run_id) }}"
SINGLE_CANCEL = "${{ github.event_name == 'pull_request' }}"
RUN_GROUP = '${{ github.workflow }}-run-${{ github.run_id }}'
BASE_RETARGET = "pr-metadata-${{ github.event_name == 'pull_request' && github.event.changes.base && format('base-{0}', github.run_id) || github.event.pull_request.number && format('pr-{0}', github.event.pull_request.number) || format('{0}-run-{1}', github.workflow, github.run_id) }}"
CODE_PR_ASSOCIATION = "${{ github.workflow }}-${{ github.event.inputs.junit-producer-run && format('consumer-run-{0}', github.run_id) || (github.event.pull_request.number || github.event.inputs.code-pr) && format('pr-{0}', github.event.pull_request.number || github.event.inputs.code-pr) || format('run-{0}', github.run_id) }}"
EXCEPTION = re.compile(r"^#[ \t]*pr-concurrency:[ \t]*exception[ \t]+([\w-]+)[ \t]+[—:][ \t]*(.*)$", re.M)
MARKER = re.compile(r"^#\s*pr-concurrency:\s*(?:exception|exempt)\b", re.M)
ERROR = 'non-canonical concurrency expression; use the canonical form or add a named exception'


def matches(concurrency, group, cancel):
    return (set(concurrency) == {'group', 'cancel-in-progress'}
            and concurrency['group'] == group
            and concurrency['cancel-in-progress'] == cancel)


def named_exception(name, decoded, triggered, concurrency):
    """Each exception is a literal form with a finite trigger/input contract."""
    if name == 'complete-evidence':
        return matches(concurrency, RUN_GROUP, 'false')
    if name == 'reusable-regenerate':
        return (decoded.get('name') == 'Regenerate' and 'workflow_call' in triggered
                and matches(concurrency, 'regenerate-' + GROUP, CANCEL))
    # These metadata forms are not reusable: inherited caller payloads/inputs
    # must never enter their special PR-association branches.
    if not triggered <= {'pull_request', 'push', 'merge_group', 'workflow_dispatch'}:
        return False
    if name == 'base-retarget':
        return (decoded.get('name') == 'PR metadata'
                and matches(concurrency, BASE_RETARGET, SINGLE_CANCEL))
    if name == 'code-pr-association':
        on = decoded.get('on', {})
        dispatch = on.get('workflow_dispatch', {}) if isinstance(on, dict) else {}
        inputs = dispatch.get('inputs', {}) if isinstance(dispatch, dict) else {}
        return (decoded.get('name') == 'Project control'
                and all(isinstance(inputs.get(key), dict) and inputs[key].get('type') == 'string'
                        for key in ('code-pr', 'junit-producer-run'))
                and matches(concurrency, CODE_PR_ASSOCIATION, SINGLE_CANCEL))
    return False


def problems(text):
    """Return (line, problem) pairs; workflow_call-only definitions inherit callers."""
    document = yaml.compose(text, Loader=yaml.BaseLoader)
    fields = {k.value: v for k, v in document.value} if isinstance(document, yaml.MappingNode) else {}
    decoded = yaml.load(text, Loader=yaml.BaseLoader) or {}
    triggered = events(decoded)
    if not triggered & PR_EVENTS:
        return []
    header = text[:fields.get('jobs', document).start_mark.index] if document else ''
    declaration = EXCEPTION.search(header)
    line = fields.get('concurrency', fields.get('on', document)).start_mark.line + 1
    concurrency = decoded.get('concurrency')
    if MARKER.search(header) and (not declaration or not declaration[2].strip(' \t:-—')):
        return [(line, ERROR + '; named exception must include a specific reason')]
    if not isinstance(concurrency, dict):
        return [(line, ERROR)]
    if declaration:
        if named_exception(declaration[1], decoded, triggered, concurrency):
            return []
        return [(line, ERROR)]
    if matches(concurrency, GROUP, CANCEL):
        return []
    # This existing canonical variant is safe only when a target/caller event
    # cannot enter it; mixed reusable definitions require the two-PR form.
    if not triggered & {'pull_request_target', 'workflow_call'} and matches(concurrency, SINGLE_GROUP, SINGLE_CANCEL):
        return []
    return [(line, ERROR)]
