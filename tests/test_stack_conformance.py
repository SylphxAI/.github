#!/usr/bin/env python3
"""Tests for the stack-conformance ratchet."""

from __future__ import annotations

import contextlib
import datetime
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

    def test_non_atlas_engine_is_a_departure(self):
        t = sc.DictTree({"sylphx.toml": TOML.format(engine="drizzle", dockerfile="none")})
        self.assertEqual(sc.departures(t), ["migrations sylphx.toml drizzle"])

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

    def test_a_stale_line_fails_so_the_baseline_shrinks(self):
        errors = sc.decide(self.FOUND[:1], set(self.FOUND), set(self.FOUND))
        self.assertEqual(len(errors), 1)
        self.assertIn("stale baseline line", errors[0])

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

    def test_engine_commerce_and_shader_consumers_pass(self):
        tree = self.tree(**{"crates/game/src/shop.rs": (
            "use keel::commerce::iap::Sku;\n"
            "use keel_commerce::Catalog;\nuse cubeage_kit::commerce;\n"
            "fn shop(sku: Sku) {}\nfn shader() { keel::render::draw(); }\n")})
        self.assertEqual(sc.keel_departures(tree), [])

    def test_local_sku_structs_fail_even_if_engine_commerce_is_also_imported(self):
        tree = self.tree(**{"src/shop.rs": (
            "use keel_commerce::Sku;\npub struct CoinSku { amount: u32 }\n"
            "struct\nPremiumSku;\nstruct SkuId;\n")})
        found = sc.keel_departures(tree)
        self.assertEqual(found, ["keel-local-sku src/shop.rs:2 CoinSku",
                                 "keel-local-sku src/shop.rs:3 PremiumSku"])
        self.assertIn("keel_commerce", sc.decide_keel(found)[0])

    def test_pixel_vector_functions_fail_with_multiline_arguments_and_types(self):
        tree = self.tree(**{"crates/game/src/textures.rs": (
            "pub fn cloud_pixels(\nwidth: u32, height: u32,\n) -> Vec < u8 > { vec![] }\n"
            "fn sun_pixels() -> std::vec::Vec<u8> { vec![] }\n"
            "fn read_pixels() -> Vec<f32> { vec![] }\n")})
        found = sc.keel_departures(tree)
        self.assertEqual(len(found), 2)
        self.assertTrue(all(line.startswith("keel-cpu-pixels") for line in found))

    def test_comments_literals_and_dependency_build_outputs_are_not_code(self):
        bad = "struct CoinSku; fn cloud_pixels() -> Vec<u8> {}"
        tree = self.tree(**{"src/game.rs": (
            f"// {bad}\n/* nested /* comment */ {bad} */\n"
            f'const S: &str = r##"{bad}"##;\nconst T: &str = "{bad}";\n'),
            "target/generated/src/game.rs": bad, "vendor/src/game.rs": bad})
        self.assertEqual(sc.keel_departures(tree), [])

    def test_boilerplate_files_fail_only_for_keel_consumers(self):
        files = {"docs/BUILDING.md": "# Build", "scripts/measure-rust-debug.py": "pass"}
        self.assertEqual(sc.keel_departures(self.tree(**files)), [
            "keel-boilerplate docs/BUILDING.md", "keel-boilerplate scripts/measure-rust-debug.py"])
        self.assertEqual(sc.keel_departures(sc.DictTree(files)), [])
        self.assertEqual(sc.keel_departures(self.tree(**{"docs/build.md": "# Build"})), [])

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


class AgentRuntimeTest(unittest.TestCase):
    POLICY = {
        "owner_repos": ["SylphxAI/cloud"],
        "baseline": [{"repo": "o/app", "departure": "agent-runtime-package crates/eg/Cargo.toml egress-guard",
                      "expires": "2026-11-30"}],
    }
    TODAY = datetime.date(2026, 10, 6)

    def test_packages_by_name(self):
        t = sc.DictTree({
            "crates/eg/Cargo.toml": '[package]\nname = "egress-guard"\n',
            "crates/vp/Cargo.toml": '[package]\nname = "spiron_vault_proxy"\n[lib]\nname = "x"\n',
            "crates/ok/Cargo.toml": '[package]\nname = "vault-client"\n',
            "Cargo.toml": '[workspace]\nmembers = ["crates/*"]\n[workspace.dependencies]\negress-guard = "1"\n',
            "packages/h/package.json": '{"name": "@acme/agent-harness"}',
            "packages/c/package.json": '{"name": "credential-crypto-lite"}',
            "node_modules/x/package.json": '{"name": "egress-guard"}',
        })
        self.assertEqual(sc.agent_runtime(t), [
            "agent-runtime-package crates/eg/Cargo.toml egress-guard",
            "agent-runtime-package crates/vp/Cargo.toml spiron_vault_proxy",
            "agent-runtime-package packages/c/package.json credential-crypto-lite",
            "agent-runtime-package packages/h/package.json @acme/agent-harness",
        ])

    def test_line_reader_finds_the_package_name(self):
        self.assertEqual(sc.parse_toml_lines('[package]\nname = "egress-guard"\n')["package"],
                         {"name": "egress-guard"})

    def test_tables_created_and_not_dropped(self):
        t = sc.DictTree({
            "db/migrations/001.sql": (
                "CREATE TABLE public.session_entries (id uuid);\n"
                'create table if not exists "tool_calls" (id uuid);\n'
                "-- CREATE TABLE memory_entries (id uuid);\n"
                "/* CREATE TABLE session_turns (id uuid); */\n"
                "CREATE UNLOGGED TABLE agent_memory_items (id uuid);\n"
                "CREATE TABLE tool_calls_archive (id uuid);\n"
                "CREATE TABLE memory_entries_v2 (id uuid);\n"),
            "db/migrations/002.sql": "DROP TABLE IF EXISTS foo, public.tool_calls CASCADE;\n",
            "db/schema/memory.sql": "CREATE TABLE agent_memory (id uuid);\nDROP TABLE agent_memory;\n",
        })
        self.assertEqual(sc.agent_runtime(t), [
            "agent-runtime-table db/migrations/001.sql agent_memory_items",
            "agent-runtime-table db/migrations/001.sql session_entries",
        ])

    def test_a_new_part_fails(self):
        errors, _ = sc.decide_agent_runtime(
            ["agent-runtime-table m/1.sql tool_calls"], "o/app", self.POLICY, self.TODAY)
        self.assertEqual(len(errors), 1)
        self.assertIn("new agent-runtime part", errors[0])

    def test_an_allowed_part_passes_until_it_expires(self):
        found = ["agent-runtime-package crates/eg/Cargo.toml egress-guard"]
        self.assertEqual(sc.decide_agent_runtime(found, "o/app", self.POLICY, self.TODAY), ([], []))
        self.assertEqual(sc.decide_agent_runtime(found, "o/app", self.POLICY, datetime.date(2026, 11, 30)),
                         ([], []))
        errors, _ = sc.decide_agent_runtime(found, "o/app", self.POLICY, datetime.date(2026, 12, 1))
        self.assertIn("expired on 2026-11-30", errors[0])

    def test_an_allowance_is_per_repository(self):
        found = ["agent-runtime-package crates/eg/Cargo.toml egress-guard"]
        errors, _ = sc.decide_agent_runtime(found, "o/other", self.POLICY, self.TODAY)
        self.assertEqual(len(errors), 1)

    def test_the_platform_owner_is_not_checked(self):
        found = ["agent-runtime-table services/agents/m/1.sql session_entries"]
        self.assertEqual(sc.decide_agent_runtime(found, "SylphxAI/cloud", self.POLICY, self.TODAY), ([], []))

    def test_a_gone_part_is_a_notice(self):
        errors, notices = sc.decide_agent_runtime([], "o/app", self.POLICY, self.TODAY)
        self.assertEqual(errors, [])
        self.assertIn("no longer found", notices[0])


class AgentRuntimePolicyTest(unittest.TestCase):
    """The shipped policy file is well formed."""

    def test_policy_file(self):
        policy = json.loads(sc.AGENT_RUNTIME_POLICY.read_text())
        self.assertEqual(policy["owner_repos"], ["SylphxAI/cloud"])
        seen = set()
        for entry in policy["baseline"]:
            self.assertRegex(entry["repo"], r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
            self.assertRegex(entry["departure"], r"^agent-runtime-(package|table) \S+ \S+$")
            datetime.date.fromisoformat(entry["expires"])
            key = (entry["repo"], entry["departure"])
            self.assertNotIn(key, seen)
            seen.add(key)

    def test_spiron_relocated_session_schema_keeps_the_original_expiry_and_scope(self):
        policy = json.loads(sc.AGENT_RUNTIME_POLICY.read_text())
        path = "crates/spiron-core/src/session/local_schema.sql"
        found = sc.agent_runtime(sc.DictTree({path:
            "CREATE TABLE session_entries (id TEXT);\nCREATE TABLE session_turns (id TEXT);\n"}))
        expected = {f"agent-runtime-table {path} {table}"
                    for table in ("session_entries", "session_turns")}
        self.assertEqual(set(found), expected)
        admitted = {entry["departure"]: entry["expires"] for entry in policy["baseline"]
                    if entry["repo"] == "SylphxAI/spiron" and entry["departure"] in expected}
        self.assertEqual(admitted, dict.fromkeys(expected, "2026-11-30"))
        self.assertFalse(any("20260306000000_initial.sql session_entries" in entry["departure"]
                             or "20260722234500_session_turn_settlement_authority.sql session_turns"
                             in entry["departure"] for entry in policy["baseline"]))
        errors, _ = sc.decide_agent_runtime(found, "SylphxAI/spiron", policy, datetime.date(2026, 11, 30))
        self.assertEqual(errors, [])
        errors, _ = sc.decide_agent_runtime(found, "SylphxAI/spiron", policy, datetime.date(2026, 12, 1))
        self.assertEqual(len(errors), 2)
        self.assertTrue(all("expired on 2026-11-30" in error for error in errors))
        for repo, findings in (("example/other", found),
                               ("SylphxAI/spiron", [line.replace(path, "src/copied.sql") for line in found]),
                               ("SylphxAI/spiron", [f"agent-runtime-table {path} tool_calls"])):
            errors, _ = sc.decide_agent_runtime(findings, repo, policy, datetime.date(2026, 10, 30))
            self.assertEqual(len(errors), len(findings))
            self.assertTrue(all("new agent-runtime part" in error for error in errors))


KNOWLEDGE_SQL = "CREATE TABLE knowledge_entities (id uuid PRIMARY KEY, title text);\n"


class KnowledgeTableGuardTest(unittest.TestCase):
    """test knowledge-table-guard: a new knowledge or CRM table fails, because
    the company's graph and customer records are Sylphx Knowledge
    (SylphxAI/cloud ADR-01M4AJK2TXTJWZ4X5B06HPT0R4, D6)."""

    def test_the_named_families_are_found(self):
        cases = {
            "CREATE TABLE kg_nodes (id uuid);": ["kg_nodes"],
            "CREATE TABLE knowledge_edges (id uuid);": ["knowledge_edges"],
            'create table if not exists "crm_accounts" (id uuid);': ["crm_accounts"],
            "CREATE TABLE entities (id uuid);": ["entities"],
            "CREATE TABLE relations (id uuid);": ["relations"],
            "CREATE TABLE contacts (id uuid);": ["contacts"],
            "CREATE TABLE customer_profiles (id uuid);": ["customer_profiles"],
            "CREATE UNLOGGED TABLE public.kg_edges (id uuid);": ["kg_edges"],
        }
        for sql, tables in cases.items():
            with self.subTest(sql=sql):
                found = sc.knowledge_tables("m/1.sql", sql)
                self.assertEqual(found, [f"knowledge-table m/1.sql {t}" for t in tables])

    def test_everyday_tables_are_not_found(self):
        for sql in ("CREATE TABLE items (id uuid, status text);",
                    "CREATE TABLE crmbridge (id uuid);",
                    "CREATE TABLE knowledge (id uuid);",
                    "CREATE TABLE contactless (id uuid);",
                    "CREATE TABLE bot_entities (id uuid);"):
            with self.subTest(sql=sql):
                self.assertEqual(sc.knowledge_tables("m/1.sql", sql), [])

    def test_a_new_table_fails_and_a_recorded_one_passes(self):
        policy = {"owner_repos": ["SylphxAI/cloud"], "baseline": [
            {"repo": "SylphxAI/spiron", "table": "knowledge_entries", "expires": "2026-11-30"}]}
        today = datetime.date(2026, 10, 7)
        errors, _ = sc.decide_knowledge(
            ["knowledge-table m/1.sql knowledge_entries", "knowledge-table m/1.sql crm_accounts"],
            "SylphxAI/spiron", policy, today)
        self.assertEqual(len(errors), 1)
        self.assertIn("crm_accounts", errors[0])
        self.assertIn("new knowledge or CRM table", errors[0])

    def test_an_allowance_expires(self):
        policy = {"owner_repos": [], "baseline": [
            {"repo": "o/app", "table": "knowledge_entries", "expires": "2026-11-30"}]}
        found = ["knowledge-table m/1.sql knowledge_entries"]
        self.assertEqual(sc.decide_knowledge(found, "o/app", policy, datetime.date(2026, 10, 7)), ([], []))
        errors, _ = sc.decide_knowledge(found, "o/app", policy, datetime.date(2026, 12, 1))
        self.assertIn("expired on 2026-11-30", errors[0])

    def test_an_allowance_is_per_repository(self):
        policy = {"owner_repos": [], "baseline": [
            {"repo": "o/app", "table": "knowledge_entries", "expires": "2026-11-30"}]}
        errors, _ = sc.decide_knowledge(
            ["knowledge-table m/1.sql knowledge_entries"], "o/other", policy, datetime.date(2026, 10, 7))
        self.assertEqual(len(errors), 1)

    def test_the_platform_owner_is_not_checked(self):
        policy = {"owner_repos": ["SylphxAI/cloud"], "baseline": []}
        self.assertEqual(sc.decide_knowledge(
            ["knowledge-table m/1.sql knowledge_nodes"], "SylphxAI/cloud", policy, datetime.date(2026, 10, 7)),
            ([], []))

    def test_a_gone_table_is_a_notice(self):
        policy = {"owner_repos": [], "baseline": [
            {"repo": "o/app", "table": "knowledge_entries", "expires": "2026-11-30"}]}
        errors, notices = sc.decide_knowledge([], "o/app", policy, datetime.date(2026, 10, 7),
                                              present=set())
        self.assertEqual(errors, [])
        self.assertIn("no longer found", notices[0])

    def test_a_table_still_on_the_tree_is_not_a_notice(self):
        policy = {"owner_repos": [], "baseline": [
            {"repo": "o/app", "table": "knowledge_entries", "expires": "2026-11-30"}]}
        errors, notices = sc.decide_knowledge([], "o/app", policy, datetime.date(2026, 10, 7),
                                              present={"knowledge_entries"})
        self.assertEqual((errors, notices), ([], []))

    def test_a_down_migration_is_not_checked(self):
        self.assertEqual(sc.knowledge_tables("m/1.down.sql", KNOWLEDGE_SQL), [])

    def test_create_table_as_is_not_checked(self):
        self.assertEqual(sc.knowledge_tables("m/1.sql", "CREATE TABLE knowledge_entries AS SELECT 1;"), [])


class KnowledgeTablePolicyTest(unittest.TestCase):
    """The shipped policy file is well formed."""

    def test_policy_file(self):
        policy = json.loads(sc.KNOWLEDGE_POLICY.read_text())
        self.assertEqual(policy["owner_repos"], ["SylphxAI/cloud"])
        seen = set()
        for entry in policy["baseline"]:
            self.assertRegex(entry["repo"], r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
            self.assertRegex(entry["table"], r"^[a-z_][a-z0-9_]*$")
            datetime.date.fromisoformat(entry["found"])
            datetime.date.fromisoformat(entry["expires"])
            key = (entry["repo"], entry["table"])
            self.assertNotIn(key, seen)
            seen.add(key)


FIXTURES = ROOT / "tests" / "fixtures" / "work-obligations"
AGENT_TASKS = (FIXTURES / "0073_agent_tasks.up.sql").read_text()
MIGRATION = "crates/api/migrations/0073_agent_tasks.up.sql"


class ObligationTableGuardTest(unittest.TestCase):
    """test obligation-table-guard: a new work-engine table must say whose
    records it holds (SylphxAI/work docs/adr/0010, Guard for the class)."""

    def test_the_fixture_without_a_header_fails(self):
        self.assertEqual(sc.obligation_tables(MIGRATION, AGENT_TASKS),
                         [f"work-obligation-table {MIGRATION} agent_tasks"])

    def test_a_work_resource_header_passes(self):
        text = "-- Work resource: items (workspace ozyrix, kind operate)\n" + AGENT_TASKS
        self.assertEqual(sc.obligation_tables(MIGRATION, text), [])

    def test_a_customer_records_header_passes(self):
        text = "-- Rows are tickets our customers own: customer records (Work ADR 0010 D1)\n" + AGENT_TASKS
        self.assertEqual(sc.obligation_tables(MIGRATION, text), [])
        block = "/*\n * Helpdesk tickets.\n * customer records\n * (Work ADR 0010 D1)\n */\n" + AGENT_TASKS
        self.assertEqual(sc.obligation_tables(MIGRATION, block), [])

    def test_a_header_after_the_first_statement_does_not_count(self):
        text = AGENT_TASKS + "\n-- Work resource: items\n"
        self.assertEqual(len(sc.obligation_tables(MIGRATION, text)), 1)

    def test_an_empty_work_resource_does_not_count(self):
        self.assertEqual(len(sc.obligation_tables(MIGRATION, "-- Work resource:\n" + AGENT_TASKS)), 1)

    def test_all_three_column_kinds_are_needed(self):
        sql = "CREATE TABLE t (id uuid PRIMARY KEY, {cols});"
        cases = {
            "status text, assignee_id uuid, due_at timestamptz": True,
            '"state" text, "executing_principal_id" text, "deadline" timestamptz': True,
            "stage text, owner_role text, sla_breach_at timestamptz": True,
            "status text, assigned_to uuid, escalation_level int": True,
            "status text, customer_id uuid, due_at timestamptz": False,  # an invoice
            "status text, owner_id uuid, expires_at timestamptz": False,  # a subscription
            "assignee_id uuid, due_at timestamptz, title text": False,
            "status text, role text, created_at timestamptz": False,
        }
        for cols, expected in cases.items():
            with self.subTest(cols=cols):
                self.assertEqual(bool(sc.obligation_tables("m.sql", sql.format(cols=cols))), expected)

    def test_constraints_and_nested_parentheses_are_not_columns(self):
        sql = ("CREATE TABLE IF NOT EXISTS app.t (id uuid, status text CHECK (status IN ('a', 'b')),\n"
               "  CONSTRAINT role_due CHECK (deadline > now()), PRIMARY KEY (id), UNIQUE (assignee));")
        self.assertEqual(sc.obligation_tables("m.sql", sql), [])

    def test_a_down_migration_is_not_checked(self):
        self.assertEqual(sc.obligation_tables("crates/api/migrations/0090_drop.down.sql", AGENT_TASKS), [])

    def test_create_table_as_is_not_checked(self):
        self.assertEqual(sc.obligation_tables("m.sql", "CREATE TABLE t AS SELECT * FROM agent_tasks;"), [])


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
                    "source": {"repository": "SylphxAI/cloud", "commit": "a" * 40,
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
        self.assertEqual(len(self.scan({"src/client.rs": 'const MODEL: &str = "gpt-5.1-2025-11-13";'}, "SylphxAI/cloud")), 1)

    def test_duplicates_and_new_versions_cannot_hide_in_a_baseline(self):
        first = self.scan({"src/client.ts": 'call("gpt-5.1-2025-11-13");'})
        recorded = {sc.model_pins.baseline_key(row) for row in first}
        self.assertEqual(sc.model_pins.decide(first, recorded, recorded, first, True, True), [])
        duplicate = self.scan({"src/client.ts": 'call("gpt-5.1-2025-11-13");\ncall("gpt-5.1-2025-11-13");'})
        self.assertTrue(sc.model_pins.decide(duplicate, recorded, recorded, first, True, True))
        grown = {sc.model_pins.baseline_key(row) for row in duplicate}
        self.assertTrue(any("cannot grow" in e for e in sc.model_pins.decide(duplicate, grown, recorded, first, True, True)))
        self.assertTrue(sc.model_pins.decide([], recorded, recorded, first, True, True))

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
                          "--shared-source", str(ROOT)])
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

    def test_agent_runtime_over_commits(self):
        policy = pathlib.Path(self.repo, "..", pathlib.Path(self.repo).name + "-policy.json").resolve()
        policy.write_text(json.dumps({"owner_repos": ["o/platform"], "baseline": [
            {"repo": "o/app", "departure": "agent-runtime-package eg/Cargo.toml egress-guard",
             "expires": "2026-11-30"}]}))
        self.addCleanup(policy.unlink)

        def check(base, head, repository="o/app", today="2026-10-06"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = sc.main(["check", "--repo", self.repo, "--base", base, "--head", head,
                              "--repository", repository, "--agent-runtime-policy", str(policy),
                              "--today", today])
            return rc, out.getvalue()

        base = self.commit({"eg/Cargo.toml": '[package]\nname = "egress-guard"\n'})
        same = self.commit({"README.md": "x\n"})
        self.assertEqual(check(base, same)[0], 0)
        self.assertEqual(check(base, same, today="2026-12-01")[0], 1)

        added = self.commit({"db/migrations/2.sql": "CREATE TABLE session_turns (id uuid);\n"})
        rc, out = check(same, added)
        self.assertEqual(rc, 1)
        self.assertIn("agent-runtime-table db/migrations/2.sql session_turns", out)
        self.assertEqual(check(same, added, repository="o/platform")[0], 0)

        recorded = self.commit({sc.BASELINE: "agent-runtime-table db/migrations/2.sql session_turns\n"})
        rc, out = check(added, recorded)
        self.assertEqual(rc, 1)
        self.assertIn("allowed only by", out)

    def test_keel_game_boundaries_fail_at_the_real_check_entrypoint(self):
        base = self.commit({"Cargo.toml": KeelGameGuardTest.MANIFEST,
                            "src/main.rs": "fn main() {}"})
        self.assertEqual(self.check(base, base)[0], 0)
        head = self.commit({"src/shop.rs": "struct CoinSku;\n",
                            "src/art.rs": "fn cloud_pixels() -> Vec<u8> { vec![] }\n",
                            "docs/BUILDING.md": "# Build\n",
                            "scripts/measure-rust-debug.py": "pass\n"})
        rc, output = self.check(base, head)
        self.assertEqual(rc, 1)
        for kind in ("keel-local-sku", "keel-cpu-pixels", "keel-boilerplate"):
            self.assertIn(kind, output)
        baseline = self.commit({sc.BASELINE: "\n".join(sc.keel_departures(sc.Tree(self.repo, head)))})
        self.assertEqual(self.check(head, baseline)[0], 1)
        self.commit({"src/shop.rs": "use keel_commerce::Sku;\n",
                     "src/art.rs": "fn shader() {}\n", sc.BASELINE: ""})
        self.git("rm", "docs/BUILDING.md", "scripts/measure-rust-debug.py")
        self.git("commit", "-qm", "remove boilerplate")
        self.assertEqual(self.check(base, self.git("rev-parse", "HEAD"))[0], 0)

    def test_obligation_table_guard_over_commits(self):
        """Only migrations added in the range are checked: a table that is
        already on the base (current main of an adopting repository) passes."""
        base = self.commit({MIGRATION: AGENT_TASKS})
        self.assertEqual(self.check(base, base)[0], 0)
        same = self.commit({"README.md": "x\n", MIGRATION: AGENT_TASKS + "-- edited\n"})
        self.assertEqual(self.check(base, same)[0], 0)

        new = "crates/api/migrations/0074_more_tasks.up.sql"
        added = self.commit({new: AGENT_TASKS.replace("agent_tasks", "more_tasks")})
        rc, out = self.check(same, added)
        self.assertEqual(rc, 1)
        self.assertIn(f"work-obligation-table {new} more_tasks", out)
        self.assertIn("docs/adr/0010", out)

        headed = self.commit({new: "-- Work resource: items\n" + AGENT_TASKS.replace("agent_tasks", "more_tasks")})
        self.assertEqual(self.check(same, headed)[0], 0)
        self.git("mv", MIGRATION, "crates/api/migrations/0073_renamed.up.sql")
        self.git("commit", "-q", "-m", "rename")
        self.assertEqual(self.check(headed, self.git("rev-parse", "HEAD"))[0], 0)

    def test_knowledge_table_guard_over_commits(self):
        """A fixture pull request adding a knowledge or CRM table fails; a
        table already on the base (a recorded instance) passes."""
        policy = pathlib.Path(self.repo, "..", pathlib.Path(self.repo).name + "-knowledge.json").resolve()
        policy.write_text(json.dumps({"owner_repos": ["o/platform"], "baseline": [
            {"repo": "o/app", "table": "knowledge_entries",
             "found": "2026-10-07", "expires": "2026-11-30"}]}))
        self.addCleanup(policy.unlink)

        def check(base, head, repository="o/app", today="2026-10-07"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = sc.main(["check", "--repo", self.repo, "--base", base, "--head", head,
                              "--repository", repository, "--knowledge-policy", str(policy),
                              "--today", today])
            return rc, out.getvalue()

        base = self.commit({"db/migrations/1.sql": "CREATE TABLE knowledge_entries (id uuid);\n"})
        self.assertEqual(check(base, base)[0], 0)

        added = self.commit({"db/migrations/2.sql": "CREATE TABLE crm_accounts (id uuid);\n"})
        rc, out = check(base, added)
        self.assertEqual(rc, 1)
        self.assertIn("knowledge-table db/migrations/2.sql crm_accounts", out)
        self.assertIn("ADR-01M4AJK2TXTJWZ4X5B06HPT0R4", out)
        self.assertEqual(check(base, added, repository="o/platform")[0], 0)

        allowed = self.commit({"db/migrations/3.sql": "CREATE TABLE knowledge_entries (id uuid);\n"})
        self.assertEqual(check(added, allowed)[0], 0)

    def test_all_counts_clean_checkouts(self):
        self.commit({"sylphx.toml": TOML.format(engine="atlas", dockerfile="none")})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            sc.main(["all", self.repo])
        self.assertIn("Products fully on our own stack (stack conformance): 1 of 1 (100%)", out.getvalue())


if __name__ == "__main__":
    unittest.main()
