#!/usr/bin/env python3
"""Tests for the stack-conformance ratchet."""

from __future__ import annotations

import contextlib
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

    def test_all_counts_clean_checkouts(self):
        self.commit({"sylphx.toml": TOML.format(engine="atlas", dockerfile="none")})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            sc.main(["all", self.repo])
        self.assertIn("Products fully on our own stack (stack conformance): 1 of 1 (100%)", out.getvalue())


if __name__ == "__main__":
    unittest.main()
