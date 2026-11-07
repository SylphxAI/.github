"""Base review-policy declarations shared by review collection and ci-ok."""
from __future__ import annotations

import json
import re
import sys
import tomllib

POLICY_PATH = '.github/review-policy.toml'
MAX_POLICY_BYTES = 131072


def pattern_regex(pattern):
    result, index = '^', 0
    while index < len(pattern):
        char = pattern[index]
        if char == '*' and pattern[index:index + 2] == '**':
            index += 1
            if pattern[index + 1:index + 2] == '/':
                index += 1
                result += '(?:.*/)?'
            else:
                result += '.*'
        elif char == '*':
            result += '[^/]*'
        elif char == '?':
            result += '[^/]'
        else:
            # Only regexp syntax is escaped; both Python and Node consume this.
            result += '\\' + char if char in r'\^$.*+?()[]{}|' else char
        index += 1
    return result + '$'


def string_list(value, *, nonempty=False):
    return isinstance(value, list) and (not nonempty or bool(value)) and all(
        isinstance(item, str) and bool(item) and not re.search(r'[\x00\r\n]', item) for item in value)


def parse_policy(content):
    if content is None:
        policy = {'version': 'missing', **{key: {'paths': []} for key in ('tier0', 'generated', 'review')}}
    else:
        if not isinstance(content, str) or len(content.encode('utf-8')) > MAX_POLICY_BYTES:
            raise ValueError('Base review policy exceeds its byte limit.')
        try:
            policy = tomllib.loads(content)
        except tomllib.TOMLDecodeError:
            raise ValueError('Base review policy is invalid TOML.') from None
        if type(policy.get('version')) is not int or policy['version'] < 1:
            raise ValueError('Base review policy has no valid version.')
    for section in ('tier0', 'generated', 'review'):
        value = policy.get(section)
        if not isinstance(value, dict) or not string_list(value.get('paths')):
            raise ValueError(f'Base review policy has invalid {section} paths.')
    provenance = policy['generated'].get('provenance', [])
    if not isinstance(provenance, list):
        raise ValueError('Base review policy has invalid generated provenance.')
    for entry in provenance:
        if (not isinstance(entry, dict) or set(entry) != {'paths', 'jobs'} or
                not string_list(entry['paths'], nonempty=True) or
                not string_list(entry['jobs'], nonempty=True) or
                any(not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*', job) for job in entry['jobs'])):
            raise ValueError('Base review policy has invalid generated provenance paths/jobs.')
    policy['generated']['provenance'] = provenance
    policy['_patterns'] = {section: [pattern_regex(p) for p in policy[section]['paths']]
                           for section in ('tier0', 'generated', 'review')}
    return policy


def matches(policy, section, path):
    return any(re.search(pattern, path) for pattern in policy['_patterns'][section])


def provenance_jobs(policy, paths):
    """All generated declarations require proof, even when review also applies."""
    jobs, missing = set(), []
    for path in sorted(set(paths)):
        if not matches(policy, 'generated', path):
            continue
        entries = [entry for entry in policy['generated']['provenance']
                   if any(re.search(pattern_regex(pattern), path) for pattern in entry['paths'])]
        if not entries:
            missing.append(path)
        for entry in entries:
            jobs.update(entry['jobs'])
    return jobs, missing


if __name__ == '__main__':
    try:
        print(json.dumps(parse_policy(json.load(sys.stdin))))
    except (ValueError, TypeError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
