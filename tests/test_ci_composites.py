"""Generated behavior parity plus local shell/runtime checks (not deployed acceptance)."""
import copy
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('ci_composites', ROOT / 'scripts/generate-ci-composites.py')
generator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(generator)


def action(name):
    return yaml.safe_load((ROOT / '.github/actions' / name / 'action.yml').read_text())


class CompositeTests(unittest.TestCase):
    def test_generated_sources_are_current(self):
        subprocess.run(['python3', str(ROOT / 'scripts/generate-ci-composites.py'), '--check'], check=True)

    def test_all_shell_programs_parse_and_composite_schema_omits_job_fields(self):
        for name in ('image-lane', 'red-main'):
            for step in action(name)['runs']['steps']:
                self.assertNotIn('timeout-minutes', step)
                self.assertNotIn('secrets.', str(step))
                self.assertNotIn('job.workflow_sha', str(step))
                if 'run' in step:
                    self.assertEqual(step['shell'], 'bash')
                    check = subprocess.run(['bash', '-n'], input=step['run'], text=True, capture_output=True)
                    self.assertEqual(check.returncode, 0, step.get('name', '') + check.stderr)
                elif 'uses' in step:
                    self.assertTrue(step['uses'].startswith('actions/checkout@'))

    def test_image_preserves_all_behavior_except_explicit_identity_plumbing(self):
        source = generator.load('image-lane')
        expected = source['jobs']['publish']['steps']
        actual = action('image-lane')['runs']['steps']
        by_name = {step.get('name', 'checkout'): step for step in actual}
        changed = {'Check out this lane', 'Authorize private cargo git deps'}
        for step in expected:
            if step.get('name') in changed:
                continue
            other = copy.deepcopy(by_name[step.get('name', 'checkout')])
            other.pop('if', None)
            original = copy.deepcopy(step)
            original.pop('if', None)
            self.assertEqual(other, original, step.get('name'))
        self.assertEqual(set(action('image-lane')['outputs']), {'image_tag', 'digest', 'evidence_tag', 'evidence_digest', 'verify_receipt'})
        names = [step.get('name') for step in actual]
        self.assertLess(names.index('Publisher credential pre-flight (GitHub OIDC -> repository JWT)'), names.index('Build image'))
        self.assertLess(names.index('Remove the job-scoped git credential'), names.index('Registry auth (GitHub OIDC -> identitytoken)'))
        self.assertLess(names.index('Drop registry credentials before the caller verifier'), names.index('Verify promotion evidence'))
        for cleanup in ('Remove the job-scoped git credential', 'Remove registry credentials'):
            self.assertIn('always()', by_name[cleanup]['if'])
        self.assertIn("inputs.phase != 'publish'", by_name['Publisher credential pre-flight (GitHub OIDC -> repository JWT)']['if'])
        self.assertIn("inputs.phase != 'preflight'", by_name['Build image']['if'])

    def test_image_probe_reaches_bundled_tool_before_any_checkout_or_compile(self):
        step = next(step for step in action('image-lane')['runs']['steps'] if step.get('name', '').startswith('Publisher credential'))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / '.github/actions/image-lane'
            bundle.mkdir(parents=True)
            (root / 'scripts').mkdir()
            (root / 'scripts/mint-registry-auth.py').write_text('import sys; assert "--probe" in sys.argv; print("probe-refused"); sys.exit(13)\n')
            result = subprocess.run(['bash', '-c', step['run']], env={**os.environ, 'GITHUB_ACTION_PATH': str(bundle), 'LANE_IMAGE': 'registry.example/image', 'LANE_PREFLIGHT_TIMEOUT': '20'}, text=True, capture_output=True, cwd=directory)
            self.assertEqual(result.returncode, 13)
            self.assertIn('probe-refused', result.stdout)
            self.assertFalse((root / '.image-lane').exists())

    def test_red_main_retains_programs_and_uses_only_fresh_broker_for_post_trace_writes(self):
        source = generator.load('red-main')['jobs']['red-main']['steps']
        steps = action('red-main')['runs']['steps']
        by_id = {step['id']: step for step in steps if 'id' in step}
        # Every operational trace/classification body remains the same source.
        for step in source:
            if step.get('id') in ('gate', 'helpers', 'probe', 'run', 'idem', 'junit', 'infra', 'verdict', 'confirm', 'trace'):
                self.assertEqual(by_id[step['id']]['run'], step['run'])
        for key in ('broker-token', 'broker-token-act', 'lifecycle-broker'):
            self.assertEqual(by_id[key]['env']['SOFT_FAIL'], 'false')
            self.assertEqual(by_id[key]['env']['REPO'], '${{ github.repository }}')
            self.assertNotIn('continue-on-error', by_id[key])
        names = [step.get('id') for step in steps]
        self.assertLess(names.index('trace'), names.index('broker-token-act'))
        self.assertLess(names.index('broker-token-act'), names.index('revert'))
        self.assertEqual(by_id['revert']['env']['GH_TOKEN'], '${{ steps.broker-token-act.outputs.token }}')
        self.assertEqual(by_id['revert']['env']['APP_SLUG'], '${{ steps.broker-token-act.outputs.app-slug }}')
        self.assertEqual(by_id['revert']['env']['BROKER_ISSUER'], '${{ steps.broker-token-act.outputs.issuer }}')
        self.assertIn("steps.broker-token-act.outcome == 'success'", by_id['revert']['if'])
        record = next(step for step in steps if step.get('name') == 'Record what the handler did')
        self.assertEqual(record['env']['GH_TOKEN'], '${{ steps.broker-token-act.outputs.token }}')
        self.assertIn('no fresh Broker token; API writes refused', record['run'])
        lifecycle = next(step for step in steps if step.get('name') == 'Reconcile owned identity alert')
        compile(lifecycle['env']['LIFECYCLE_PY'], '<lifecycle>', 'exec')
        self.assertEqual(lifecycle['env']['LIFECYCLE_PY'], generator.load('red-main')['jobs']['lifecycle']['env']['LIFECYCLE_PY'])
        self.assertIn("inputs.simulate-mint-failure == 'true'", by_id['probe']['env']['SIMULATE'])

    def test_top_level_projection_passes_actionlint_when_available(self):
        import shutil
        if not shutil.which('actionlint'):
            self.skipTest('actionlint not installed')
        for name in ('image-lane', 'red-main'):
            doc = action(name)
            ins = {key: {'type': 'string', **{k: v for k, v in value.items() if k in ('default', 'required')}} for key, value in doc['inputs'].items()}
            wrapper = {'name': 'adapter schema probe', 'on': {'workflow_call': {'inputs': ins}},
                       'jobs': {'probe': {'runs-on': 'self-hosted', 'steps': doc['runs']['steps']}}}
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'workflow.yml'
                path.write_text(yaml.safe_dump(wrapper, sort_keys=False))
                result = subprocess.run(['actionlint', '-shellcheck=', '-pyflakes=', str(path)], text=True, capture_output=True, cwd=ROOT)
                self.assertEqual(result.returncode, 0, name + ': ' + result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
