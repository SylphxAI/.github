#!/usr/bin/env python3
"""Tests for the identifiers action: it flags non-v7 ids on added lines only."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import tempfile
import unittest

import yaml

ACTION = pathlib.Path(__file__).resolve().parents[1] / ".github" / "actions" / "identifiers"


def run_check(
    before: str, after: str, path: str = "schema.sql", *specs: str, mode: str = "fail",
    repository: str = "example/repo"
) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as tmp:
        def git(*args: str) -> None:
            subprocess.run(["git", *args], cwd=tmp, check=True, capture_output=True)

        git("init", "-q")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "t")
        doc = pathlib.Path(tmp, path)
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text(before)
        git("add", ".")
        git("commit", "-qm", "base")
        doc.write_text(after)
        git("commit", "-qam", "change")
        return subprocess.run(
            [str(ACTION / "check.sh"), "HEAD~1..HEAD", str(ACTION / "rules.tsv"), *specs],
            cwd=tmp, capture_output=True, text=True, env={**os.environ, "IDENTIFIERS_MODE": mode,
                                                        "CHECK_REPOSITORY": repository},
        )


class IdentifiersTest(unittest.TestCase):
    def test_sql_uuid_v4_generators_are_flagged(self) -> None:
        result = run_check("", "id uuid primary key default gen_random_uuid(),\n"
                               "other uuid default uuid_generate_v4(),\n"
                               "third uuid default uuid_generate_v1()\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("file=schema.sql,line=1::identifiers: gen_random_uuid()", result.stdout)
        self.assertIn("file=schema.sql,line=2::identifiers: uuid_generate_v4()", result.stdout)
        self.assertIn("file=schema.sql,line=3::identifiers: uuid_generate_v1()", result.stdout)
        self.assertIn("uuidv7() - see owner standards/identifiers.md", result.stdout)

    def test_sql_text_primary_key_is_flagged(self) -> None:
        result = run_check("", "id TEXT NOT NULL PRIMARY KEY,\nsession_id text primary key,\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("file=schema.sql,line=1::identifiers: a text primary key", result.stdout)
        self.assertIn("file=schema.sql,line=2::identifiers: a text primary key", result.stdout)

    def test_sql_varchar_and_citext_primary_keys_are_flagged(self) -> None:
        result = run_check("", "code varchar(36) primary key,\nhandle citext not null primary key\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("line=1::identifiers: a text primary key", result.stdout)
        self.assertIn("line=2::identifiers: a text primary key", result.stdout)

    def test_sql_serial_primary_keys_are_flagged(self) -> None:
        result = run_check("", "id serial primary key,\nother bigserial primary key\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("line=1::identifiers: a serial primary key", result.stdout)
        self.assertIn("line=2::identifiers: a serial primary key", result.stdout)

    def test_sql_rules_are_case_insensitive(self) -> None:
        result = run_check("", "ALTER TABLE t ALTER COLUMN id SET DEFAULT UUID_GENERATE_V4();\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("uuid_generate_v4()", result.stdout)

    def test_sql_v7_uuid_and_plain_text_columns_pass(self) -> None:
        result = run_check("", "create table t (id uuid primary key default uuidv7(),\n"
                               "  tenant_id uuid not null,\n"
                               "  note text not null, id uuid primary key default uuidv7());\n")
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_rust_non_v7_generators_are_flagged(self) -> None:
        result = run_check("", "let a = Uuid::new_v4();\nlet b = ulid::Ulid::new();\n"
                               "let c = nanoid::nanoid!();\nlet d = cuid();\nlet e = ksuid::Ksuid::new();\n",
                           "src/ids.rs")
        self.assertEqual(result.returncode, 1)
        for line, rule in [(1, "Uuid::new_v4"), (2, "ULID"), (3, "nanoid"), (4, "CUID"), (5, "KSUID")]:
            self.assertIn(f"file=src/ids.rs,line={line}::identifiers: {rule}", result.stdout)

    def test_rust_v7_passes(self) -> None:
        result = run_check("", "let id = Uuid::now_v7();\n", "src/ids.rs")
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_typescript_non_v7_generators_are_flagged(self) -> None:
        result = run_check("", "const a = randomUUID();\nimport { nanoid } from \"nanoid\";\n"
                               "import { ulid } from 'ulid';\nconst b = uuidv4();\n"
                               "const c = uuid.v4();\nimport { v4 as uuid } from 'uuid';\nconst d = cuid();\n",
                           "src/ids.ts")
        self.assertEqual(result.returncode, 1)
        for line in range(1, 8):
            self.assertIn(f"file=src/ids.ts,line={line}::identifiers: ", result.stdout)

    def test_typescript_js_and_tsx_are_covered(self) -> None:
        for path in ("src/ids.mjs", "src/ids.js", "src/ids.tsx"):
            result = run_check("", "const id = randomUUID();\n", path)
            self.assertEqual(result.returncode, 1, path)
            self.assertIn(f"file={path},line=1::", result.stdout)

    def test_typescript_v7_passes(self) -> None:
        result = run_check("", "import { uuidv7 } from '@sylphx/ids';\nconst id = uuidv7();\n", "src/ids.ts")
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_python_non_v7_generators_are_flagged(self) -> None:
        result = run_check("", "a = uuid4()\nb = uuid1()\n", "app/ids.py")
        self.assertEqual(result.returncode, 1)
        self.assertIn("file=app/ids.py,line=1::identifiers: uuid4()", result.stdout)
        self.assertIn("file=app/ids.py,line=2::identifiers: uuid1()", result.stdout)

    def test_python_v7_passes(self) -> None:
        result = run_check("", "a = uuid7()\n", "app/ids.py")
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_go_non_v7_generators_are_flagged(self) -> None:
        result = run_check("", "a := uuid.New()\nb := uuid.NewString()\nc := ulid.Make()\n", "ids/ids.go")
        self.assertEqual(result.returncode, 1)
        self.assertIn("file=ids/ids.go,line=1::identifiers: uuid.New()", result.stdout)
        self.assertIn("file=ids/ids.go,line=2::identifiers: uuid.NewString()", result.stdout)
        self.assertIn("file=ids/ids.go,line=3::identifiers: ULID", result.stdout)

    def test_go_v7_passes(self) -> None:
        result = run_check("", "a := uuid.NewV7()\n", "ids/ids.go")
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_allow_marker_skips_the_line(self) -> None:
        result = run_check("", "id text primary key -- identifiers: allow a legacy table we do not own\n")
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_existing_code_does_not_block(self) -> None:
        result = run_check("id text primary key\n", "id text primary key\nnote text\n")
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_rules_are_case_sensitive_outside_sql(self) -> None:
        result = run_check("", "const a = RANDOMUUID();\nconst b = NANOID();\n", "src/ids.ts")
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_files_without_a_rule_set_are_not_checked(self) -> None:
        result = run_check("", "run gen_random_uuid() by hand\n", "notes.md")
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_excluded_paths_are_skipped(self) -> None:
        result = run_check("", "id text primary key\n", "schema.sql", ":(exclude)schema.sql")
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_warn_mode_annotates_and_passes(self) -> None:
        result = run_check("", "id text primary key\n", mode="warn")
        self.assertEqual(result.returncode, 0)
        self.assertIn("::warning file=schema.sql,line=1::", result.stdout)


MIGRATION_PATH = "services/api/migrations/20261002000000_erasure_requests.sql"
MIGRATION = (pathlib.Path(__file__).parent / "fixtures" / "identifiers" /
             "20261002000000_erasure_requests.sql").read_bytes()
MIGRATION_SHA256 = "ec45154ae1d30614b4909e0da9d2d5f74ecd6695de3f0e0d09c761568b4f54d2"


class HistoricalAllowanceTest(unittest.TestCase):
    def test_fixture_identity(self) -> None:
        self.assertEqual(hashlib.sha256(MIGRATION).hexdigest(), MIGRATION_SHA256)
        blob = hashlib.sha1(f"blob {len(MIGRATION)}\0".encode() + MIGRATION).hexdigest()
        self.assertEqual(blob, "98bc238255f41ee91e0af595c545f1a76ef0d83f")
        allowances = json.loads((ACTION / "historical-allowances.json").read_text())
        self.assertEqual(allowances, [{"repository": "SylphxAI/tachyn", "path": MIGRATION_PATH,
                                      "code": "sql.text-primary-key", "sha256": MIGRATION_SHA256}])

    def test_rules_have_unique_stable_codes(self) -> None:
        rows = [line.split("\t") for line in (ACTION / "rules.tsv").read_text().splitlines()
                if line and not line.startswith("#")]
        self.assertTrue(all(len(row) == 4 and row[3] for row in rows))
        self.assertEqual(len(rows), len({row[3] for row in rows}))

    def invoke(self, event: str, *, data: bytes = MIGRATION,
               repository: str = "SylphxAI/tachyn", path: str = MIGRATION_PATH,
               caller_allowance: bool = False, other_code: bool = False,
               dirty: bytes | None = None) -> subprocess.CompletedProcess[str]:
        """Run the actual composite shell step in an independent consumer repo."""
        manifest = yaml.safe_load((ACTION / "action.yml").read_text())
        step = manifest["runs"]["steps"][0]
        self.assertEqual(step["env"]["CHECK_REPOSITORY"], "${{ github.repository }}")
        self.assertEqual(set(manifest["inputs"]), {"mode", "exclude"})
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            def git(*args: str) -> str:
                return subprocess.run(["git", "-C", tmp, *args], check=True,
                                      capture_output=True, text=True).stdout.strip()
            git("init", "-q", "-b", "main")
            git("config", "user.email", "test@invalid")
            git("config", "user.name", "test")
            git("commit", "-qm", "base", "--allow-empty")
            base = git("rev-parse", "HEAD")
            git("switch", "-qc", "change")
            doc = root / path
            doc.parent.mkdir(parents=True)
            doc.write_bytes(data)
            fake_list = [{"repository": repository, "path": path,
                          "code": "sql.text-primary-key", "sha256": hashlib.sha256(data).hexdigest()}]
            if caller_allowance:
                # Attempt both a conventional caller-side file and a shadow
                # action data file as committed PR/merge/push content.
                for name in ("historical-allowances.json",
                             ".github/actions/identifiers/historical-allowances.json"):
                    target = root / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(json.dumps(fake_list))
            git("add", ".")
            git("commit", "-qm", "change")
            head = git("rev-parse", "HEAD")
            git("remote", "add", "origin", tmp)
            if dirty is not None:
                doc.write_bytes(dirty)
            env = dict(os.environ, EVENT_NAME=event, CHECK_REPOSITORY=repository,
                       PR_BASE_SHA=base, PR_BASE_REF="main", PR_HEAD_SHA=head,
                       MG_BASE_SHA=base, MG_HEAD_SHA=head, PUSH_BEFORE=base, GIT_SHA=head,
                       MODE="fail", EXCLUDE="", GITHUB_ACTION_PATH=str(ACTION),
                       INPUT_ALLOWANCES=json.dumps(fake_list),
                       IDENTIFIERS_ALLOWANCES=str(root / "historical-allowances.json"),
                       ALLOWANCES=str(root / "historical-allowances.json"))
            command = ["bash", "-c", step["run"]]
            if other_code:
                rules = root / "other-rules.tsv"
                rules.write_text((ACTION / "rules.tsv").read_text().replace(
                    "sql.text-primary-key", "sql.other-finding"))
                command = [str(ACTION / "check.sh"), f"{base}..{head}", str(rules)]
            return subprocess.run(command, cwd=tmp, env=env, capture_output=True, text=True)

    def test_exact_historical_file_passes_all_events(self) -> None:
        for event in ("pull_request", "merge_group", "push"):
            with self.subTest(event=event):
                result = self.invoke(event)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("::warning", result.stdout)
                self.assertNotIn("::error", result.stdout)

    def test_each_mismatch_fails_all_events_despite_caller_allowances(self) -> None:
        cases = [dict(data=MIGRATION + b"\n"),  # exactly one added byte
                 dict(repository="SylphxAI/another-repo"),
                 dict(path="other/20261002000000_erasure_requests.sql"),
                 dict(path="services/api/migrations/20261003000000_erasure_requests.sql")]
        for event in ("pull_request", "merge_group", "push"):
            for case in cases:
                with self.subTest(event=event, case=case):
                    result = self.invoke(event, caller_allowance=True, **case)
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertIn("::error", result.stdout)
                    self.assertIn("[sql.text-primary-key]", result.stdout)

    def test_other_finding_code_is_not_allowed(self) -> None:
        result = self.invoke("push", other_code=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("[sql.other-finding]", result.stdout)

    def test_hash_uses_committed_endpoint_not_working_tree(self) -> None:
        result = self.invoke("push", data=MIGRATION + b"\n", dirty=MIGRATION)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        result = self.invoke("push", dirty=MIGRATION + b"\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
