#!/usr/bin/env python3
"""Review-only entry point for repositories with an inline required verdict."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'contribution-policy'))
from contribution_policy import GitHub, PolicyError, merge_group_snapshot, review_findings, recheck_merge_group


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--require-merge-group-review', action='store_true', required=True)
    parser.parse_args(argv)
    if os.environ.get('GITHUB_EVENT_NAME') != 'merge_group':
        print('Review gate: no merge-group review admission on this event.')
        return 0
    try:
        token = os.environ.get('CONTRIBUTION_POLICY_TOKEN', '')
        if not token:
            raise PolicyError('read-only review metadata token is unavailable')
        event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
        api = GitHub(os.environ['GITHUB_REPOSITORY'], token)
        commits, snapshots, queued = merge_group_snapshot(api, event['merge_group'], require_head_binding=True)
        findings = [finding for pr, commits in snapshots for finding in review_findings(api, pr, commits)]
        findings.extend(recheck_merge_group(api, event['merge_group'], commits, snapshots, queued))
        for finding in findings:
            print('::error::Review gate: {rule} at {location}'.format(**finding))
        print(f'Review gate: {len(findings)} finding(s).')
        return int(bool(findings))
    except (PolicyError, KeyError, ValueError, OSError, TypeError) as error:
        message = str(error) if isinstance(error, PolicyError) else 'merge-group metadata could not be read'
        print('::error::Review gate: ' + message)
        return 2


if __name__ == '__main__':
    sys.exit(main())
