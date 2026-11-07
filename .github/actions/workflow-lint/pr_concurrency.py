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
STATUS_CHECK = re.compile(r'\b(?:always|success|failure|cancelled)\s*\(', re.I)


def matches(concurrency, group, cancel):
    return (set(concurrency) == {'group', 'cancel-in-progress'}
            and concurrency['group'] == group
            and concurrency['cancel-in-progress'] == cancel)


def named_exception(name, decoded, triggered, concurrency, workflows=None):
    """Each exception is a literal form with a finite trigger/input contract."""
    if name == 'complete-evidence':
        return matches(concurrency, RUN_GROUP, 'false')
    if name == 'independent-verdict':
        return ('workflow_call' not in triggered
                and independent_verdict(decoded, concurrency, workflows))
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


def unlocked_local_callee(uses, workflows):
    """Admit only resolved one-level local calls without another lock owner."""
    if not isinstance(uses, str) or not isinstance(workflows, dict):
        return False
    path = re.fullmatch(r'\./\.github/workflows/([^/\\]+\.ya?ml)', uses)
    text = workflows.get(path[1]) if path else None
    if not isinstance(text, str):
        return False
    try:
        callee = yaml.load(text, Loader=yaml.BaseLoader)
        if (not isinstance(callee, dict) or 'workflow_call' not in events(callee)
                or 'concurrency' in callee):
            return False
    except (yaml.YAMLError, TypeError):
        return False
    jobs = callee.get('jobs')
    return (isinstance(jobs, dict) and bool(jobs)
            and all(isinstance(job, dict) and 'concurrency' not in job and 'uses' not in job
                    for job in jobs.values()))


def independent_verdict(decoded, concurrency, workflows=None):
    """Keep the unconditional verdict outside every cancellable work lock.

    A surviving always() verdict must not hold the admission lock needed by
    its replacement's cleanup. Literal job namespaces also avoid collisions
    between sibling jobs. Every reusable call must resolve to a local callee
    with no workflow/job locks or further calls, since github.workflow is inherited.
    This validates lock coverage, not the verdict's result-propagation code.
    """
    if not matches(concurrency, RUN_GROUP, 'false'):
        return False
    jobs = decoded.get('jobs')
    if not isinstance(jobs, dict) or len(jobs) < 2:
        return False
    verdicts = []
    work = []
    for name, job in jobs.items():
        if not isinstance(name, str) or not isinstance(job, dict):
            return False
        if 'uses' in job and not unlocked_local_callee(job['uses'], workflows):
            return False
        if job.get('if') in ('always()', '${{ always() }}'):
            if 'concurrency' in job:
                return False
            verdicts.append(job)
        else:
            # Explicit status functions replace GitHub's implicit success()
            # guard. Do not infer cancellability from arbitrary boolean forms:
            # a surviving work job could retain the replacement cleanup lock.
            condition = job.get('if', '')
            if not isinstance(condition, str) or STATUS_CHECK.search(condition):
                return False
            lock = job.get('concurrency')
            if not isinstance(lock, dict) or not matches(lock, name + '-' + GROUP, CANCEL):
                return False
            work.append(name)
    # GitHub concurrency groups are case-insensitive even when job IDs differ.
    if len(verdicts) != 1 or not work or len({name.casefold() for name in work}) != len(work):
        return False
    needs = verdicts[0].get('needs')
    if isinstance(needs, str):
        needs = [needs]
    return (isinstance(needs, list) and all(isinstance(name, str) for name in needs)
            and len(needs) == len(work)
            and set(needs) == set(work))


def problems(text, workflows=None):
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
        if named_exception(declaration[1], decoded, triggered, concurrency, workflows):
            return []
        return [(line, ERROR)]
    if matches(concurrency, GROUP, CANCEL):
        return []
    # This existing canonical variant is safe only when a target/caller event
    # cannot enter it; mixed reusable definitions require the two-PR form.
    if not triggered & {'pull_request_target', 'workflow_call'} and matches(concurrency, SINGLE_GROUP, SINGLE_CANCEL):
        return []
    return [(line, ERROR)]
