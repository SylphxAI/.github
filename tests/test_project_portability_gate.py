"""The delivered CLI refuses new debt and keeps inherited baselines shrinking."""
import pathlib
import tempfile
import unittest

from test_rule_adoption import Fixture, OLD, UNKNOWN, WORKFLOW, ROOT, sc, workflow

SOURCE = 'src/client.ts'
BAD = 'export const origin = "https://api.tenant.svc.cluster.local";\n'
SAFE = 'export const origin = "https://api.example.invalid";\n'
RULE = 'architecture/C1/project-portability'


class ProjectPortabilityGateTests(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Fixture(temporary.name)

    def test_live_cli_positive_clean_and_allowlisted_manifest_fixtures(self):
        f = self.fixture()
        base = f.commit({WORKFLOW: workflow(OLD), SOURCE: SAFE})
        f.commit({SOURCE: BAD})
        output, report = f.check(base, 1)
        self.assertIn('project-portability', output)
        self.assertEqual([r['disposition'] for r in report['findings'] if r['rule'] == 'project-portability'], ['regression'])
        f.commit({SOURCE: SAFE, 'deploy/service.yaml': 'origin: "https://api.tenant.svc.cluster.local"\n'})
        f.check(base, 0)

    def test_inherited_adoption_only_shrinks_and_copying_adds_debt(self):
        f = self.fixture()
        base = f.commit({WORKFLOW: workflow(OLD), SOURCE: BAD})
        f.commit({'README.md': 'unrelated change\n'})
        self.assertIn('project-portability', f.check(base, 1)[0])
        result = f.plan(base)
        self.assertIn(RULE, result['introduced_rules'])
        baseline = result['baseline_text']
        adopted = f.commit({WORKFLOW: workflow(UNKNOWN), sc.BASELINE: baseline})
        f.check(base, 0)
        f.commit({SOURCE: '// context moves the finding\n' + BAD})
        f.check(adopted, 0)
        f.commit({SOURCE: BAD + BAD})
        self.assertIn('project-portability', f.check(adopted, 1)[0])
        f.commit({SOURCE: SAFE})
        self.assertIn('stale architecture baseline', f.check(adopted, 1)[0])
        smaller = '\n'.join(line for line in baseline.splitlines() if not line.startswith('architecture C1 project-portability ')) + '\n'
        clean = f.commit({sc.BASELINE: smaller})
        f.check(adopted, 0)
        f.commit({SOURCE: BAD, sc.BASELINE: baseline})
        self.assertIn('cannot grow', f.check(clean, 1)[0])

    def test_new_debt_cannot_be_seeded_during_adoption(self):
        f = self.fixture()
        base = f.commit({WORKFLOW: workflow(OLD), SOURCE: SAFE})
        f.commit({SOURCE: BAD})
        with self.assertRaisesRegex(ValueError, 'new head debt'):
            f.plan(base)

    def test_capability_profile_binds_the_executable_module(self):
        self.assertIn(RULE, sc.adoption.current_rules())
        action = pathlib.Path(sc.__file__).parent
        files = {sc.adoption.PREFIX + p.name: p.read_text() for p in action.glob('*.py')}
        files[sc.adoption.PREFIX + 'action.yml'] = (action / 'action.yml').read_text()
        files['policy/deal-randomness-scope.json'] = (ROOT / 'policy/deal-randomness-scope.json').read_text()
        profile = sc.adoption.source_profile(sc.DictTree(files))
        self.assertIn(RULE, profile['rules'])
        self.assertIn(sc.adoption.PREFIX + 'project_portability.py', profile['sources'])
        del files[sc.adoption.PREFIX + 'project_portability.py']
        with self.assertRaisesRegex(ValueError, 'project portability checker source missing'):
            sc.adoption.source_profile(sc.DictTree(files))


if __name__ == '__main__':
    unittest.main()
