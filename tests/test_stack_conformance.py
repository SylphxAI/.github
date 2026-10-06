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
            rc = sc.main(["check", "--repo", self.repo, "--base", base, "--head", head])
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

    def test_all_counts_clean_checkouts(self):
        self.commit({"sylphx.toml": TOML.format(engine="atlas", dockerfile="none")})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            sc.main(["all", self.repo])
        self.assertIn("Products fully on our own stack (stack conformance): 1 of 1 (100%)", out.getvalue())


if __name__ == "__main__":
    unittest.main()
