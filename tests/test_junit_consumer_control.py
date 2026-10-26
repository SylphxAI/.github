"""Keep the live consumer proof connected to actual producers and shared source."""
from pathlib import Path
import json
import runpy
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


def workflow(name):
    # BaseLoader keeps GitHub's `on` key literal rather than YAML 1.1 boolean.
    return yaml.load((ROOT / '.github/workflows' / name).read_text(), Loader=yaml.BaseLoader)


class JunitConsumerControlTest(unittest.TestCase):
    def test_exact_matrix_producers_and_dispatch_are_gated(self):
        control = workflow('project-control.yml')
        self.assertIn('junit-producer-run', control['on']['workflow_dispatch']['inputs'])
        producer = control['jobs']['junit-producer']
        self.assertEqual(producer['strategy']['matrix']['shard'], ['1', '701'])
        self.assertEqual(producer['name'], 'JUnit fixture (${{ matrix.shard }})')
        self.assertEqual(producer['permissions']['id-token'], 'write')
        put = producer['steps'][-1]
        self.assertEqual(put['name'], 'run-store put ' + put['with']['name'])
        self.assertEqual(put['uses'], './.github/actions/run-store')
        self.assertNotIn('required', put['with'])  # proof fails if real publication fails
        consumer = control['jobs']['junit-consumer']
        self.assertEqual(consumer['uses'], './.github/workflows/junit-consumer.yml')
        self.assertEqual(consumer['permissions']['actions'], 'read')
        self.assertEqual(consumer['permissions']['id-token'], 'write')
        self.assertIn('junit-producer', control['jobs']['ci-ok']['needs'])
        self.assertIn('junit-consumer', control['jobs']['ci-ok']['needs'])

    def test_automatic_cross_run_and_shared_source_binding(self):
        control = workflow('junit-consumer-control.yml')
        self.assertEqual(control['on']['workflow_run']['workflows'], ['Project control'])
        self.assertEqual(control['jobs']['consumer']['with']['producer-run'],
                         "${{ format('{0}', github.event.workflow_run.id) }}")
        consumer = workflow('junit-consumer.yml')
        steps = consumer['jobs']['consume']['steps']
        self.assertEqual(steps[0]['with']['ref'], '${{ job.workflow_sha }}')
        self.assertEqual(consumer['permissions']['id-token'], 'write')
        self.assertEqual(consumer['permissions']['actions'], 'read')
        self.assertIn('check-junit-consumer.py', steps[-1]['run'])
        for name in ('project-control.yml', 'junit-consumer.yml', 'junit-consumer-control.yml'):
            text = (ROOT / '.github/workflows' / name).read_text()
            self.assertNotIn('actions/upload-artifact', text)
            self.assertNotIn('actions/download-artifact', text)
        script = (ROOT / 'scripts/check-junit-consumer.py').read_text()
        self.assertIn('get-junit.py', script)
        self.assertIn("run == os.environ['GITHUB_RUN_ID']", script)
        self.assertNotIn('unittest.mock', script)


class JunitConsumerReportsTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.reports, self.units = root / 'reports', root / 'units.tsv'
        self.reports.mkdir()
        self.lanes = {f'junit-consumer-{shard}': f'JUnit fixture ({shard})' for shard in (1, 701)}
        (self.reports / '.run-store-lanes.json').write_text(json.dumps(self.lanes))
        for shard in (1, 701):
            directory = self.reports / f'junit-consumer-{shard}' / 'nested'
            directory.mkdir(parents=True)
            (directory / 'junit.xml').write_text(
                f'<testsuite><testcase classname="suite" name="broken-{shard}"><failure/></testcase>'
                f'<testcase classname="suite" name="passing-{shard}"/></testsuite>')
        self.verify = runpy.run_path(str(ROOT / 'scripts/check-junit-consumer.py'))['verify_reports']

    def test_both_canonical_parsers_preserve_nested_matrix_test_identity(self):
        # Run the real parsers on report bytes, without inline workflow programs
        # or network/store mocks. The automatic consumer covers live transport.
        self.verify(self.reports, self.units)
        self.assertEqual(set(self.units.read_text().splitlines()), {
            'test\tsuite::broken-1\tJUnit fixture (1)',
            'test\tsuite::broken-701\tJUnit fixture (701)',
        })

    def test_wrong_lane_attribution_is_refused(self):
        (self.reports / '.run-store-lanes.json').write_text(json.dumps({
            'junit-consumer-1': 'JUnit fixture (701)',
            'junit-consumer-701': 'JUnit fixture (1)',
        }))
        with self.assertRaisesRegex(AssertionError, 'lane attribution mismatch'):
            self.verify(self.reports, self.units)

    def test_missing_nested_shard_is_refused(self):
        (self.reports / 'junit-consumer-701/nested/junit.xml').unlink()
        with self.assertRaisesRegex(AssertionError, 'Missing nested shard'):
            self.verify(self.reports, self.units)

    def test_wrong_failing_test_identity_is_refused(self):
        path = self.reports / 'junit-consumer-701/nested/junit.xml'
        path.write_text(path.read_text().replace('broken-701', 'wrong-test'))
        with self.assertRaisesRegex(AssertionError, 'Failed-unit parser lost'):
            self.verify(self.reports, self.units)

    def test_wrong_passing_test_identity_is_refused(self):
        path = self.reports / 'junit-consumer-701/nested/junit.xml'
        path.write_text(path.read_text().replace('passing-701', 'wrong-test'))
        with self.assertRaisesRegex(AssertionError, 'Regression-window parser lost'):
            self.verify(self.reports, self.units)


if __name__ == '__main__':
    unittest.main()
