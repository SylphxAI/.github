#!/usr/bin/env python3
"""Render top-level-identity adapters from the tenant-compatible workflow programs.

The old reusable workflows stay unchanged for external tenants. Their behavior
is the source of truth; adapters change only identity/credential plumbing.
Run with --check in tests, or without it after changing a shared workflow.
"""
import copy
from pathlib import Path
import sys
import yaml

ROOT = Path(__file__).resolve().parents[1]


class Dumper(yaml.SafeDumper):
    pass


def string(dumper, value):
    return dumper.represent_scalar('tag:yaml.org,2002:str', value, style='|' if '\n' in value else None)


Dumper.add_representer(str, string)


def load(name):
    return yaml.safe_load((ROOT / '.github/workflows' / (name + '.yml')).read_text())


def inputs(workflow, omit=()):
    result = copy.deepcopy(workflow.get('on', workflow.get(True))['workflow_call']['inputs'])
    for key in list(result):
        if key in omit:
            del result[key]
            continue
        result[key].pop('type', None)
        if 'default' in result[key]:
            value = result[key]['default']
            result[key]['default'] = str(value).lower() if isinstance(value, bool) else str(value)
    return result


def inp(description, default=None, required=False):
    result = dict(description=description, required=required)
    if default is not None:
        result['default'] = default
    return result


def run_step(name, run, **kwargs):
    return dict(name=name, shell='bash', run=run, **kwargs)


def condition(step, mode):
    old = str(step.get('if', '')).removeprefix('${{').removesuffix('}}').strip()
    step['if'] = '${{ ' + mode + (' && (' + old + ')' if old else '') + ' }}'
    step.pop('timeout-minutes', None)  # Composite schema has only the caller job's timeout.
    if 'run' in step:
        step['shell'] = 'bash'
    return step


def broker_env(lifecycle=False):
    return dict(BROKER_POLICY='${{ inputs.broker-trust-policy }}', BROKER_CI_NAME='${{ inputs.broker-ci-name }}',
                REPO='${{ github.repository }}', API_URL='${{ inputs.api-url }}', SOFT_FAIL='false', READ_ONLY='false',
                PERMISSIONS='{"issues":"write","metadata":"read"}' if lifecycle else '{"contents":"write","issues":"write","pull_requests":"write","workflows":"write","metadata":"read"}',
                APP_SLUG='${{ inputs.broker-app-slug }}', EXPECTED_ISSUER='${{ inputs.expected-issuer }}',
                EXPECTED_CONNECTION='${{ inputs.expected-connection }}')


def image():
    source = load('image-lane')
    ins = inputs(source, ('lane-ref', 'builder-app-id', 'builder-repositories', 'runner-class', 'publisher-label', 'artifact-retention-days'))
    ins.update({
        'phase': inp('all runs preflight before publish; split jobs use preflight then publish with needs. Each signed top-level job needs its own exact publisher grant.', 'all'),
        'broker-ci-name': inp('Exact Access CI declaration for this top-level source-read job.', ''),
        'broker-trust-policies': inp('Exact JSON owner/name -> TrustPolicy map for source-repositories.', '{}'),
        'source-repositories': inp('Optional comma/newline separated full owner/name private source repositories.', ''),
        'api-url': inp('Ordinary platform API endpoint.', 'https://api.sylphx.com'),
        'expected-issuer': inp('Optional exact Broker issuer pin.', ''),
        'expected-connection': inp('Optional exact Broker Connection pin.', ''),
    })
    steps = [run_step('Validate execution phase', '''set -euo pipefail
case "$PHASE" in all|preflight|publish) ;; *) echo '::error::phase must be all, preflight or publish'; exit 1;; esac
''', env={'PHASE': '${{ inputs.phase }}'})]
    probe = copy.deepcopy(source['jobs']['preflight']['steps'][1])
    # Preflight is independent of a caller checkout and reads immutable bundled tools.
    probe['run'] = probe['run'].replace('.image-lane/scripts/', '"$GITHUB_ACTION_PATH/../../../scripts/').replace('mint-registry-auth.py', 'mint-registry-auth.py"')
    steps.append(condition(probe, "inputs.phase != 'publish'"))
    for step in copy.deepcopy(source['jobs']['publish']['steps']):
        if step.get('name') == 'Check out this lane':
            step = run_step('Snapshot the pinned lane tools', '''set -euo pipefail
# No network checkout or moving ref. These bytes belong to the pinned action.
mkdir -p .image-lane/scripts
for tool in mint-registry-auth pack-image-evidence read-registry-manifest; do
  cp "$GITHUB_ACTION_PATH/../../../scripts/$tool.py" ".image-lane/scripts/$tool.py"
done
''')
        elif step.get('name') == 'Authorize private cargo git deps':
            step = run_step('Authorize independent source repositories', 'python3 "$GITHUB_ACTION_PATH/../broker-git-credentials/configure.py"\n',
                            env={'BROKER_CI_NAME': '${{ inputs.broker-ci-name }}', 'REPOSITORIES': '${{ inputs.source-repositories }}',
                                 'TRUST_POLICIES': '${{ inputs.broker-trust-policies }}', 'API_URL': '${{ inputs.api-url }}',
                                 'EXPECTED_ISSUER': '${{ inputs.expected-issuer }}', 'EXPECTED_CONNECTION': '${{ inputs.expected-connection }}'},
                            **{'if': "inputs.source-repositories != ''"})
        steps.append(condition(step, "inputs.phase != 'preflight'"))
    outs = copy.deepcopy(source['jobs']['publish']['outputs'])
    outs = {key: {'description': source.get('on', source.get(True))['workflow_call']['outputs'][key]['description'], 'value': value} for key, value in outs.items()}
    return dict(name='Image lane (top-level identity)',
                description='Native build, evidence, publish and readback with Broker-only source reads. Top-level caller owns jobs, permissions, environment and concurrency; composite preserves its signed workflow identity.',
                inputs=ins, outputs=outs, runs={'using': 'composite', 'steps': steps})


def red_main():
    source = load('red-main')
    ins = inputs(source, ('app-id', 'environment'))
    ins.update({
        'broker-trust-policy': inp('Exact TrustPolicy granting this caller repository; no App-key or workflow-token fallback.', required=True),
        'broker-ci-name': inp('Exact Access CI declaration for the signed top-level workflow.', required=True),
        'broker-app-slug': inp('Expected App slug, checked against the Broker issuer.', required=True),
        'api-url': inp('Ordinary platform API endpoint.', 'https://api.sylphx.com'),
        'expected-issuer': inp('Optional exact Broker issuer pin.', ''),
        'expected-connection': inp('Optional exact Broker Connection pin.', ''),
    })
    steps = [run_step('Validate explicit Broker authority', '''set -euo pipefail
case "$MODE" in repair|lifecycle) ;; *) echo '::error::mode must be repair or lifecycle'; exit 1;; esac
test -n "$POLICY" && test -n "$CI_NAME" && test -n "$APP_SLUG" || { echo '::error::explicit Broker policy, CI name and App slug required'; exit 1; }
test "$TARGET_REPOSITORY" = "$GITHUB_REPOSITORY" || { echo '::error::red-main acts only on its own caller repository'; exit 1; }
''', env={'MODE': '${{ inputs.mode }}', 'POLICY': '${{ inputs.broker-trust-policy }}', 'CI_NAME': '${{ inputs.broker-ci-name }}',
          'APP_SLUG': '${{ inputs.broker-app-slug }}', 'TARGET_REPOSITORY': '${{ inputs.repository || github.repository }}'})]
    lifecycle = source['jobs']['lifecycle']
    mint = run_step('Mint repository-scoped lifecycle token', 'python3 "$GITHUB_ACTION_PATH/../broker-write-token/exchange.py"\n',
                    id='lifecycle-broker', env=broker_env(True))
    steps.append(condition(mint, "inputs.mode == 'lifecycle'"))
    reconcile = copy.deepcopy(lifecycle['steps'][-1])
    reconcile['env'] = {**lifecycle['env'], **reconcile['env']}
    reconcile['env']['REPO'] = '${{ inputs.repository || github.repository }}'
    reconcile['env'].update(GH_TOKEN='${{ steps.lifecycle-broker.outputs.token }}', APP_SLUG='${{ steps.lifecycle-broker.outputs.app-slug }}')
    reconcile['env'].pop('PYTHONPATH')
    reconcile['run'] = 'export PYTHONPATH="$GITHUB_ACTION_PATH/../ci-range"\n' + reconcile['run']
    steps.append(condition(reconcile, "inputs.mode == 'lifecycle'"))
    job = source['jobs']['red-main']
    for step in copy.deepcopy(job['steps']):
        sid, name = step.get('id'), step.get('name')
        if sid in ('app-token', 'app-token-act') or name == 'Read canonical run-store consumer':
            continue
        if sid == 'programs':
            step = run_step('Snapshot the pinned handler programs', '''set -euo pipefail
mkdir -p red-main-programs/.github .red-main-store/.github/actions
rm -rf red-main-programs/.github/red_main .red-main-store/.github/actions/run-store
cp -R "$GITHUB_ACTION_PATH/../../red_main" red-main-programs/.github/
cp -R "$GITHUB_ACTION_PATH/../run-store" .red-main-store/.github/actions/
''', id='programs')
        elif sid == 'key':
            step['env'] = {'DECLARED_MODE': '${{ steps.gate.outputs.mode }}'}
            step['name'] = 'Select the explicit Broker write path'
            step['run'] = '''set -euo pipefail
printf 'path=broker\npresent=no\nwrite=yes\nmode=%s\n' "$DECLARED_MODE" >>"$GITHUB_OUTPUT"
'''
        elif sid in ('broker-token', 'broker-token-act'):
            step.pop('uses'); step.pop('with'); step.pop('continue-on-error', None)
            step['run'] = 'python3 "$GITHUB_ACTION_PATH/../broker-write-token/exchange.py"\n'
            step['env'] = broker_env()
            if sid == 'broker-token-act':
                # The trace can exceed a token's one-hour lifetime. Never fall
                # back to that token if this separate fresh issuance fails.
                step['if'] = "always() && !cancelled() && steps.gate.outputs.proceed == 'true' && steps.broker-token.outputs.token != ''"
        if 'env' in step or 'run' in step:
            step['env'] = {**job.get('env', {}), **step.get('env', {})}
        # Keep workflow token only as the separate Actions/notice authority.
        for key, value in step.get('env', {}).items():
            if key == 'APP_ID':
                step['env'][key] = ''  # Unused by revert; no global App id assumption.
                continue
            if not isinstance(value, str):
                continue
            value = value.replace('steps.app-token.outputs.token || ', '').replace('steps.app-token-act.outputs.token || ', '')
            value = value.replace('steps.app-token.outputs.app-slug || ', '')
            value = value.replace("(steps.app-token.outputs.token != '' || steps.broker-token.outputs.token != '')", "(steps.broker-token.outputs.token != '')")
            value = value.replace('inputs.simulate-mint-failure &&', "inputs.simulate-mint-failure == 'true' &&")
            if key == 'GH_TOKEN' and sid not in ('gate', 'grant-notice') and name != 'Alert that the red-main handler itself failed':
                value = '${{ steps.broker-token-act.outputs.token }}' if sid == 'revert' or name == 'Record what the handler did' else '${{ steps.broker-token.outputs.token }}'
            step['env'][key] = value
        if sid == 'grant-notice':
            step['if'] = "always() && steps.gate.outputs.proceed == 'true' && (steps.broker-token.outcome == 'failure' || steps.probe.outcome == 'failure')"
            step['run'] = step['run'].replace('the App token mint for $REPO failed: the builder App installation does not grant contents, issues, pull-requests and workflows write, or has no installation on this repository', 'the Broker token exchange for $REPO failed; inspect the named exchange reason and exact CI/TrustPolicy grant')
        if sid == 'revert':
            step['if'] += " && steps.broker-token-act.outcome == 'success' && steps.broker-token-act.outputs.token != ''"
            step['env']['BROKER_ISSUER'] = '${{ steps.broker-token-act.outputs.issuer }}'
            step['env']['APP_SLUG'] = '${{ steps.broker-token-act.outputs.app-slug }}'
            step['run'] = 'export APP_ID="${BROKER_ISSUER#github-apps/}"\n' + step['run']
        if name == 'Record what the handler did':
            # Local record still runs after refresh failure, without API writes.
            step['run'] = step['run'].replace('if [ -s "$WORK_DIR/temp-branches" ]; then', 'if [ -n "$GH_TOKEN" ] && [ -s "$WORK_DIR/temp-branches" ]; then')
            step['run'] = step['run'].replace('[ -n "$HEAD_SHA" ] ||', '[ -n "$GH_TOKEN" ] || { echo "no fresh Broker token; API writes refused"; exit 0; }\n[ -n "$HEAD_SHA" ] ||')
        steps.append(condition(step, "inputs.mode == 'repair'"))
    return dict(name='Red main (top-level identity)', description='Broker-only repair or owned-alert lifecycle. Caller owns self-hosted job, permissions, environment and repository concurrency; external tenant reusable workflow remains unchanged.',
                inputs=ins, runs={'using': 'composite', 'steps': steps})


def render():
    return {'.github/actions/image-lane/action.yml': image(), '.github/actions/red-main/action.yml': red_main()}


def main():
    failed = False
    for name, doc in render().items():
        path = ROOT / name
        body = '# Generated by scripts/generate-ci-composites.py; do not edit directly.\n' + yaml.dump(doc, Dumper=Dumper, sort_keys=False, width=120)
        if '--check' in sys.argv:
            if not path.exists() or path.read_text() != body:
                print('out of date: ' + name, file=sys.stderr)
                failed = True
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
    return int(failed)


if __name__ == '__main__':
    raise SystemExit(main())
