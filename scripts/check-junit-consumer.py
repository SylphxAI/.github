#!/usr/bin/env python3
"""Real cross-run consumer proof. No metadata, OIDC or store transport mocks."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

import yaml

ROOT = Path(__file__).resolve().parents[1]


def api(path):
    return json.loads(subprocess.check_output(['gh', 'api', path], text=True, timeout=60))


def main():
    repo = os.environ['GITHUB_REPOSITORY']
    run = os.environ['PRODUCER_RUN']
    if not re.fullmatch(r'[0-9]{1,20}', run) or run == os.environ['GITHUB_RUN_ID']:
        raise ValueError('Consumer requires a different numeric producer run id')
    producer = api(f'repos/{repo}/actions/runs/{run}')
    if (producer['repository']['full_name'] != repo
            or producer['path'] != '.github/workflows/project-control.yml'
            or producer['status'] != 'completed' or producer['conclusion'] != 'success'):
        raise ValueError('Expected a completed successful Project control producer run')

    with tempfile.TemporaryDirectory(dir=os.environ['RUNNER_TEMP'], prefix='junit-consumer-') as tmp:
        reports = Path(tmp) / 'reports'
        subprocess.run([sys.executable, str(ROOT / '.github/actions/run-store/get-junit.py'),
                        repo, run, str(reports)], check=True, timeout=480)
        expected_lanes = {f'junit-consumer-{shard}': f'JUnit fixture ({shard})' for shard in (1, 701)}
        lanes = json.loads((reports / '.run-store-lanes.json').read_text())
        if lanes != expected_lanes:
            raise AssertionError(f'Producer discovery/lane attribution mismatch: {lanes!r}')
        for name in expected_lanes:
            if not (reports / name / 'nested/junit.xml').is_file():
                raise AssertionError(f'Missing nested shard report: {name}')

        # Execute the actual shared red-main parsers, not a test reimplementation.
        env = yaml.safe_load((ROOT / '.github/workflows/red-main.yml').read_text())['jobs']['red-main']['env']
        units = Path(tmp) / 'units.tsv'
        parser = Path(tmp) / 'junit.py'
        parser.write_text(env['JUNIT_PY'])
        subprocess.run([sys.executable, str(parser), str(reports), str(units),
                        ','.join(expected_lanes.values())], check=True, timeout=30)
        expected_failed = {(f'suite::broken-{shard}', f'JUnit fixture ({shard})') for shard in (1, 701)}
        expected_rows = {f'test\t{name}\t{lane}' for name, lane in expected_failed}
        if set(units.read_text().splitlines()) != expected_rows:
            raise AssertionError('Failed-unit parser lost exact test or matrix job identity')
        window = {'__name__': 'consumer'}
        exec(compile(env['WINDOW_PY'], 'window.py', 'exec'), window)
        failed, passed, covered = window['read_junit'](str(reports))
        expected_passed = {(f'suite::passing-{shard}', f'JUnit fixture ({shard})') for shard in (1, 701)}
        if (failed, passed, covered) != (expected_failed, expected_passed, set(expected_lanes.values())):
            raise AssertionError('Regression-window parser lost passing/failing shard identity')

    evidence = (f'Consumer source `{os.environ["CONSUMER_SOURCE"]}`; producer '
                f'[{run}](https://github.com/{repo}/actions/runs/{run}) at `{producer["head_sha"]}`. '
                'Authenticated discovery and OIDC run-store reads retrieved shards 1 and 701; '
                'both red-main parsers preserved nested reports and exact passing/failing test/job identity.')
    print(evidence)
    with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
        summary.write(evidence + '\n')


if __name__ == '__main__':
    main()
