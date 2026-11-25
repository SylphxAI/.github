#!/usr/bin/env python3
"""Tests for the stack-conformance ratchet."""

from __future__ import annotations

import contextlib
import json
import importlib.util
import io
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "stack_conformance", ROOT / ".github" / "actions" / "stack-conformance" / "stack_conformance.py")
sc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sc)

BUN = "FROM oven/bun:1.4.0-alpine@sha256:abc AS builder\nRUN bun install\nFROM oven/bun:1.4.0-alpine AS runner\nCMD [\"bun\", \"run\", \"src/main.ts\"]\n"
NITRO = 'FROM node:22-alpine\nCMD ["node", ".output/server/index.mjs"]\n'
RUST = "FROM rust:1.90 AS build\nFROM gcr.io/distroless/cc-debian12\nCMD [\"/app\"]\n"
TOML = """
[project]
name = "t"

[database.migrations]
engine = "{engine}"

[[services]]
name = "api"
[services.build]
strategy = "dockerfile"
dockerfile = "{dockerfile}"
"""


class DeparturesTest(unittest.TestCase):
    def test_rust_and_atlas_is_clean(self):
        t = sc.DictTree({
            "sylphx.toml": TOML.format(engine="atlas", dockerfile="crates/api/Dockerfile"),
            "crates/api/Dockerfile": RUST,
        })
        self.assertEqual(sc.departures(t), [])

    def test_default_migration_engines_are_clean_case_insensitively(self):
        for engine in ("sylphx", "SYLPHX", "Sylphx", "atlas", "ATLAS"):
            with self.subTest(engine=engine):
                t = sc.DictTree({"services/api/sylphx.toml": f'[database.migrations]\nengine = "{engine}"\n'})
                self.assertEqual(sc.departures(t), [])

    def test_non_default_migration_engines_are_departures(self):
        for engine in ("drizzle", "prisma", "typeorm"):
            with self.subTest(engine=engine):
                t = sc.DictTree({"services/api/sylphx.toml": f'[database.migrations]\nengine = "{engine}"\n'})
                self.assertEqual(sc.departures(t), [f"migrations services/api/sylphx.toml {engine}"])

    def test_bun_service_without_a_web_framework_is_a_departure(self):
        t = sc.DictTree({
            "sylphx.toml": TOML.format(engine="atlas", dockerfile="apps/api/Dockerfile"),
            "apps/api/Dockerfile": BUN,
            "apps/api/package.json": '{"dependencies": {"postgres": "3"}}',
        })
        self.assertEqual(sc.departures(t), ["bun-node-service apps/api/Dockerfile"])

    def test_web_app_on_node_is_allowed(self):
        t = sc.DictTree({
            "sylphx.toml": TOML.format(engine="atlas", dockerfile="apps/web/Dockerfile"),
            "apps/web/Dockerfile": "FROM oven/bun AS b\nFROM node:22-alpine AS runner\nCMD [\"node\", \"server.js\"]\n",
            "apps/web/package.json": '{"dependencies": {"next": "16"}}',
        })
        self.assertEqual(sc.departures(t), [])

    def test_each_solidstart_dependency_identifies_a_web_app(self):
        for field, name in (("dependencies", "@solidjs/start"),
                            ("dependencies", "solid-js"),
                            ("devDependencies", "vinxi")):
            with self.subTest(dependency=name):
                t = sc.DictTree({
                    "sylphx.toml": TOML.format(engine="atlas", dockerfile="Dockerfile"),
                    "Dockerfile": NITRO,
                    "package.json": json.dumps({field: {name: "1"}}),
                })
                self.assertEqual(sc.departures(t), [])

    def test_final_stage_alias_is_followed(self):
        t = sc.DictTree({
            "sylphx.toml": TOML.format(engine="atlas", dockerfile="Dockerfile"),
            "Dockerfile": "FROM node:22 AS base\nFROM base\n",
            "package.json": "{}",
        })
        self.assertEqual(sc.departures(t), ["bun-node-service Dockerfile"])

    def test_a_non_javascript_entrypoint_on_a_bun_base_is_not_a_service(self):
        t = sc.DictTree({
            "sylphx.toml": TOML.format(engine="atlas", dockerfile="migrator/Dockerfile"),
            "migrator/Dockerfile": 'FROM oven/bun:1.4\nENTRYPOINT ["atlas"]\nCMD ["migrate", "apply"]\n',
        })
        self.assertEqual(sc.departures(t), [])

    def test_monorepo_web_app_named_by_the_command_is_allowed(self):
        t = sc.DictTree({
            "sylphx.toml": TOML.format(engine="atlas", dockerfile="Dockerfile"),
            "Dockerfile": 'FROM oven/bun:1-alpine AS runner\nWORKDIR /app\nCMD ["bun", "apps/web/server.js"]\n',
            "package.json": '{"workspaces": ["apps/*"]}',
            "apps/web/package.json": '{"dependencies": {"next": "16"}}',
        })
        self.assertEqual(sc.departures(t), [])

    def test_nested_build_output_uses_the_nearest_web_package(self):
        for script in ("apps/web/.output/server/index.mjs", "/app/apps/web/.output/server/index.mjs"):
            with self.subTest(script=script):
                t = sc.DictTree({
                    "sylphx.toml": TOML.format(engine="atlas", dockerfile="Dockerfile"),
                    "Dockerfile": f'FROM node:24\nCMD ["node", "{script}"]\n',
                    "package.json": '{"workspaces": ["apps/*"]}',
                    "apps/web/package.json": '{"devDependencies": {"vite": "8"}}',
                })
                self.assertEqual(sc.departures(t), [])

    def test_nested_node_service_does_not_inherit_a_framework_from_another_package(self):
        for script, inner_package in (
            ("apps/api/.output/server/index.mjs", "apps/api/package.json"),
            ("apps/web/tools/dist/server.mjs", "apps/web/tools/package.json"),
        ):
            with self.subTest(script=script):
                t = sc.DictTree({
                    "sylphx.toml": TOML.format(engine="atlas", dockerfile="Dockerfile"),
                    "Dockerfile": f'FROM node:24\nCMD ["node", "{script}"]\n',
                    "package.json": '{"workspaces": ["apps/*"]}',
                    "apps/web/package.json": '{"devDependencies": {"vite": "8"}}',
                    inner_package: '{"dependencies": {"postgres": "3"}}',
                })
                self.assertEqual(sc.departures(t), ["bun-node-service Dockerfile"])

    def test_shell_form_command_on_a_bun_base_is_a_service(self):
        t = sc.DictTree({
            "sylphx.toml": TOML.format(engine="atlas", dockerfile="worker/Dockerfile"),
            "worker/Dockerfile": "FROM oven/bun:1.4\nCMD sh worker/start.sh\n",
        })
        self.assertEqual(sc.departures(t), ["bun-node-service worker/Dockerfile"])

    def test_entrypoint_script_wrapping_bun_is_a_service(self):
        t = sc.DictTree({
            "sylphx.toml": TOML.format(engine="atlas", dockerfile="backend/Dockerfile"),
            "backend/Dockerfile": 'FROM oven/bun:1.4\nENTRYPOINT ["./entrypoint.sh"]\nCMD ["bun", "run", "web-prod"]\n',
        })
        self.assertEqual(sc.departures(t), ["bun-node-service backend/Dockerfile"])

    def test_server_framework_dependency_is_a_departure(self):
        t = sc.DictTree({
            "server/package.json": '{"dependencies": {"hono": "4", "zod": "3"}}',
            "web/package.json": '{"devDependencies": {"express": "5"}}',
            "node_modules/x/package.json": '{"dependencies": {"express": "5"}}',
        })
        self.assertEqual(sc.departures(t), ["ts-server server/package.json hono"])

    def test_dockerfile_relative_to_a_nested_manifest(self):
        t = sc.DictTree({
            "svc/sylphx.toml": TOML.format(engine="custom", dockerfile="Dockerfile"),
            "svc/Dockerfile": BUN,
            "svc/package.json": "{}",
        })
        self.assertEqual(sc.departures(t), [
            "bun-node-service svc/Dockerfile", "migrations svc/sylphx.toml custom"])

    def test_line_reader_matches_tomllib(self):
        text = TOML.format(engine="drizzle", dockerfile="a/Dockerfile")
        data = sc.parse_toml_lines(text)
        self.assertEqual(data["database"]["migrations"]["engine"], "drizzle")
        self.assertEqual(sc.dockerfiles(data), ["a/Dockerfile"])

    def test_solar2d_project_is_a_departure(self):
        t = sc.DictTree({
            "build.settings": "settings = { orientation = { default = 'portrait' } }\n",
            "main.lua": "display.setStatusBar(display.HiddenStatusBar)\n",
        })
        self.assertEqual(sc.departures(t), ["solar2d build.settings"])
        t_without_main = sc.DictTree({
            "build.settings": "settings = { orientation = { default = 'portrait' } }\n",
        })
        self.assertEqual(sc.departures(t_without_main), [])


class DecideTest(unittest.TestCase):
    FOUND = ["migrations sylphx.toml drizzle", "ts-server api/package.json hono"]

    def test_baselined_departures_pass(self):
        base = set(self.FOUND)
        self.assertEqual(sc.decide(self.FOUND, set(self.FOUND), base), [])

    def test_a_new_departure_fails(self):
        errors = sc.decide(self.FOUND, {self.FOUND[0]}, {self.FOUND[0]})
        self.assertEqual(len(errors), 1)
        self.assertIn("new departure", errors[0])
        self.assertIn("hono", errors[0])

    def test_adding_to_the_baseline_fails(self):
        errors = sc.decide(self.FOUND, set(self.FOUND), {self.FOUND[0]})
        self.assertTrue(any("baseline grew" in e for e in errors))
        self.assertTrue(any("new departure" in e for e in errors))

    def test_a_stale_line_emits_a_notice_without_errors(self):
        notices = []
        self.assertEqual(sc.decide(self.FOUND[:1], set(self.FOUND), set(self.FOUND), notice=notices.append), [])
        self.assertEqual(len(notices), 1)
        self.assertIn("stale baseline line", notices[0])

    def test_removing_a_departure_and_its_line_passes(self):
        self.assertEqual(sc.decide(self.FOUND[:1], {self.FOUND[0]}, set(self.FOUND)), [])

    def test_first_adoption_seeds_from_the_head(self):
        self.assertEqual(sc.decide(self.FOUND, set(self.FOUND), None), [])

    def test_no_baseline_anywhere_fails_every_departure(self):
        self.assertEqual(len(sc.decide(self.FOUND, None, None)), 2)

    def test_baseline_comments_and_spacing(self):
        parsed = sc.parse_baseline("# header\n\nts-server  api/package.json hono  # rewrite booked\n")
        self.assertEqual(parsed, {"ts-server api/package.json hono"})
        self.assertIsNone(sc.parse_baseline(None))


class KeelGameGuardTest(unittest.TestCase):
    MANIFEST = '[package]\nname = "example-game"\n[dependencies]\nkeel = "0.1"\n'

    def tree(self, **files):
        return sc.DictTree({"Cargo.toml": self.MANIFEST, **files})

    def test_detects_workspace_renamed_and_target_dependencies(self):
        manifests = [
            '[workspace.dependencies]\nkeel = "0.1"\n',
            '[dependencies]\nengine = { package = "keel", version = "0.1" }\n',
            '[dependencies]\nengine = { package = "sylphx-keel-engine", version = "0.1" }\n',
            '[target.\'cfg(wasm)\'.dependencies]\ncubeage-kit = "0.1"\n',
            '[dependencies.keel_commerce]\nversion = "0.1"\n',
        ]
        for text in manifests:
            with self.subTest(text=text):
                self.assertTrue(sc.keel_game(sc.DictTree({"crates/game/Cargo.toml": text})))
                self.assertTrue(sc.keel_game(sc.DictTree({"Cargo.toml": text})))

    def test_line_reader_works_without_tomllib(self):
        original = sc.tomllib
        try:
            sc.tomllib = None
            self.assertTrue(sc.keel_game(self.tree()))
        finally:
            sc.tomllib = original

    def test_shared_engine_and_commerce_owners_are_not_game_consumers(self):
        for name in ("keel", "keel-commerce", "sylphx-keel-engine", "sylphx-keel-commerce", "cubeage-kit"):
            tree = sc.DictTree({"Cargo.toml": self.MANIFEST.replace("example-game", name),
                                "src/commerce.rs": "struct CoinSku;"})
            self.assertEqual(sc.keel_departures(tree), [])

    def test_stack_lane_also_runs_for_document_only_and_empty_ranges(self):
        text = (ROOT / "workflow-templates" / "optimistic-gate.yml").read_text()
        lane = next(line for line in text.splitlines() if line.strip().startswith("stack:"))
        spec = importlib.util.spec_from_file_location("range_stack_test", ROOT / ".github/actions/ci-range/ci_range.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        lanes = module.parse_lanes(lane)
        for paths in ([], ["README.md"], ["src/a.rs"], ["infra/service.yml"], ["docs/BUILDING.md"]):
            self.assertTrue(module.select(lanes, paths)["stack"])


class ModelPinTest(unittest.TestCase):
    # This fixture is an input declaration, never the production allowlist.
    ALIASES = frozenset({"example-vendor/example-line-latest"})

    def scan(self, files, repository="example/app"):
        return sc.model_pins.scan(sc.DictTree(files), repository, self.ALIASES,
                                  sc.architecture.source_views)["findings"]

    def test_vendor_versions_are_refused_without_requiring_a_model_key(self):
        for model in ("deepseek/deepseek-v4.1-flash", "claude-opus-4-5-20251101",
                      "gpt-5.1-2025-11-13"):
            with self.subTest(model=model):
                rows = self.scan({"src/client.ts": f'const options = {{model: "{model}"}};\n'})
                self.assertEqual(len(rows), 1)
                errors = sc.model_pins.decide(rows, set(), set(), [], True, True)
                self.assertTrue(any("runtime default" in e and "catalog alias" in e for e in errors))
                self.assertEqual(len(self.scan({"src/client.rs": f'const MODEL: &str = "{model}";'})), 1)

    def test_only_declared_aliases_pass(self):
        self.assertEqual(self.scan({"src/client.ts": 'call({model: "example-vendor/example-line-latest"});'}), [])
        self.assertEqual(self.scan({"src/client.ts": "call({prompt});"}), [])
        self.assertEqual(len(self.scan({"src/client.ts": 'call({model: "deepseek/unlisted-line-latest"});'})), 1)

    def test_default_contract_allows_latest_aliases_without_network_or_catalog_snapshot(self):
        files = {"src/client.ts": 'call({model: "deepseek/deepseek-flash-latest"});'}
        self.assertIsNone(sc.model_pins.load_aliases())
        self.assertEqual(sc.model_pins.scan(sc.DictTree(files), "example/app", None,
                                            sc.architecture.source_views)["findings"], [])

    def test_variables_libraries_cli_names_and_non_model_metadata_pass(self):
        self.assertEqual(self.scan({
            "src/math.rs": "let o1 = phi.sin() + phi.cos();",
            "src/client.py": "from llama_index.core.schema import Document\nwhisper_json = {}\n",
            "src/client.ts": 'const GPT_TRANSCRIBE = "audio"; const TTS_URL = tts.generate();',
            "settings.json": '{"hook": "--target claude-code", "bone": "yi_body_1"}',
            "policy/repositories.json": '{"full_name": "SylphxAI/gpt-review"}',
            "package.json": '{"dependencies": {"google/apiclient-services": "1"}}',
            "src/fixture.ts": "const order = { id: 'o1' };",
            "scripts/submit.sh": 'repository="microsoft/winget-pkgs"\n',
            "scripts/benchmark.yml": 'command: "$RUNNER_TEMP/llama-b11177/llama-server"\n',
            "ground-truth.json": '{"text": "The GPT-2 example explains the format."}',
            "openapi.json": '{"description": "Call `gpt-5.1` for this example."}',
            "scripts/eval.py": 'archive = "tts.tar.bz2"\n',
            "src/tests.rs": 'const MODEL = "gpt-5.1-2025-11-13";',
            "thirdparty/legacy.py": 'print "Python 2"\n',
        }), [])

    def test_vendor_prefix_predicates_are_not_complete_model_ids(self):
        self.assertEqual(self.scan({
            "src/policy.py": 'any(model.startswith("openai/gpt-") for model in models)\n',
            "src/policy.ts": 'const modelPrefix = "anthropic/claude-";\n',
            "config.yml": 'model_prefix: "deepseek/deepseek-"\n',
        }), [])
        self.assertEqual(len(self.scan({
            "src/policy.py": 'model = "openai/custom-version-v7"\n',
        })), 1)

    def test_test_files_comments_and_inline_rust_test_modules_pass(self):
        pinned = 'const MODEL = "gpt-5.1-2025-11-13";'
        for path in ("tests/client.ts", "fixtures/client.json", "src/client.test.ts",
                     "src/test_client.py", "src/client_tests.rs", "src/client.spec.ts"):
            with self.subTest(path=path):
                self.assertEqual(self.scan({path: pinned}), [])
        self.assertEqual(self.scan({"src/client.rs": '#[cfg(test)]\nmod tests { ' + pinned + " }"}), [])
        self.assertEqual(self.scan({"src/client.rs": '#[cfg(test)]\npub(super) mod tests { ' + pinned + " }"}), [])
        self.assertEqual(self.scan({"src/client.rs": '#[tokio::test]\nasync fn check() { ' + pinned + " }"}), [])
        self.assertEqual(self.scan({"src/client.ts": "// " + pinned}), [])
        self.assertEqual(self.scan({"config.yml": '# model: "gpt-5.1-2025-11-13"\n'}), [])
        self.assertEqual(len(self.scan({"src/client.rs": pinned + '\n#[cfg(test)] mod tests { ' + pinned + " }"})), 1)

    def test_configuration_and_raw_strings_are_checked(self):
        for path, text in {
            "config.yml": "model: deepseek/deepseek-v4.1-flash\n",
            "config.toml": 'model = "gpt-5.1-2025-11-13"\n',
            "config.json": '{"model": "claude-opus-4-5-20251101"}',
            "src/client.rs": 'let request = r#"{"model":"gpt-5.1-2025-11-13"}"#;',
            "src/Client.cs": 'const string Model = "gpt-5.1-2025-11-13";',
            "src/client.swift": 'let model = "gpt-5.1-2025-11-13"',
            "src/config.rs": 'const MODEL: &str = "soniox/stt-rt-v5";',
            "config.yml": 'model: "soniox/stt-rt-v5"\n',
        }.items():
            with self.subTest(path=path):
                self.assertEqual(len(self.scan({path: text})), 1)

    def test_optional_catalog_declaration_is_validated_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder, "aliases.json")
            data = {"version": 1, "aliases": sorted(self.ALIASES),
                    "source": {"repository": "SylphxAI/sylphx", "commit": "a" * 40,
                               "path": "catalog/aliases.json", "sha256": "b" * 64}}
            path.write_text(json.dumps(data))
            self.assertEqual(sc.model_pins.load_aliases(path), self.ALIASES)
            del data["source"]
            path.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                sc.model_pins.load_aliases(path)
            with self.assertRaises(OSError):
                sc.model_pins.load_aliases(path.with_name("missing.json"))

    def test_every_reasoned_exemption_is_narrow_and_tested(self):
        for entry in sc.model_pins.EXEMPTIONS:
            self.assertTrue(entry["reason"].strip())
            path = entry["path"] + "model.rs" if entry["path"].endswith("/") else entry["path"]
            pinned = 'MODEL = "gpt-5.1-2025-11-13"'
            with self.subTest(path=path):
                self.assertEqual(self.scan({path: pinned}, entry["repository"]), [])
                self.assertEqual(len(self.scan({path: pinned}, "example/product")), 1)
        self.assertEqual(len(self.scan({"src/client.rs": 'const MODEL: &str = "gpt-5.1-2025-11-13";'}, "SylphxAI/sylphx")), 1)

    def test_duplicates_and_new_versions_cannot_hide_in_a_baseline(self):
        first = self.scan({"src/client.ts": 'call("gpt-5.1-2025-11-13");'})
        recorded = {sc.model_pins.baseline_key(row) for row in first}
        self.assertEqual(sc.model_pins.decide(first, recorded, recorded, first, True, True), [])
        duplicate = self.scan({"src/client.ts": 'call("gpt-5.1-2025-11-13");\ncall("gpt-5.1-2025-11-13");'})
        self.assertTrue(sc.model_pins.decide(duplicate, recorded, recorded, first, True, True))
        grown = {sc.model_pins.baseline_key(row) for row in duplicate}
        self.assertTrue(any("cannot grow" in e for e in sc.model_pins.decide(duplicate, grown, recorded, first, True, True)))
        notices = []
        self.assertEqual(sc.model_pins.decide([], recorded, recorded, first, True, True, notice=notices.append), [])
        self.assertIn("stale model-pin baseline record", notices[0])
        self.assertTrue(sc.model_pins.decide(first, recorded, recorded, [], True, True))

    def test_adoption_seeds_only_base_findings_and_marker_cannot_disappear(self):
        first = self.scan({"src/client.ts": 'call("gpt-5.1-2025-11-13");'})
        recorded = {sc.model_pins.baseline_key(row) for row in first}
        self.assertEqual(sc.model_pins.decide(first, recorded, set(), first, True, False, allow_seed=True), [])
        self.assertTrue(sc.model_pins.decide(first, recorded, set(), first, True, False))
        self.assertTrue(sc.model_pins.decide(first, recorded, set(), [], True, False))
        self.assertTrue(sc.model_pins.decide([], set(), set(), [], False, True))

    def test_baseline_paths_with_spaces_are_lossless(self):
        rows = self.scan({"src/model  examples.ts": 'call("gpt-5.1-2025-11-13");'})
        key = sc.model_pins.baseline_key(rows[0])
        self.assertIn("model%20%20examples.ts", key)
        self.assertEqual(sc.parse_baseline(key), {key})


class GitTest(unittest.TestCase):
    """check and all against a real repository with a base and a head commit."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = self.tmp.name
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", "-C", self.repo, *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, files):
        for path, text in files.items():
            p = pathlib.Path(self.repo, path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "c")
        return self.git("rev-parse", "HEAD")

    def check(self, base, head):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = sc.main(["check", "--repo", self.repo, "--base", base, "--head", head,
                          "--shared-source", str(ROOT), "--report", str(pathlib.Path(self.repo) / ".git" / "report.json")])
        return rc, out.getvalue()

    def test_ratchet_over_commits(self):
        base = self.commit({
            "sylphx.toml": TOML.format(engine="drizzle", dockerfile="none"),
            sc.BASELINE: "migrations sylphx.toml drizzle\n",
        })
        same = self.commit({"README.md": "x\n"})
        self.assertEqual(self.check(base, same)[0], 0)

        added = self.commit({"api/package.json": '{"dependencies": {"fastify": "5"}}'})
        rc, out = self.check(same, added)
        self.assertEqual(rc, 1)
        self.assertIn("ts-server api/package.json fastify", out)

        grown = self.commit({sc.BASELINE: "migrations sylphx.toml drizzle\nts-server api/package.json fastify\n"})
        rc, out = self.check(same, grown)
        self.assertEqual(rc, 1)
        self.assertIn("baseline grew", out)

    def test_migration_engine_switch_passes_without_a_baseline_departure(self):
        path = "services/api/sylphx.toml"
        manifest = '[database.migrations]\nengine = "{}"\n'
        base = self.commit({path: manifest.format("atlas"), sc.BASELINE: ""})
        head = self.commit({path: manifest.format("sylphx")})
        rc, out = self.check(base, head)
        self.assertEqual(rc, 0, out)
        self.assertIn("0 departure(s) found", out)

        for engine in ("drizzle", "prisma", "typeorm"):
            with self.subTest(engine=engine):
                other = self.commit({path: manifest.format(engine)})
                rc, out = self.check(head, other)
                self.assertEqual(rc, 1, out)
                self.assertIn(f"new departure from the default stack: 'migrations {path} {engine}'", out)

    def test_solidstart_nitro_check_passes_with_generated_output(self):
        base = self.commit({sc.BASELINE: ""})
        head = self.commit({
            "sylphx.toml": TOML.format(engine="atlas", dockerfile="Dockerfile"),
            "Dockerfile": NITRO,
            "package.json": json.dumps({"dependencies": {"@solidjs/start": "1", "solid-js": "1"},
                                        "devDependencies": {"vinxi": "0.5"}}),
            ".output/server/index.mjs": "export {};\n",
            # These generated runtime manifests would trip ts-server if scanned.
            ".output/server/package.json": '{"dependencies": {"h3": "1"}}',
            ".vinxi/server/package.json": '{"dependencies": {"h3": "1"}}',
        })
        rc, out = self.check(base, head)
        self.assertEqual(rc, 0, out)
        self.assertIn("0 departure(s) found", out)

    def test_non_web_node_and_bun_checks_fail_closed(self):
        base = self.commit({sc.BASELINE: ""})
        for image, dockerfile in (("node", NITRO), ("bun", BUN)):
            with self.subTest(image=image):
                head = self.commit({
                    "sylphx.toml": TOML.format(engine="atlas", dockerfile="Dockerfile"),
                    "Dockerfile": dockerfile,
                    "package.json": '{"dependencies": {"postgres": "3"}}',
                })
                rc, out = self.check(base, head)
                self.assertEqual(rc, 1, out)
                self.assertIn("new departure from the default stack: 'bun-node-service Dockerfile'", out)

    def test_explicit_server_framework_check_fails_even_with_solidstart(self):
        base = self.commit({sc.BASELINE: ""})
        head = self.commit({"package.json": '{"dependencies": {"@solidjs/start": "1", "hono": "4"}}'})
        rc, out = self.check(base, head)
        self.assertEqual(rc, 1, out)
        self.assertIn("new departure from the default stack: 'ts-server package.json hono'", out)

    def test_model_pin_adoption_preserves_existing_stack_ratchet(self):
        with mock.patch.object(sc.model_pins, "load_aliases", return_value=ModelPinTest.ALIASES):
            base = self.commit({"src/client.ts": 'call("gpt-5.1-2025-11-13");\n',
                                sc.BASELINE: "# existing stack baseline\n",
                                '.github/workflows/stack.yml': 'uses: SylphxAI/.github/.github/actions/stack-conformance@bc664ac7410c00d233fe4f729b6747969a4a9bed # main\n'})
            rows = sc.model_pins.scan(sc.Tree(self.repo, base), "", ModelPinTest.ALIASES,
                                      sc.architecture.source_views)["findings"]
            baseline = sc.model_pins.BASELINE_MARKER + "\n" + "\n".join(sc.model_pins.baseline_key(r) for r in rows) + "\n"
            adopted = self.commit({sc.BASELINE: baseline})
            self.assertEqual(self.check(base, adopted)[0], 0)
            same = self.commit({"README.md": "documentation\n"})
            self.assertEqual(self.check(adopted, same)[0], 0)
            self.assertEqual(self.check("", same)[0], 0)
            new = self.commit({"src/other.ts": 'call("claude-opus-4-5-20251101");\n'})
            rc, output = self.check(same, new)
            self.assertEqual(rc, 1)
            self.assertIn("catalog alias", output)
            grown = self.commit({sc.BASELINE: baseline + "model-pin src/other.ts claude-opus-4-5-20251101 1\n"})
            self.assertEqual(self.check(same, grown)[0], 1)

    def test_model_pin_relocation_is_not_adoption(self):
        old = "apps/web/atlas/migrations/initial seed.sql"
        new = "db/migrations/initial seed.sql"
        sql = "INSERT INTO config (model) VALUES ('openai/gpt-4o-mini');\n"
        marker = sc.model_pins.BASELINE_MARKER + "\n"
        old_record = "model-pin apps/web/atlas/migrations/initial%20seed.sql openai/gpt-4o-mini 1\n"
        new_record = "model-pin db/migrations/initial%20seed.sql openai/gpt-4o-mini 1\n"
        # No shared caller pin: treating a move as adoption would refuse it.
        base = self.commit({old: sql, sc.BASELINE: marker + old_record})
        self.git("rm", old)
        head = self.commit({new: sql, sc.BASELINE: marker + new_record})
        self.assertEqual(self.git("rev-parse", f"{base}:{old}"), self.git("rev-parse", f"{head}:{new}"))
        rc, output = self.check(base, head)
        self.assertEqual(rc, 0, output)

    def test_model_pin_relocation_refuses_unproven_records(self):
        old, new, copy = "src/old.ts", "src/new.ts", "src/copy.ts"
        model, other = "openai/gpt-4o-mini", "anthropic/claude-opus-4-5-20251101"
        source = f'call("{model}");\n'
        marker = sc.model_pins.BASELINE_MARKER + "\n"

        def record(path, model_id=model, occurrence=1):
            return f"model-pin {path} {model_id} {occurrence}\n"

        base = self.commit({old: source, sc.BASELINE: marker + record(old)})
        for case, keep_file, keep_record, extra, contents, new_record in (
            ("old file kept", True, False, {}, source, record(new)),
            ("old record kept", False, True, {}, source, record(new)),
            ("edited blob", False, False, {}, source + "// edit\n", record(new)),
            ("increased count", False, False, {}, source * 2, record(new) + record(new, occurrence=2)),
            ("changed model", False, False, {}, source.replace(model, other), record(new, other)),
            ("two destinations", False, False, {copy: source}, source, record(new) + record(copy)),
        ):
            with self.subTest(case=case):
                self.git("checkout", "--quiet", "--detach", base)
                if not keep_file:
                    self.git("rm", old)
                baseline = marker + (record(old) if keep_record else "") + new_record
                head = self.commit({new: contents, sc.BASELINE: baseline, **extra})
                rc, output = self.check(base, head)
                self.assertEqual(rc, 1, output)
                self.assertIn("model-pin baseline cannot grow or seed new debt: " + new_record.splitlines()[0],
                              output)

        self.git("checkout", "--quiet", "--detach", base)
        unrecorded = self.commit({sc.BASELINE: marker})
        self.git("rm", old)
        head = self.commit({new: source, sc.BASELINE: marker + record(new)})
        rc, output = self.check(unrecorded, head)
        self.assertEqual(rc, 1, output)
        self.assertIn("model-pin baseline cannot grow or seed new debt: " + record(new).strip(), output)

    def test_legacy_encoded_source_still_checks_ascii_model_ids(self):
        path = pathlib.Path(self.repo, "client.cs")
        path.write_bytes(b'// Legacy text: \x96\nconst string Model = "gpt-5.1-2025-11-13";\n')
        self.git("add", "client.cs")
        self.git("commit", "-qm", "legacy source")
        with sc.Tree(self.repo, "HEAD") as tree:
            self.assertIsNone(tree.read("client.cs"))
            report = sc.model_pins.scan(tree, "example/app", None, sc.architecture.source_views)
        self.assertEqual(report["unreadable"], [])
        self.assertEqual(len(report["findings"]), 1)

    def test_retired_sql_and_keel_name_checks_do_not_block(self):
        base = self.commit({"README.md": "base"})
        head = self.commit({"Cargo.toml": KeelGameGuardTest.MANIFEST,
                            "src/shop.rs": "struct CoinSku;\n",
                            "src/art.rs": "fn cloud_pixels() -> Vec<u8> { vec![] }\n",
                            "docs/BUILDING.md": "# Build\n",
                            "scripts/measure-rust-debug.py": "pass\n",
                            "adapter/package.json": '{"name":"egress-guard"}',
                            "migrations/01.sql": "CREATE TABLE tasks (status text, assignee uuid, due_at timestamptz);\n"
                            + "\n".join(f"CREATE TABLE {table} (id uuid);" for table in
                                        ("entities", "relations", "contacts", "crm_accounts", "kg_nodes", "session_turns"))})
        rc, out = self.check(base, head)
        self.assertEqual(rc, 0, out)

    def test_fix_with_stale_record_passes_but_cannot_authorize_reintroduction(self):
        api = "api/package.json"
        legacy = '{"dependencies": {"hono": "4"}}'
        base = self.commit({api: legacy, sc.BASELINE: "ts-server api/package.json hono\n"})
        fixed = self.commit({api: "{}"})
        rc, out = self.check(base, fixed)
        self.assertEqual(rc, 0, out)
        self.assertIn("::notice::stale baseline line:", out)
        self.assertEqual(self.check(fixed, fixed)[0], 0)
        restored = self.commit({api: legacy})
        rc, out = self.check(fixed, restored)
        self.assertEqual(rc, 1, out)
        self.assertIn("new departure", out)

    def test_all_counts_clean_checkouts(self):
        self.commit({"sylphx.toml": TOML.format(engine="atlas", dockerfile="none")})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            sc.main(["all", self.repo])
        self.assertIn("Products fully on our own stack (stack conformance): 1 of 1 (100%)", out.getvalue())

    def test_sharded_baseline_dir_migration_and_ratchet(self):
        """A repository can shard .github/stack-departures.txt into
        .github/stack-departures.d/*.txt while preserving the strict
        no-growth, no-stale, no-duplicate ratchet."""
        self.assertTrue(sc.blob_candidate(".github/stack-departures.d/crates.txt"))
        self.assertEqual(sc.baseline_shard_dir(".github/stack-departures.txt"), ".github/stack-departures.d")

        dep_a = "migrations apps/a/sylphx.toml drizzle"
        dep_b = "migrations apps/b/sylphx.toml prisma"
        base = self.commit({
            "apps/a/sylphx.toml": TOML.format(engine="drizzle", dockerfile="none"),
            "apps/b/sylphx.toml": TOML.format(engine="prisma", dockerfile="none"),
            sc.BASELINE: f"# header\n{dep_a}\n{dep_b}\n",
        })
        self.assertEqual(self.check(base, base)[0], 0)

        # Clean migration from monolithic baseline to sharded .d/*.txt passes.
        self.git("rm", sc.BASELINE)
        sharded = self.commit({
            ".github/stack-departures.d/00-headers.txt": "# header\n",
            ".github/stack-departures.d/apps-a.txt": dep_a + "\n",
            ".github/stack-departures.d/apps-b.txt": dep_b + "\n",
        })
        rc, out = self.check(base, sharded)
        self.assertEqual(rc, 0, out)

        # Duplicate record across two different shards is rejected.
        dup = self.commit({".github/stack-departures.d/apps-b.txt": dep_b + "\n" + dep_a + "\n"})
        rc, out = self.check(sharded, dup)
        self.assertEqual(rc, 1, out)
        self.assertIn("duplicate baseline record", out)

        # Smuggling a new departure into a shard is rejected.
        grown = self.commit({
            ".github/stack-departures.d/apps-b.txt": dep_b + "\n",
            "apps/c/sylphx.toml": TOML.format(engine="prisma", dockerfile="none"),
            ".github/stack-departures.d/apps-c.txt": "migrations apps/c/sylphx.toml prisma\n",
        })
        rc, out = self.check(sharded, grown)
        self.assertEqual(rc, 1, out)
        self.assertIn("baseline grew", out)

        # Fixing one departure and deleting its shard passes cleanly.
        self.git("rm", "apps/c/sylphx.toml", ".github/stack-departures.d/apps-c.txt",
                 ".github/stack-departures.d/apps-b.txt")
        shrunk = self.commit({"apps/b/sylphx.toml": TOML.format(engine="atlas", dockerfile="none")})
        rc, out = self.check(sharded, shrunk)
        self.assertEqual(rc, 0, out)


if __name__ == "__main__":
    unittest.main()
