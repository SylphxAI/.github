"""Git-backed checker reads must preserve reports without per-file processes."""

import datetime
import contextlib
import hashlib
import importlib.util
import json
import io
import os
import pathlib
import statistics
import subprocess
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "conformance_tree", ROOT / ".github/actions/stack-conformance/stack_conformance.py")
sc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sc)


class PerFileTree:
    """Independent reference: ask Git for each path, without batching/caching."""

    def __init__(self, repo, rev):
        self.repo, self.rev = repo, rev
        self._paths = sc.git(repo, "ls-tree", "-r", "-z", "--name-only", rev).split("\0")[:-1]

    def paths(self):
        return self._paths

    def read(self, path):
        result = subprocess.run(
            ["git", "-C", self.repo, "cat-file", "blob", f"{self.rev}:{path}"], capture_output=True)
        if result.returncode:
            return None
        try:
            return result.stdout.decode("utf-8")
        except UnicodeDecodeError:
            return None


class GitTreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = pathlib.Path(self.tmp.name)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "t")
        self.git("config", "user.email", "t@example.com")

    def git(self, *args):
        return sc.git(str(self.repo), *args).strip()

    def commit(self, files):
        for path, data in files.items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data if isinstance(data, bytes) else data.encode())
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        return self.git("rev-parse", "HEAD")

    def test_contents_paths_and_missing_objects_match_git(self):
        rev = self.commit({
            "empty": "", "binary": b"\xff\x00\xfe", "payload": "abc\n\x00def\n123 blob 5\n",
            "space name": "text", "tab\tname": "tab", "line\nname": "line",
            "colon:name": "colon", "-option": "option", "duplicate": "text",
            "large": "字\n" * 100_000,
        })
        (self.repo / "link").symlink_to("space name")
        self.git("add", "link")
        self.git("update-index", "--add", "--cacheinfo", f"160000,{rev},submodule")
        self.git("commit", "-qm", "links")
        reference = PerFileTree(str(self.repo), "HEAD")
        with sc.Tree(str(self.repo), "HEAD") as tree:
            self.assertEqual(tree.paths(), reference.paths())
            for _ in range(2):
                for path in tree.paths() + ["absent", "folder", "link/child", "./space name",
                                            ".//space name", "./folder/../space name",
                                            "./space name/", "folder/../space name", "../space name"]:
                    with self.subTest(path=path):
                        self.assertEqual(tree.read(path), reference.read(path))
            reader = tree._reader
        self.assertIsNotNone(reader.poll(), "the batch process must be reaped")

    def test_tree_reads_the_revision_even_when_checkout_changes(self):
        base = self.commit({"src/app.rs": "fn base() {}\n"})
        with sc.Tree(str(self.repo), base) as tree:
            head = self.commit({"src/app.rs": "fn head() {}\n", "new": "new"})
            with sc.Tree(str(self.repo), head) as newer:
                self.assertEqual(tree.read("src/app.rs"), "fn base() {}\n")
                self.assertIsNone(tree.read("new"))
                self.assertEqual(newer.read("src/app.rs"), "fn head() {}\n")

    def test_public_baseline_option_accepts_relative_dot_prefix(self):
        rev = self.commit({
            "sylphx.toml": '[database.migrations]\nengine="drizzle"\n',
            "custom-baseline": "migrations sylphx.toml drizzle\n",
        })
        with contextlib.redirect_stdout(io.StringIO()):
            result = sc.main(["check", "--repo", str(self.repo), "--base", rev, "--head", rev,
                              "--baseline", "./custom-baseline", "--report", str(self.repo / "report.json")])
        self.assertEqual(result, 0)

    def test_missing_blob_is_unreadable_and_next_blob_still_reads(self):
        rev = self.commit({"missing": "missing contents", "present": "present contents"})
        oid = self.git("rev-parse", f"{rev}:missing")
        (self.repo / ".git/objects" / oid[:2] / oid[2:]).unlink()
        with sc.Tree(str(self.repo), rev) as tree:
            self.assertIsNone(tree.read("missing"))
            self.assertEqual(tree.read("present"), "present contents")

    def test_all_checker_passes_produce_the_same_findings(self):
        rev = self.commit({
            "Cargo.toml": '[package]\nname="example"\n',
            "api/package.json": '{"dependencies":{"express":"5"}}',
            "src/main.rs": 'fn main() { std::env::var("UNDECLARED"); }\n',
            ".env.example": "DECLARED=value\n",
            "sylphx.toml": '[database.migrations]\nengine="drizzle"\n',
            "migrations/1.sql": "CREATE TABLE session_turns (id uuid);\n",
            "tests/fixture.rs": "fn test_fixture() {}\n",
        })
        reference = PerFileTree(str(self.repo), rev)
        with sc.Tree(str(self.repo), rev) as tree:
            for check in (sc.departures, sc.agent_runtime, sc.keel_departures, sc.knowledge_tables_in):
                self.assertEqual(check(tree), check(reference))
            for changed in (None, {"src/main.rs": {1}}):
                today = datetime.date(2026, 10, 29)
                self.assertEqual(sc.architecture.evaluate(tree, "example/app", changed, today),
                                 sc.architecture.evaluate(reference, "example/app", changed, today))

    def test_git_process_count_does_not_grow_with_reads(self):
        rev = self.commit({f"src/file_{i}.rs": f"fn file_{i}() {{}}\n" for i in range(64)})
        results = {}
        for label, cls in (("before", PerFileTree), ("after", sc.Tree)):
            rounds = []
            for _ in range(3):
                start = time.perf_counter()
                digest = hashlib.sha256()
                with mock.patch.object(subprocess, "Popen", wraps=subprocess.Popen) as processes:
                    tree = cls(str(self.repo), rev)
                    try:
                        for _ in range(3):
                            for path in tree.paths():
                                digest.update(tree.read(path).encode())
                    finally:
                        if isinstance(tree, sc.Tree):
                            tree.close()
                rounds.append({"git_processes": processes.call_count,
                               "seconds": round(time.perf_counter() - start, 6)})
            results[label] = {"rounds": rounds, "sha256": digest.hexdigest(),
                              "median_seconds": statistics.median(r["seconds"] for r in rounds)}
        self.assertEqual(results["before"]["sha256"], results["after"]["sha256"])
        self.assertTrue(all(r["git_processes"] <= 2 for r in results["after"]["rounds"]), results)
        report = {"measurement": "conformance git reader", "files": 64, "reads_per_round": 192,
                  "fixture_commit": rev, **results}
        print("\nconformance-reader: " + json.dumps(report, sort_keys=True))
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a") as stream:
                stream.write("\n### Conformance reader before/after\n\n"
                             "64 files, 192 reads per round, 3 rounds; identical content hashes. "
                             "Timing is reported without a timing gate.\n\n```json\n" +
                             json.dumps(report, indent=2, sort_keys=True) + "\n```\n")


if __name__ == "__main__":
    unittest.main()
