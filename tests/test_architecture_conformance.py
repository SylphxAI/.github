"""Paired and adversarial cases through the public stack-conformance CLI."""
import datetime
import hashlib
import importlib.util
import json
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
ACTION = ROOT / '.github/actions/stack-conformance'
SPEC = importlib.util.spec_from_file_location('sc_arch_tests', ACTION / 'stack_conformance.py')
sc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sc)
a = sc.architecture
TODAY = datetime.date(2026, 10, 29)

VIOLATIONS = {
    'C1': ('src/auth.rs', 'fn auth() { if is_internal { return Ok(()); } }\n'),
    'C2': ('src/pay.ts', 'const payment = new Stripe(key);\n'),
    'C3': ('src/config.rs', 'const TENANT: &str = "cubeage";\n'),
    'C4': ('src/settings.py', 'import os\nvalue = os.getenv("UNDECLARED_SETTING")\n'),
    'C5': ('src/tasks.rs', 'async fn tasks() {\n loop {\n query();\n sleep(1);\n }\n}\n'),
    'C6': ('src/module.rs', '#[allow(dead_code)]\nfn old() {}\n'),
    'C7': ('.github/workflows/ci.yml', 'jobs:\n  verdict:\n    if: needs.test.result != \'failure\'\n'),
}
CLEAN = {
    'C1': ('src/auth.rs', 'fn auth() { if is_internal { authorize(grant)?; return Ok(()); } }\n'),
    'C2': ('src/pay.ts', 'const payment = platform.money.checkout(grant);\n'),
    'C3': ('src/config.rs', 'fn run(config: TenantConfig) { engine.run(config); }\n'),
    'C4': ('src/settings.py', 'import os\nvalue = os.getenv("DECLARED_SETTING")\n'),
    'C5': ('src/tasks.rs', 'async fn tasks() {\n for attempt in 0..3 {\n fetch();\n sleep(1);\n }\n}\n'),
    'C6': ('src/module.rs', '#[allow(dead_code)] // reason: called by external ABI\nfn callback() {}\n'),
    'C7': ('.github/workflows/ci.yml', 'jobs:\n  verdict:\n    if: needs.test.result != \'failure\' && needs.test.result != \'cancelled\'\n'),
}


class GitFixture:
    def __init__(self, directory):
        self.root = pathlib.Path(directory)
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')
        self.write({'README.md': 'test\n', '.env.example': 'DECLARED_SETTING=\n'})
        self.commit()
        self.base = self.git('rev-parse', 'HEAD').strip()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], text=True, stderr=subprocess.PIPE)

    def write(self, files):
        for name, text in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def commit(self):
        self.git('add', '.')
        self.git('commit', '-qm', 'fixture', '--allow-empty')

    def check(self, base=None, repository='SylphxAI/spiron'):
        report = self.root.parent / (self.root.name + '-report.json')
        result = subprocess.run(['python3', str(ACTION / 'stack_conformance.py'), 'check',
            '--repo', str(self.root), '--base', base or self.base, '--head', 'HEAD',
            '--repository', repository, '--today', TODAY.isoformat(), '--report', str(report),
            '--shared-source', str(ROOT)],
            text=True, capture_output=True)
        return result, json.loads(report.read_text()) if report.exists() else None


class PublicEntryPointTests(unittest.TestCase):
    def test_seven_seeds_fail_and_seven_controls_pass(self):
        for cls in VIOLATIONS:
            for bad, examples in [(True, VIOLATIONS), (False, CLEAN)]:
                with self.subTest(cls=cls, violation=bad), tempfile.TemporaryDirectory() as d:
                    f = GitFixture(d)
                    name, body = examples[cls]
                    f.write({name: body})
                    f.commit()
                    result, report = f.check()
                    self.assertEqual(result.returncode, int(bad), result.stdout + result.stderr)
                    self.assertEqual(bool([r for r in report['findings'] if r['class'] == cls]), bad)
                    self.assertEqual(report['commit'], f.git('rev-parse', 'HEAD').strip())
                    for row in report['findings']:
                        self.assertEqual(row['repository'], 'SylphxAI/spiron')
                        self.assertEqual(row['commit'], report['commit'])
                        self.assertTrue(row['contract'].startswith('https://github.com/SylphxAI/skills/'))
                        self.assertIsInstance(row['line'], int)

    def test_context_header_edit_cannot_hide_unchanged_poll_body(self):
        with tempfile.TemporaryDirectory() as d:
            f = GitFixture(d)
            f.write({'src/jobs.py': 'for attempt in range(3):\n    fetch()\n    sleep(1)\n'})
            f.commit()
            before = f.git('rev-parse', 'HEAD').strip()
            f.write({'src/jobs.py': 'while True:\n    fetch()\n    sleep(1)\n'})
            f.commit()
            result, report = f.check(base=before)
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertTrue(any(r['class'] == 'C5' and r['changed'] for r in report['findings']))

    def test_inherited_finding_visible_without_blocking_document_edit(self):
        with tempfile.TemporaryDirectory() as d:
            f = GitFixture(d)
            f.write(dict([VIOLATIONS['C5']]))
            f.commit()
            before = f.git('rev-parse', 'HEAD').strip()
            f.write({'README.md': 'new documentation\n'})
            f.commit()
            result, report = f.check(base=before)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertEqual(report['findings'][0]['disposition'], 'inherited')
            self.assertFalse(report['semantic_census_complete'])

    def test_removing_declaration_rescans_unchanged_consumers(self):
        with tempfile.TemporaryDirectory() as d:
            f = GitFixture(d)
            f.write(dict([CLEAN['C4']]))
            f.commit()
            before = f.git('rev-parse', 'HEAD').strip()
            f.write({'.env.example': '# setting was removed\n'})
            f.commit()
            result, report = f.check(base=before)
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertEqual(report['findings'][0]['class'], 'C4')

    def test_missing_base_fails_without_claiming_a_clean_report(self):
        with tempfile.TemporaryDirectory() as d:
            f = GitFixture(d)
            result, report = f.check(base='bad-base-does-not-exist')
            self.assertNotEqual(result.returncode, 0)
            self.assertIsNone(report)
            self.assertIn('base commit is unavailable', result.stderr)

    def test_non_utf8_supported_source_fails_explicitly(self):
        with tempfile.TemporaryDirectory() as d:
            f = GitFixture(d)
            (f.root / 'binary.py').write_bytes(b'\xff\xfe')
            f.commit()
            result, report = f.check()
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertEqual(report['unreadable'][0]['path'], 'binary.py')


class StructuralBoundaryTests(unittest.TestCase):
    def evaluate(self, files, repository='SylphxAI/spiron', changed=None):
        return a.evaluate(sc.DictTree(files), repository, changed, TODAY)

    def test_docs_comments_strings_and_test_paths_are_negatives(self):
        bodies = '\n'.join(x[1] for x in VIOLATIONS.values())
        report = self.evaluate({'docs/seeds.rs': bodies, 'tests/seeds.rs': bodies,
            'src/a_test.rs': bodies, 'src/lib.rs': '// if is_internal { return Ok(()); }\n'
            'const EXAMPLE: &str = r#"loop { query(); sleep(1); }"#;\n'
            'const LINT: &str = "#[allow(dead_code)]";\n'})
        self.assertEqual(report['findings'], [])
        self.assertEqual(len(report['excluded_paths']), 3)

    def test_inline_rust_test_module_does_not_hide_production(self):
        source = '#[cfg(test)]\nmod tests {\n' + VIOLATIONS['C5'][1] + '\n}\n'
        self.assertEqual(self.evaluate({'src/lib.rs': source})['findings'], [])
        self.assertTrue(self.evaluate({'src/lib.rs': source + VIOLATIONS['C5'][1]})['findings'])

    def test_generated_bundled_skill_seed_is_not_excluded(self):
        report = self.evaluate({'crates/spiron-tools/src/bundled_skills_generated.rs':
            'pub const SKILL: &str = "company method";\n'})
        self.assertEqual(report['findings'][0]['rule'], 'bundled-method')

    def test_platform_provider_adapter_allowed_but_product_adapter_is_not(self):
        files = {'providers/stripe.ts': 'const client = new Stripe(key);\n'}
        self.assertEqual(self.evaluate(files, 'SylphxAI/cloud')['findings'], [])
        self.assertEqual(self.evaluate(files, 'Cubeage/game')['findings'][0]['class'], 'C2')
        files = {'src/pay.ts': files['providers/stripe.ts']}
        self.assertTrue(self.evaluate(files, 'SylphxAI/cloud')['findings'])

    def test_ui_animation_debounce_frame_and_event_loops_pass(self):
        files = {'web/ui.ts': 'setTimeout(() => animate(), 16);\n'
            'setTimeout(() => search(term), 200);\n'
            'function tick() { update(dt); requestAnimationFrame(tick); }\n'
            'while (playing) { render(); }\n',
            'src/events.rs': 'while let Some(event) = stream.next().await { handle(event); }\n'}
        self.assertEqual(self.evaluate(files)['findings'], [])

    def test_interval_state_poll_fails_but_animation_interval_passes(self):
        for expression, fail in [('setInterval(() => fetchStatus(), 1000);', True),
                                 ('setInterval(() => fetch(), 1000);', True),
                                 ('setInterval(() => animate(), 16);', False)]:
            with self.subTest(expression=expression):
                self.assertEqual(bool(self.evaluate({'web/a.ts': expression})['findings']), fail)

    def test_cron_calendar_and_safety_resync_need_named_contract(self):
        cron = 'kind: CronJob\nmetadata:\n  annotations:\n{annotation}spec:\n  schedule: "0 9 * * *"\n'
        for annotation, fail in [('', True),
                ('    conformance.calendar-obligation: "https://company.test/contracts#daily-report"\n', False),
                ('    conformance.calendar-obligation: "https://company.test/contracts#expiry"\n', False),
                ('    conformance.calendar-obligation: "https://company.test/contracts#safety-resync"\n', False)]:
            with self.subTest(annotation=annotation):
                self.assertEqual(bool(self.evaluate({'infra/report.yml': cron.format(annotation=annotation)})['findings']), fail)

    def test_warm_floors_with_exact_yaml_lines(self):
        text = '# manifest\n---   \nkind: Service\nmetadata:\n  annotations:\n    autoscaling.knative.dev/min-scale: "1"\n---\nkind: ScaledObject\nmetadata:\n  annotations:\n    conformance.request-driven: "true"\nspec:\n  minReplicaCount: 2\n'
        rows = self.evaluate({'infra/service.yaml': text})['findings']
        self.assertEqual([r['line'] for r in rows], [6, 13])
        self.assertFalse(self.evaluate({'infra/service.yaml': text.replace('"1"', '"0"').replace('Count: 2', 'Count: 0')})['findings'])

    def test_hpa_not_assumed_request_driven(self):
        text = 'kind: HorizontalPodAutoscaler\nspec:\n  minReplicas: 2\n'
        self.assertEqual(self.evaluate({'infra/a.yaml': text})['findings'], [])
        text = 'metadata:\n  annotations:\n    conformance.request-driven: "true"\n' + text
        self.assertEqual(self.evaluate({'infra/a.yaml': text})['findings'][0]['class'], 'C5')

    def test_python_syntax_failure_is_explicit(self):
        report = self.evaluate({'src/a.py': 'if x:\n  "unterminated\n'})
        self.assertEqual(len(report['unreadable']), 1)
        self.assertEqual(report['findings'], [])

    def test_conflicting_defaults_fail_but_declared_precedence_passes(self):
        self.assertEqual(self.evaluate({'src/a.py': 'DEFAULT_PORT = 80\nDEFAULT_PORT = 81\n'})['findings'][0]['rule'], 'competing-default')
        self.assertEqual(self.evaluate({'src/a.py': 'DEFAULT_PORT = 80\nport = config.get("port", DEFAULT_PORT)\n'})['findings'], [])

    def test_retired_route_only_fails_with_completed_migration_contract(self):
        files = {'src/a.rs': 'router.route("/old", handler);\n',
            '.github/architecture-migrations.json': json.dumps({'completed': False, 'retired_routes': ['/old']})}
        self.assertEqual(self.evaluate(files)['findings'], [])
        files['.github/architecture-migrations.json'] = json.dumps({'completed': True, 'retired_routes': ['/old']})
        self.assertEqual(self.evaluate(files)['findings'][0]['rule'], 'retired-route')

    def test_merge_only_completion_fails_receipted_completion_passes(self):
        for code, fail in [('if merged { settle_completed(); }', True),
            ('if merged && receipt.verified { settle_completed(receipt); }', False)]:
            self.assertEqual(bool(self.evaluate({'src/goal.rs': code})['findings']), fail)

    def decision(self, row):
        return {k: row[k] for k in ('path', 'line', 'class', 'rule', 'source_sha256')} | {
            'repository': 'SylphxAI/spiron', 'owner': 'owner-infrastructure',
            'reason': 'Required minimum for database quorum', 'contract': a.CONTRACT + '#scale',
            'authority': 'https://company.test/decision/1', 'evidence': 'https://company.test/proof/1',
            'reconsideration': 'When consensus storage supports scale to zero', 'expires': '2026-11-29'}

    def test_exact_decision_visible_not_claimed_fixed(self):
        files = {'infra/service.yml': 'autoscaling.knative.dev/minScale: "1"\n'}
        row = self.evaluate(files)['findings'][0]
        decision = self.decision(row)
        files['.github/architecture-decisions.json'] = json.dumps({'version': 1, 'decisions': [decision]})
        report = self.evaluate(files)
        self.assertEqual(report['findings'][0]['decision'], decision)
        self.assertFalse(report['semantic_census_complete'])

    def test_bare_wildcard_expired_duplicate_mismatched_decisions_fail(self):
        source = 'autoscaling.knative.dev/minScale: "1"\n'
        files = {'infra/service.yml': source}
        decision = self.decision(self.evaluate(files)['findings'][0])
        for mutation in [{'reason': 'allow'}, {'path': 'infra/*'}, {'expires': '2026-01-01'},
                         {'repository': 'other/repo'}, {'source_sha256': '0'*64}]:
            with self.subTest(mutation=mutation):
                files['.github/architecture-decisions.json'] = json.dumps({'version': 1, 'decisions': [decision | mutation]})
                try:
                    result = self.evaluate(files)
                    self.assertTrue(result['unreadable'])
                except ValueError:
                    pass
        files['.github/architecture-decisions.json'] = json.dumps({'version': 1, 'decisions': [decision, decision]})
        with self.assertRaises(ValueError):
            self.evaluate(files)

    def test_findings_do_not_expose_source_values(self):
        secret = 'fake-value-that-must-never-appear-in-report'
        report = self.evaluate({'src/a.ts': 'const value = new Stripe("' + secret + '");\n'})
        self.assertTrue(report['findings'])
        self.assertNotIn(secret, json.dumps(report))

class AdversarialContextTests(unittest.TestCase):
    evaluate = StructuralBoundaryTests.evaluate
    decision = StructuralBoundaryTests.decision
    def test_docstring_only_classes_and_unicode_docstrings_are_readable(self):
        report = self.evaluate({'src/a.py': 'class Result:\n    """Only a marker."""\n\ndef f():\n    """中文 # noqa"""\n    return 1\n'})
        self.assertEqual(report['unreadable'], [])
        self.assertEqual(report['findings'], [])

    def test_rust_lifetimes_do_not_mask_polling_source(self):
        source = "fn run<'a>(value: &'a str) { loop { query(); sleep(1); } }\n"
        self.assertEqual(self.evaluate({'src/a.rs': source})['findings'][0]['class'], 'C5')

    def test_neighboring_fetch_does_not_turn_ui_timer_into_polling(self):
        source = 'setInterval(() => animate(), 16);\nfetchOnceOnDemand();\n'
        self.assertEqual(self.evaluate({'src/ui.ts': source})['findings'], [])

    def test_attempt_condition_without_progress_is_not_a_bound(self):
        for path, source in [('src/a.py', 'while attempts < 3:\n    fetch()\n    sleep(1)\n'),
                             ('src/a.rs', 'while attempts < 3 { fetch(); sleep(1); }')]:
            self.assertEqual(self.evaluate({path: source})['findings'][0]['class'], 'C5')

    def test_attempt_condition_with_progress_is_bounded_retry(self):
        for path, source in [('src/a.py', 'while attempts < 3:\n    fetch()\n    attempts += 1\n    sleep(1)\n'),
                             ('src/a.rs', 'while attempts < 3 { fetch(); attempts += 1; sleep(1); }')]:
            self.assertEqual(self.evaluate({path: source})['findings'], [])

    def test_literal_env_read_variations_share_declaration(self):
        for path, source in [('src/a.py', 'import os\nv = os.environ.get("DECLARED_SETTING")\n'),
                             ('src/a.ts', 'const v = process.env["DECLARED_SETTING"];\n')]:
            self.assertEqual(self.evaluate({path: source})['findings'][0]['class'], 'C4')
            self.assertEqual(self.evaluate({path: source, '.env.example': 'DECLARED_SETTING=\n'})['findings'], [])

    def test_yaml_comments_and_descriptions_are_not_executable_verdicts(self):
        source = '# if: needs.x.result != "failure"\ndescription: jq all(.[]; .result != "failure")\n'
        self.assertEqual(self.evaluate({'ci.yml': source})['findings'], [])

    def test_decision_does_not_survive_changed_context(self):
        files = {'infra/service.yml': 'autoscaling.knative.dev/minScale: "1"\n'}
        row = self.evaluate(files)['findings'][0]
        files['.github/architecture-decisions.json'] = json.dumps({'version': 1, 'decisions': [self.decision(row)]})
        files['infra/service.yml'] += '# new context\n'
        report = self.evaluate(files)
        self.assertTrue(report['unreadable'])
        self.assertIsNone(report['findings'][0]['decision'])

    def test_real_required_chain_rejects_skipped_failed_and_cancelled_stack(self):
        import os
        import yaml
        for workflow in (ROOT / '.github/workflows/project-control.yml', ROOT / 'workflow-templates/optimistic-gate.yml'):
            jobs = yaml.safe_load(workflow.read_text())['jobs']
            self.assertIn('stack', jobs['ci-ok']['needs'])
            step = next(s for s in jobs['ci-ok']['steps']
                        if s.get('uses', '').split('@')[0].endswith(('/needs-pass', '/ci-ok')))
            action = step['uses'].split('@')[0].rsplit('/', 1)[1]
            script = ROOT / f'.github/actions/{action}/{action.replace("-", "_")}.py'
            required = step['with']['required']
            self.assertIn('stack', required.split(','))
            for event, producer_run in [('pull_request', ''), ('merge_group', ''), ('push', ''),
                                        ('workflow_dispatch', ''), ('workflow_dispatch', '123')]:
                # Resolve the two event selections used by Project control;
                # GitHub evaluates these before setting the action's inputs.
                selected_required = required.replace(
                    "${{ github.event_name != 'push' && 'template-plan,template-verdict' || '' }}",
                    'template-plan,template-verdict' if event != 'push' else '')
                selected_required = selected_required.replace(
                    "${{ (github.event_name != 'workflow_dispatch' || !inputs.junit-producer-run) && 'junit-producer' || 'junit-consumer' }}",
                    'junit-producer' if event != 'workflow_dispatch' or not producer_run else 'junit-consumer')
                self.assertNotIn('${{', selected_required)
                selected_junit = ({'junit-producer', 'junit-consumer'} & set(selected_required.split(',')))
                if 'junit-producer' in jobs['ci-ok']['needs']:
                    self.assertEqual(selected_junit, {'junit-consumer'} if producer_run else {'junit-producer'})
                for target in ['stack', *sorted(selected_junit)]:
                    for result in ('success', 'failure', 'cancelled', 'skipped'):
                        needs = {k: {'result': 'success'} for k in jobs['ci-ok']['needs']}
                        for optional in {'junit-producer', 'junit-consumer'} - selected_junit:
                            if optional in needs:
                                needs[optional]['result'] = 'skipped'
                        needs[target]['result'] = result
                        run = subprocess.run(['python3', str(script)],
                            env=os.environ | {'NEEDS': json.dumps(needs), 'REQUIRED': selected_required, 'ADVISORY': '', 'EVENT_NAME': event,
                                'REQUIRED_UNLESS_MERGE_GROUP': ''}, capture_output=True, text=True)
                        self.assertEqual(run.returncode, int(result != 'success'),
                                         (workflow, event, producer_run, target, result, run.stdout))

class BaselineEntryPointTests(unittest.TestCase):
    def test_baseline_adopts_only_existing_debt_and_never_mutes_changed_source(self):
        with tempfile.TemporaryDirectory() as d:
            f = GitFixture(d)
            path, source = VIOLATIONS['C5']
            f.write({path: source, '.github/workflows/stack.yml':
                     'uses: SylphxAI/.github/.github/actions/stack-conformance@bc664ac7410c00d233fe4f729b6747969a4a9bed # main\n'})
            f.commit()
            existing = f.git('rev-parse', 'HEAD').strip()
            row = a.evaluate(sc.DictTree({path: source}), 'SylphxAI/spiron', None, TODAY)['findings'][0]
            record = a.baseline_key(row)
            f.write({'.github/stack-departures.txt': a.BASELINE_MARKER + '\n' +
                     sc.adoption.RULE_MARKER + 'architecture/C5/polling-loop\n' + record + '\n'})
            f.commit()
            result, report = f.check(base=existing)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertTrue(report['findings'][0]['baseline_recorded'])
            adopted = f.git('rev-parse', 'HEAD').strip()
            f.write({path: source.replace('query();', 'query_again();')})
            f.commit()
            result, report = f.check(base=adopted)
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn('stale architecture baseline record', result.stdout)
            self.assertTrue(report['findings'][0]['changed'])

    def test_empty_ratchet_cannot_be_reset_or_grown(self):
        with tempfile.TemporaryDirectory() as d:
            f = GitFixture(d)
            path, source = VIOLATIONS['C5']
            f.write({path: source, '.github/stack-departures.txt': a.BASELINE_MARKER + '\n'})
            f.commit()
            base = f.git('rev-parse', 'HEAD').strip()
            row = a.evaluate(sc.DictTree({path: source}), 'SylphxAI/spiron', None, TODAY)['findings'][0]
            f.write({'.github/stack-departures.txt': a.BASELINE_MARKER + '\n' + a.baseline_key(row) + '\n'})
            f.commit()
            result, report = f.check(base=base)
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn('cannot grow', result.stdout)
            f.write({'.github/stack-departures.txt': ''})
            f.commit()
            result, report = f.check(base=base)
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn('marker cannot be removed', result.stdout)

    def test_invalid_decision_fails_the_public_entrypoint(self):
        with tempfile.TemporaryDirectory() as d:
            f = GitFixture(d)
            f.write({'.github/architecture-decisions.json': '{"version":1,"decisions":[{"reason":"allow"}]}'})
            f.commit()
            result, report = f.check()
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertTrue(report['unreadable'])

    def test_unknown_class_schema_never_exposes_bad_configuration_values(self):
        with tempfile.TemporaryDirectory() as d:
            f = GitFixture(d)
            f.write({'.github/architecture-decisions.json': '{"version":1,"decisions":"fake-sensitive-value"}'})
            f.commit()
            result, report = f.check()
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertNotIn('fake-sensitive-value', result.stdout + json.dumps(report))

class DistributionTests(unittest.TestCase):
    def test_downloaded_action_without_git_uses_immutable_supplied_pin(self):
        import os
        import shutil
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            source = root / 'downloaded'
            action = source / '.github/actions/stack-conformance'
            shutil.copytree(ACTION, action)
            (source / 'scripts').mkdir()
            for name in ('action_pin_drift_check.py', 'audit_optimistic_merge.py', 'source_provenance.py'):
                shutil.copy(ROOT / 'scripts' / name, source / 'scripts' / name)
            (source / 'policy').mkdir()
            for name in ('architecture-repositories.json', 'source-relocations.json'):
                shutil.copy(ROOT / 'policy' / name, source / 'policy' / name)
            (root / 'consumer').mkdir()
            f = GitFixture(root / 'consumer')
            report = root / 'report.json'
            for pin, expected in [('a'*40, 0), ('main', 1)]:
                run = subprocess.run(['python3', str(action / 'stack_conformance.py'), 'check',
                    '--repo', str(f.root), '--base', f.base, '--head', 'HEAD', '--report', str(report),
                    '--repository', 'Cubeage/example'], capture_output=True, text=True,
                    env=os.environ | {'STACK_CONFORMANCE_CHECKER_COMMIT': pin})
                self.assertEqual(run.returncode, expected, run.stdout + run.stderr)
                data = json.loads(report.read_text())
                self.assertEqual(data['checker_commit'], pin if expected == 0 else None)

class StandardFitRegressionTests(unittest.TestCase):
    evaluate = StructuralBoundaryTests.evaluate

    def test_negative_fixture_declarations_are_excluded_but_real_invalid_configs_fail(self):
        invalid = {'sylphx.toml': 'this is = = not toml\n', 'env.schema.json': '{invalid\n'}
        for name, source in invalid.items():
            with self.subTest(name=name):
                fixture = 'tools/launch-gates/fixtures/b-invalid/' + name
                report = self.evaluate({fixture: source})
                self.assertEqual(report['unreadable'], [])
                self.assertEqual(report['findings'], [])
                self.assertIn(fixture, [row['path'] for row in report['excluded_paths']])
                with self.assertRaisesRegex(ValueError, 'invalid environment declaration'):
                    self.evaluate({'config/' + name: source})

    def test_fixture_environment_keys_cannot_authorize_production_reads(self):
        fixtures = {
            'tools/fixtures/valid/sylphx.toml': '[env]\nFIXTURE_SETTING = "example"\n',
            'tests/env.schema.json': '{"properties":{"FIXTURE_SETTING":{"type":"string"}}}\n',
            'tools/__fixtures__/.env.example': 'FIXTURE_SETTING=\n',
            '.github/actions/test/fixture.yml': 'env:\n  FIXTURE_SETTING: example\n',
        }
        source = 'import os\nvalue = os.getenv("FIXTURE_SETTING")\n'
        for path, declaration in fixtures.items():
            with self.subTest(path=path):
                report = self.evaluate({path: declaration, 'src/settings.py': source})
                self.assertEqual([row['rule'] for row in report['findings']], ['undeclared-setting'])
                real = 'config/' + pathlib.PurePosixPath(path).name
                if path.endswith('.yml'):
                    real = '.github/workflows/ci.yml'
                self.assertEqual(self.evaluate({real: declaration, 'src/settings.py': source})['findings'], [])

    def test_vendor_scope_excludes_dependency_sources_and_configuration_only(self):
        for scope in ('thirdparty', 'third_party', 'third-party', 'external'):
            with self.subTest(scope=scope):
                vendor = 'client/robot_socket/bot/' + scope + '/linux/scons/'
                files = {vendor + 'setup.py': 'print "Python 2 dependency"\n',
                         vendor + 'sylphx.toml': 'this is = = not toml\n',
                         vendor + '.env.example': 'VENDOR_SETTING=\n',
                         'src/thirdparty_adapter.py': 'import os\nvalue = os.getenv("VENDOR_SETTING")\n'}
                report = self.evaluate(files)
                self.assertEqual(report['unreadable'], [])
                self.assertEqual([row['path'] for row in report['findings']], ['src/thirdparty_adapter.py'])
                self.assertEqual(report['findings'][0]['rule'], 'undeclared-setting')
                self.assertEqual(len(report['excluded_paths']), 3)
                files['client/robot_socket/bot/src/setup.py'] = 'print "invalid production Python"\n'
                self.assertEqual([row['path'] for row in self.evaluate(files)['unreadable']],
                                 ['client/robot_socket/bot/src/setup.py'])

    def test_sylphx_environment_tables_and_inline_tables_declare_names(self):
        toml = '[env.CUBEAGE_ADMIN_CONNECT_URL]\nvalue = "https://example.invalid"\n[env]\nSECOND_SETTING = { value = "x" }\n'
        source = 'import os\na = os.getenv("CUBEAGE_ADMIN_CONNECT_URL")\nb = os.environ.get("SECOND_SETTING")\n'
        self.assertEqual(self.evaluate({'sylphx.toml': toml, 'src/a.py': source})['findings'], [])

    def test_toml_service_idle_floor_needs_a_recorded_decision(self):
        source = '[[services]]\nname = "api"\nmin_instances = 2\n'
        rows = self.evaluate({'sylphx.toml': source})['findings']
        self.assertEqual([(r['class'], r['rule'], r['line']) for r in rows], [('C5', 'idle-floor', 3)])
        self.assertEqual(self.evaluate({'sylphx.toml': source.replace('= 2', '= 0')})['findings'], [])

    def test_real_owning_service_paths_allow_only_their_capability(self):
        for path in ['services/money/crates/money-api/src/stripe.rs', 'kernel/billing/crates/billing-metering/src/stripe.rs']:
            source = 'let key = std::env::var("STRIPE_SECRET_KEY");\nlet base = "https://api.stripe.com/v1";\n'
            rows = self.evaluate({path: source, '.env.example': 'STRIPE_SECRET_KEY=\n'}, 'SylphxAI/cloud')['findings']
            self.assertEqual(rows, [])
        source = 'let base = "https://api.stripe.com/v1";\n'
        self.assertEqual(self.evaluate({'services/auth/src/a.rs': source}, 'SylphxAI/cloud')['findings'][0]['class'], 'C2')

    def test_existing_provider_host_bypasses_have_paired_controls(self):
        hosts = ['rest.nexmo.com/sms/json', 'buy.itunes.apple.com/verifyReceipt',
                 'androidpublisher.googleapis.com/androidpublisher', 'fcm.googleapis.com/v1',
                 'api.stripe.com/v1', 'fal.ai', 'queue.fal.ai/model', 'api.replicate.com/v1']
        for host in hosts:
            with self.subTest(host=host):
                source = 'const ENDPOINT = "https://' + host + '";\n'
                self.assertEqual(self.evaluate({'src/a.ts': source}, 'Cubeage/product')['findings'][0]['class'], 'C2')
                self.assertEqual(self.evaluate({'src/a.ts': '// ' + source}, 'Cubeage/product')['findings'], [])
                self.assertEqual(self.evaluate({'tests/a.ts': source}, 'Cubeage/product')['findings'], [])
        self.assertEqual(self.evaluate({'src/a.ts': 'const endpoint = "https://api.stripe.com.evil.invalid";\n'}, 'Cubeage/product')['findings'], [])

    def test_catalog_covers_products_missing_from_old_ten_name_list(self):
        for name in ['kalkas', 'tryit', 'tachyn']:
            source = 'const TENANT = "' + name + '";\n'
            self.assertEqual(self.evaluate({'src/a.ts': source})['findings'][0]['class'], 'C3')

    def test_cubeage_shared_layers_cannot_compile_a_title_identity(self):
        source = 'const TITLE = "fun-big2-hk";\n'
        for repository in ['Cubeage/cubeage-platform', 'Cubeage/cubeage-kit', 'Cubeage/tycoon-engine']:
            self.assertEqual(self.evaluate({'src/a.ts': source}, repository)['findings'][0]['class'], 'C3')
        self.assertEqual(self.evaluate({'src/a.ts': source}, 'Cubeage/fun-big2-hk')['findings'], [])

    def test_catalog_generation_counts_all_orgs_and_rejects_incomplete_sources(self):
        import runpy
        build = runpy.run_path(str(ROOT / 'scripts/build_architecture_catalog.py'))['build']
        snapshot = json.loads((ROOT / 'policy/architecture-repositories.json').read_text())
        generated = build(snapshot)
        self.assertEqual(len(generated['repositories']), sum(s['repository_count'] for s in generated['sources']))
        self.assertEqual({s['organization'] for s in generated['sources']}, {'SylphxAI', 'Cubeage', 'Cubeage-limited', 'EpiowAI', 'OzyrixLtd'})
        snapshot['sources'].append({'organization': 'TseFamily'})
        snapshot['repositories'].append({'full_name': 'TseFamily/personal', 'id': -1, 'archived': False})
        snapshot['products'].append({'repository': 'TseFamily/personal', 'repository_id': -1, 'product': 'Personal'})
        self.assertEqual(build(snapshot), generated)
        snapshot['sources'][0]['repository_count'] += 1
        with self.assertRaises(ValueError):
            build(snapshot)


class CompletionAndConfigNegatives(unittest.TestCase):
    evaluate = StructuralBoundaryTests.evaluate

    def test_evidence_must_be_a_positive_completion_condition(self):
        for source in ['if merged || receipt.verified { settle_completed(); }',
                       'if merged && !receipt.verified { settle_completed(); }',
                       'if merged { settle_completed(receipt); }']:
            self.assertEqual(self.evaluate({'src/a.rs': source})['findings'][0]['class'], 'C7')

    def test_toml_documentation_string_is_not_a_service_floor(self):
        source = 'description = """\nmin_instances = 2\n"""\n'
        self.assertEqual(self.evaluate({'sylphx.toml': source})['findings'], [])

class CatalogAndDeclarationCorrections(unittest.TestCase):
    evaluate = StructuralBoundaryTests.evaluate

    def test_upstream_services_and_tooling_are_not_downstream_products(self):
        files = {
            'services/build/crates/build-api/src/metering.rs': 'pub const SERVICE: &str = "build";\n',
            'services/ai/crates/ai-server/src/paid_dispatch.rs': 'pub const POOL: &str = "ai";\n',
            'tools/sylphx-gen/src/products.rs': 'pub const PLATFORM: &str = "platform";\n',
            'services/sandboxes/crates/sandboxes-api/src/cell.rs': 'let image = "build";\n',
        }
        self.assertEqual(self.evaluate(files, 'SylphxAI/cloud')['findings'], [])
        self.assertEqual(self.evaluate({'src/a.rs': 'const LEVEL: &str = "info";\n'}, 'Cubeage/cubeage-platform')['findings'], [])

    def test_epiow_and_ozyrix_are_engine_consumers(self):
        for name in ['epiow', 'Epiow', 'epiow-minutes', 'ozyrix', 'Ozyrix']:
            self.assertEqual(self.evaluate({'src/a.rs': 'const TENANT: &str = "' + name + '";\n'})['findings'][0]['class'], 'C3')
        self.assertEqual(self.evaluate({'src/a.rs': 'const TENANT: &str = "TACHYN";\n'})['findings'], [])
        self.assertEqual(self.evaluate({'src/a.rs': 'use tachyn_suffix;\n'})['findings'], [])

    def test_action_environment_mapping_is_a_declaration(self):
        files = {'.github/actions/action/action.yml': 'runs:\n  steps:\n    - env:\n        DECLARED_SETTING: ${{ inputs.setting }}\n      run: command\n',
                 'src/a.py': 'import os\na = os.getenv("DECLARED_SETTING")\n'}
        self.assertEqual(self.evaluate(files)['findings'], [])

    def test_yaml_declaration_removal_flags_new_gap_and_preserves_old_debt(self):
        with tempfile.TemporaryDirectory() as d:
            f = GitFixture(d)
            f.write({'.github/workflows/check.yml': 'jobs:\n  test:\n    env:\n      WAS_DECLARED: literal\n',
                     'src/a.py': 'import os\na = os.getenv("WAS_DECLARED")\nb = os.getenv("OLD_UNDECLARED")\n'})
            f.commit()
            before = f.git('rev-parse', 'HEAD').strip()
            f.write({'.github/workflows/check.yml': 'jobs:\n  test:\n    name: changed\n'})
            f.commit()
            result, report = f.check(base=before)
            self.assertEqual(result.returncode, 1, result.stdout)
            rows = {r['line']: r['disposition'] for r in report['findings']}
            self.assertEqual(rows[2], 'regression')
            self.assertEqual(rows[3], 'inherited')


if __name__ == '__main__':
    unittest.main()
