"""Literal model selection and separately adopted recognition coverage."""
import importlib.util
import pathlib
import unittest

ACTION = pathlib.Path(__file__).resolve().parents[1] / ".github/actions/stack-conformance"
spec = importlib.util.spec_from_file_location("selector_stack", ACTION / "stack_conformance.py")
sc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sc)
models = sc.model_pins


class ModelSelectorTests(unittest.TestCase):
    def scan(self, source, path="src/ai.ts"):
        result = models.scan(sc.DictTree({path: source}), "example/product", None,
                             sc.architecture.source_views)
        self.assertEqual(result["unreadable"], [])
        return result["findings"]

    def test_witnessed_constant_snapshot_and_bare_family_fail(self):
        for source in ('const AI_MODEL = "deepseek/deepseek-v4.1-flash";',
                       'const model = "gpt-5.1-2025-11-13";',
                       'const model = "claude-sonnet";',
                       'const model = "gpt";'):
            with self.subTest(source=source):
                rows = self.scan(source)
                self.assertEqual(len(rows), 1)
                self.assertTrue(models.decide(rows, set(), set(), [], True, True))

    def test_arbitrary_provider_and_line_need_no_vocabulary_update(self):
        for provider in ("openai", "anthropic", "deepseek", "google", "new-vendor"):
            for source in ('const AI_MODEL = "PROVIDER/future-line";',
                           'client({model: "PROVIDER/future-line"});',
                           'client({model:\n  "PROVIDER/future-line"});',
                           'client.languageModel("PROVIDER/future-line");',
                           'const modelId = "PROVIDER/future-line";',
                           'client({"model": "PROVIDER/future-line"});',
                           'const model = Some("PROVIDER/future-line");',
                           'const defaultModel = env.MODEL ?? "PROVIDER/future-line";'):
                with self.subTest(provider=provider, source=source):
                    self.assertEqual(len(self.scan(source.replace("PROVIDER", provider))), 1)
        self.assertEqual(len(self.scan('const models = ["new-vendor/one", "new-vendor/two"];')), 2)
        self.assertEqual(len(self.scan('const options = {models: ["new-vendor/one",\n'
                                      '"new-vendor/two"]};')), 2)

    def test_config_fields_and_typed_constants(self):
        for path, source in {
            "config.json": '{"model": "new-vendor/future-line"}',
            "config.yaml": 'model:\n  new-vendor/future-line\n',
            "config.toml": 'model = "new-vendor/future-line"\n',
            "src/ai.rs": 'const AI_MODEL: &str = "new-vendor/future-line";',
            "src/ai.py": 'model = "new-vendor/future-line"\n',
            "src/ai.go": 'model := "new-vendor/future-line"',
        }.items():
            with self.subTest(path=path):
                self.assertEqual(len(self.scan(source, path)), 1)

    def test_default_reference_catalog_sku_and_documentation_pass(self):
        for source in ('client({model: organisation.defaultModel});',
                       'client({model: runtime.defaultModel});',
                       'client({prompt});',
                       'client({model: "new-vendor/future-line-latest"});',
                       '// model: "new-vendor/future-line"',
                       'const description = "model: \'new-vendor/future-line\'";',
                       'const description = "The model is new-vendor/future-line.";'):
            with self.subTest(source=source):
                self.assertEqual(self.scan(source), [])
        for path in ("docs/selection.md", "docs/selection.ts", "tests/selection.ts",
                     "fixtures/selection.json", "src/selection.test.ts"):
            with self.subTest(path=path):
                self.assertEqual(self.scan('const model = "new-vendor/future-line";', path), [])

    def test_unversioned_families_require_selection_context(self):
        for family in models.FAMILIES:
            with self.subTest(family=family):
                self.assertEqual(len(self.scan(f'const model = "{family}";')), 0 if family == "tts" else 1)
                self.assertEqual(len(self.scan(f'const model = "{family}-custom";')), 1)
        for source in ('const command = "claude-code";',
                       'const repository = "new-vendor/future-line";',
                       'const modelRepository = "new-vendor/repository";',
                       'const modelPrefix = "new-vendor/future-";',
                       'const modelRepository = "openai/repository";',
                       'const model = "Assets/Resources/CreateModel.prefab";',
                       'const model = "backend/room/model/PlayerOperateModel.java";',
                       'const DEFAULT_FIRST_TOKEN_IDLE_MODELS = "deepseek";',
                       'classifyModel("kimi-code", modelId);',
                       'const schema = {modelName: "tts"};',
                       'const category = {value: "O1"};',
                       'const SYNONYM_GROUPS = {model: ["gpt", "claude", "gemini"]};',
                       'const options = {model: runtime.default, repository: "new-vendor/repository"};',
                       'const description = "model"; const repository = "new-vendor/repository";'):
            with self.subTest(source=source):
                self.assertEqual(self.scan(source), [])
        self.assertEqual(self.scan('{"__type__":"cc.PrefabInfo","fileId":"asset-id/o4"}',
                                   "assets/prefab.json"), [])
        self.assertEqual(len(self.scan('const model = "new-vendor/o4";')), 1)
        for path in ("scripts/test-models-canary.sh", "scripts/compliance/smokes.ts",
                     "src/gallery-fixtures/web.ts"):
            self.assertEqual(self.scan('model = "stub/compliance"', path), [])

    def test_widening_adopts_only_inherited_new_coverage(self):
        before = self.scan('const model = "new-vendor/future-line";')
        self.assertEqual(models.rule_id(before[0]), models.SELECTOR_RULE)
        records = {models.baseline_key(row) for row in before}
        self.assertTrue(models.decide(before, records, set(), before, True, True))
        self.assertEqual(models.decide(before, records, set(), before, True, True,
                                      seed_rules={models.SELECTOR_RULE}), [])
        after = self.scan('const model = "new-vendor/future-line";\n'
                          'const otherModel = "new-vendor/other-line";')
        grown = {models.baseline_key(row) for row in after}
        self.assertTrue(models.decide(after, grown, set(), before, True, True,
                                     seed_rules={models.SELECTOR_RULE}))
        self.assertTrue(models.decide(before, records, set(), before, True, True,
                                     seed_rules={"model-pin"}))
        self.assertEqual(models.decide(before, records, records, before, True, True), [])
        self.assertTrue(models.decide([], records, records, before, True, True))

    def test_sql_predicates_do_not_select_the_replacement_model(self):
        for source in ("UPDATE agents SET model_id = NULL WHERE model_id = 'sylphx/executor';",
                       "UPDATE agents SET reason = 'retired; replace old model' "
                       "WHERE model_id = 'sylphx/executor';",
                       "DELETE FROM agents WHERE model = 'gpt-4o';",
                       "UPDATE agents SET model = CASE WHEN model = 'gpt-4o' THEN NULL END;"):
            self.assertEqual(self.scan(source, "migrations/repair.sql"), [])
        for source in ("UPDATE agents SET model_id = 'new-provider/new-line' WHERE id = 1;",
                       "UPDATE agents SET model = CASE WHEN model = 'gpt-4o' "
                       "THEN 'gpt-5.1' ELSE 'gpt-5.2' END;"):
            self.assertTrue(self.scan(source, "migrations/repair.sql"))

    def test_classifier_membership_and_weight_provenance_are_not_defaults(self):
        source = '{"taxonomy":{"classes":[{"overrides":[{"model":"deepseek-flash"}]}]},' \
                 '"behavior":{"modelLimits":[{"model":"kimi-for-coding"}]},' \
                 '"cascade":{"catalog":{"imageModel":"grok-imagine-image"}},' \
                 '"providers":{"future":{"defaultModel":"new-vendor/line"}}}'
        rows = self.scan(source, "catalog.json")
        self.assertEqual({row["model"] for row in rows}, {"grok-imagine-image", "new-vendor/line"})
        source = 'weights = {"repo":"owner/weights", "revision":"digest", "licence":"example", ' \
                 '"sha256":{}, "base_model":"owner/provenance"}\n' \
                 'model = "owner/request-model"\n'
        self.assertEqual([row["model"] for row in self.scan(source, "src/weights.py")],
                         ["owner/request-model"])
        source = 'class Example:\n    """Documentation only."""\n' + source
        self.assertEqual([row["model"] for row in self.scan(source, "src/weights.py")],
                         ["owner/request-model"])

    def test_diagnostic_fixture_list_cannot_hide_an_inference_request(self):
        source = '''pub fn provider_portable_tool_surface_proof(packs: &[ToolPackId]) -> Value {
            let models = ["sylphx/lumen", "openai/gpt-4.1-mini"];
            for model in models {
                let names = projection(packs);
                rows.push(json!({"modelId": model, "names": names}));
            }
            json!({"schema":"spi_tool_surface_model_independent_projection/v1",
                   "kind":"local_model_independent_projection_check"})
        }
        '''
        path = "crates/spiron-tools/src/tool_packs.rs"
        def scan(text, repository="SylphxAI/spiron"):
            return models.scan(sc.DictTree({path:text}), repository, None,
                               sc.architecture.source_views)["findings"]
        self.assertEqual(scan(source), [])
        self.assertTrue(scan(source, "example/product"))
        self.assertTrue(scan(source.replace('let names = projection(packs);',
                                            'let names = infer(model);')))
        self.assertEqual(len(scan(source + 'const MODEL: &str = "new-vendor/line";')), 1)


if __name__ == "__main__":
    unittest.main()
