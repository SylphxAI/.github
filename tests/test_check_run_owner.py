"""Only repository-bound completed workflow owners prove an orphan."""
import importlib.util
from pathlib import Path
import unittest
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('owner', ROOT / '.github/actions/check_run_owner.py')
owner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(owner)


class CheckRunOwnerTests(unittest.TestCase):
    def test_exact_owner_url(self) -> None:
        for suffix in ('', '/attempts/2', '/job/17'):
            with self.subTest(suffix=suffix):
                self.assertEqual(
                    owner.workflow_owner_id('https://github.com/example/repo/actions/runs/42' + suffix,
                                            'example/repo'),
                    42,
                )

    def test_untrusted_urls_never_query_owner(self) -> None:
        urls = [
            None, '', 'http://github.com/example/repo/actions/runs/42',
            'https://elsewhere.test/example/repo/actions/runs/42',
            'https://github.com/other/repo/actions/runs/42',
            'https://github.com/example/repo/actions/runs/42/extra',
            'https://github.com/example/repo/actions/runs/42?x=1',
            'https://github.com/example/repo/actions/runs/42#job',
            'https://user@github.com/example/repo/actions/runs/42',
            'https://github.com/example/repo/actions/runs/0',
            'https://github.com/example/repo/actions/runs/42/job/0',
        ]
        for url in urls:
            with self.subTest(url=url):
                read = mock.Mock()
                self.assertFalse(owner.orphaned_check(
                    {'status': 'in_progress', 'details_url': url}, 'example/repo', read))
                read.assert_not_called()

    def test_only_explicit_completed_matching_owner_is_orphaned(self) -> None:
        cases = [
            ({'id': 42, 'status': 'completed'}, True),
            ({'id': 43, 'status': 'completed'}, False),
            ({'id': '42', 'status': 'completed'}, False),
            ({'id': 42, 'status': 'in_progress'}, False),
            ({'id': 42, 'status': 'queued'}, False),
            ({'id': 42}, False),
            (None, False),
            ([], False),
        ]
        for result, expected in cases:
            with self.subTest(result=result):
                self.assertIs(
                    owner.orphaned_check(
                        {'status': 'in_progress', 'details_url': 'https://github.com/example/repo/actions/runs/42'},
                        'example/repo',
                        lambda _: result,
                    ),
                    expected,
                )

    def test_completed_check_and_enterprise_origin(self) -> None:
        read = mock.Mock(return_value={'id': 42, 'status': 'completed'})
        check = {'status': 'completed', 'details_url': 'https://git.example/example/repo/actions/runs/42'}
        self.assertFalse(owner.orphaned_check(check, 'example/repo', read, 'https://git.example'))
        read.assert_not_called()
        check['status'] = 'in_progress'
        self.assertTrue(owner.orphaned_check(check, 'example/repo', read, 'https://git.example'))

    def test_review_and_merge_group_gate_deadline_contract(self) -> None:
        load = lambda path: yaml.load((ROOT / path).read_text(), Loader=yaml.BaseLoader)
        workflow = load('.github/workflows/pr-review.yml')
        self.assertTrue(all(int(job['timeout-minutes']) <= 15 for job in workflow['jobs'].values()))
        action = load('.github/actions/pr-review/action.yml')
        commands = {step.get('id'): step for step in action['runs']['steps'] if step.get('id')}
        self.assertIn('12m node', commands['review']['run'])
        final = next(step for step in action['runs']['steps'] if 'finalize' in step.get('run', ''))
        self.assertIn('always()', final['if'])
        self.assertIn('2m node', final['run'])
        cli = (ROOT / '.github/actions/pr-review/cli.mjs').read_text()
        github = (ROOT / '.github/actions/pr-review/github.mjs').read_text()
        self.assertIn('11 * 60_000', cli)
        self.assertIn('11 * 60_000', github)
        control = load('.github/workflows/project-control.yml')
        self.assertIn('merge_group', control['on'])
        self.assertLessEqual(int(control['jobs']['ci-ok']['timeout-minutes']), 15)
        self.assertEqual(control['jobs']['ci-ok']['if'], 'always()')
        polling = load('.github/actions/ci-ok-polling/action.yml')
        self.assertEqual(polling['inputs']['timeout-minutes']['default'], '14')
        terminal = polling['runs']['steps'][-1]
        self.assertIn('14m python3', terminal['run'])


if __name__ == '__main__':
    unittest.main()
