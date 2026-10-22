"""Keep the live consumer proof connected to actual producers and shared source."""
from pathlib import Path
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
        self.assertIn("env['JUNIT_PY']", script)
        self.assertIn("env['WINDOW_PY']", script)
        self.assertIn("run == os.environ['GITHUB_RUN_ID']", script)
        self.assertNotIn('unittest.mock', script)


if __name__ == '__main__':
    unittest.main()
