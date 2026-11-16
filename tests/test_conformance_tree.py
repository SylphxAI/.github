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

    def test_shared_blob_cache_and_encoding_parity(self):
        rev_a = self.commit({
            "src/a.rs": "fn a() {}\n",
            "src/raw.rs": b"fn bad() { /* \xff\xfe */ }\n",
        })
        rev_b = self.commit({
            "src/b.rs": "fn b() {}\n",
        })
        shared = {}
        with sc.Tree(str(self.repo), rev_a, blob_cache=shared) as tree_a:
            self.assertEqual(tree_a.blob_oid("./src/a.rs"), tree_a.blob_oid("src/a.rs"))
            self.assertEqual(tree_a.read("src/a.rs"), "fn a() {}\n")
            self.assertIsNone(tree_a.read("src/raw.rs"))
            self.assertEqual(tree_a.read_model_source("src/raw.rs"), b"fn bad() { /* \xff\xfe */ }\n".decode("utf-8", errors="surrogateescape"))
        with sc.Tree(str(self.repo), rev_b, blob_cache=shared) as tree_b:
            self.assertEqual(tree_b.read("src/a.rs"), "fn a() {}\n")
            self.assertIsNone(tree_b.read("src/raw.rs"))
            self.assertEqual(tree_b.read_model_source("src/raw.rs"), b"fn bad() { /* \xff\xfe */ }\n".decode("utf-8", errors="surrogateescape"))
            self.assertIsNone(tree_b._reader)
            self.assertEqual(tree_b.read("src/b.rs"), "fn b() {}\n")
            self.assertIsNotNone(tree_b._reader)

    def test_cross_tree_scan_cache_and_invalidation_parity(self):
        base = self.commit({
            "src/env.rs": 'fn f() {\n    let _ = std::env::var("FOO");\n    let _ = std::env::var("BAR");\n}\n',
            "src/models.ts": 'export const model = "openai/gpt-4o";\nexport const clean = "hello";\n',
            "crate/src/rng.rs": "pub struct WeakRng { pub state: u64 }\nimpl WeakRng { pub fn step(&mut self) -> u64 { self.state ^= self.state << 13; self.state } }\n",
            "crate/src/deal.rs": "use crate::rng::WeakRng;\npub fn deal_hand() -> u64 { let r = WeakRng::new(); r.next() }\n",
            "sylphx.toml": '[database.migrations]\nengine="postgres"\n',
            "ops/tenant-read-functions.json": json.dumps({"functions": ["fn_tenant"]}),
            "scripts/push.sh": "do_push() { git push \"$@\"; }\ndo_push --force origin main\n",
            "contracts/spec.json": '{"paths":{"/v1/x":{"model":"claude-3-5-sonnet-20241022"}},"url":"https://api.example.com"}\n',
            ".env.example": "FOO=1\n",
        })
        head = self.commit({
            ".env.example": "BAR=1\n",
            "crate/src/rng.rs": "pub struct WeakRng { pub state: u64 }\nimpl WeakRng { pub fn step(&mut self) -> u64 { self.state } }\n",
            "scripts/push.sh": "do_push() { echo \"$@\"; }\ndo_push --force origin main\n",
            "contracts/spec.json": '{"paths":{"/v1/x":{"model":"claude-3-5-sonnet-20241022"}},"url":"https://api.example.com"}\n',
            "src/extra.py": 'model = "gpt-4"\n',
        })
        today = datetime.date(2026, 11, 16)
        blob_cache, model_cache, force_cache, arch_cache, sv_cache = {}, {}, {}, {}, {}
        with sc.Tree(str(self.repo), head, blob_cache=blob_cache) as head_tree, sc.Tree(str(self.repo), base, blob_cache=blob_cache) as base_tree:
            changed = sc.added_lines(str(self.repo), base, head)
            c_m_head = sc.model_pins.scan(head_tree, "example/app", None, sc.architecture.source_views, cache=model_cache, shared_views=sv_cache)
            c_m_base = sc.model_pins.scan(base_tree, "example/app", None, sc.architecture.source_views, cache=model_cache, shared_views=sv_cache)
            c_f_head = sc.no_force_push.scan(head_tree, cache=force_cache)
            c_f_base = sc.no_force_push.scan(base_tree, cache=force_cache)
            c_a_head = sc.architecture.evaluate(head_tree, "example/app", changed, today, file_cache=arch_cache, shared_views=sv_cache)
            # Mutate head findings in place (as cmd_check does) before evaluating base.
            for row in c_a_head["findings"]:
                row["changed"] = True
                row["disposition"] = "mutated"
            c_a_base = sc.architecture.evaluate(base_tree, "example/app", None, today, ignore_expired=True, file_cache=arch_cache, shared_views=sv_cache)

            u_m_head = sc.model_pins.scan(head_tree, "example/app", None, sc.architecture.source_views)
            u_m_base = sc.model_pins.scan(base_tree, "example/app", None, sc.architecture.source_views)
            u_f_head = sc.no_force_push.scan(head_tree)
            u_f_base = sc.no_force_push.scan(base_tree)
            u_a_head = sc.architecture.evaluate(head_tree, "example/app", changed, today)
            for row in u_a_head["findings"]:
                row["changed"] = True
                row["disposition"] = "mutated"
            u_a_base = sc.architecture.evaluate(base_tree, "example/app", None, today, ignore_expired=True)

            self.assertEqual(c_m_head, u_m_head)
            self.assertEqual(c_m_base, u_m_base)
            self.assertEqual(c_f_head, u_f_head)
            self.assertEqual(c_f_base, u_f_base)
            self.assertEqual(c_a_head, u_a_head)
            self.assertEqual(c_a_base, u_a_base)
            head_env = {r["line"] for r in c_a_head["findings"] if r["path"] == "src/env.rs"}
            base_env = {r["line"] for r in c_a_base["findings"] if r["path"] == "src/env.rs"}
            self.assertEqual(head_env, {2})
            self.assertEqual(base_env, {3})
            self.assertTrue(c_f_base["findings"])
            self.assertFalse(c_f_head["findings"])
            self.assertIn("crate/src/deal.rs", {r["path"] for r in c_a_base["findings"]})
            self.assertNotIn("crate/src/deal.rs", {r["path"] for r in c_a_head["findings"]})

    def test_rust_tokens_and_matching_end_parity(self):
        pp = sc.architecture.project_portability
        cases = [
            'let x = br#"/* not comment */" "#; // line comment\nlet c = \'\\u{1F600}\';',
            '/* outer /* inner */ still comment */ let a: &\'static str = "a\\\\\\"b";',
            'ar#"quoted"# + cr#"raw"#',
        ]
        for src in cases:
            tokens = list(pp.rust_tokens(src))
            self.assertTrue(all(src[s:e] for _, s, e in tokens))
        for broken, err in [
            ("/* /* */", "unterminated Rust block comment"),
            ('r#"abc"', "unterminated Rust raw string"),
            ('"unclosed\\', "unterminated Rust string"),
        ]:
            with self.assertRaisesRegex(ValueError, err):
                list(pp.rust_tokens(broken))
        self.assertEqual(sc.architecture.matching_end("a{b}c}", 0, "{", "}"), 6)
        self.assertEqual(sc.architecture.matching_end("abc", 10, "{", "}"), 10)
        self.assertEqual(sc.architecture.matching_end("abc", 0, "{", "}"), 3)
        self.assertIsNone(sc.model_pins.balanced_end("abc", 5, "{", "}"))
        self.assertIsNone(sc.model_pins.balanced_end("abc", 0, "{", "}"))
        self.assertEqual(sc.model_pins.balanced_end("{a}}", 0, "{", "}"), 4)

    def test_model_selector_arbitrary_provider_unversioned_and_unicode_parity(self):
        base = self.commit({
            "src/custom.ts": 'export const model = "custom-provider/custom-model";\n',
            "cfg/custom.json": '{"model": "acme-ai/internal-chat"}\n',
            "cfg/custom.yaml": "model: custom-provider/custom-model\n",
            "src/bare.ts": 'export const model = "claude";\nexport const backup = {model: "gpt"};\n',
            "cfg/bare.yaml": "model:\n  claude\n",
            "src/unicode.ts": 'const note = "İİİ";\nexport const model = "gpt-4o";\nexport const ref = "anthropic/claude-3-5-sonnet";\n',
        })
        head = self.commit({
            "src/custom.ts": 'export const model = "custom-provider/custom-model";\n',
            "cfg/custom.json": '{"model": "acme-ai/internal-chat"}\n',
            "cfg/custom.yaml": "model: custom-provider/custom-model\n",
            "src/bare.ts": 'export const model = "o3";\n',
            "cfg/bare.yaml": "model:\n  claude\n",
            "src/unicode.ts": 'const note = "İİİ";\nexport const model = "gpt-4o";\nexport const ref = "anthropic/claude-3-5-sonnet";\n',
        })
        blob_cache, model_cache, sv_cache = {}, {}, {}
        with sc.Tree(str(self.repo), head, blob_cache=blob_cache) as head_tree, sc.Tree(str(self.repo), base, blob_cache=blob_cache) as base_tree:
            c_base = sc.model_pins.scan(base_tree, "example/app", None, sc.architecture.source_views, cache=model_cache, shared_views=sv_cache)
            c_head = sc.model_pins.scan(head_tree, "example/app", None, sc.architecture.source_views, cache=model_cache, shared_views=sv_cache)
            u_base = sc.model_pins.scan(base_tree, "example/app", None, sc.architecture.source_views)
            u_head = sc.model_pins.scan(head_tree, "example/app", None, sc.architecture.source_views)

        self.assertEqual(c_base, u_base)
        self.assertEqual(c_head, u_head)
        base_by_path = {(r["path"], r["model"]): r.get("rule") for r in c_base["findings"]}
        self.assertEqual(base_by_path[("src/custom.ts", "custom-provider/custom-model")], "model-selector")
        self.assertEqual(base_by_path[("cfg/custom.json", "acme-ai/internal-chat")], "model-selector")
        self.assertEqual(base_by_path[("cfg/custom.yaml", "custom-provider/custom-model")], "model-selector")
        self.assertEqual(base_by_path[("src/bare.ts", "claude")], "model-selector")
        self.assertEqual(base_by_path[("src/bare.ts", "gpt")], "model-selector")
        self.assertEqual(base_by_path[("cfg/bare.yaml", "claude")], "model-selector")
        self.assertIsNone(base_by_path[("src/unicode.ts", "gpt-4o")])
        self.assertIsNone(base_by_path[("src/unicode.ts", "anthropic/claude-3-5-sonnet")])
        head_by_path = {(r["path"], r["model"]): r.get("rule") for r in c_head["findings"]}
        self.assertIn(("src/bare.ts", "o3"), head_by_path)
        self.assertIsNone(head_by_path[("src/bare.ts", "o3")])

    def test_classifier_metadata_array_and_duplicate_key_parity(self):
        files = {
            "taxonomy_array.json": '{"taxonomy":{"models":["gpt-4o","claude-3-5-sonnet-20241022"]}}\n',
            "nested_array.json": '{"match":{"model":[["gpt-4o"]]}}\n',
            "escaped_key.json": '{"tax_onomy":{"mo\\u0064el":["gpt-4o"]}}\n',
            "duplicate_key.json": '{"taxonomy":{"models":["gpt-4o"],"models":[]}}\n',
            "nested_object.json": '{"taxonomy":{"model":{"nested":"gpt-4o"}}}\n',
        }
        res = sc.model_pins.scan(sc.DictTree(files), "example/app", None, sc.architecture.source_views)
        self.assertEqual([r["path"] for r in res["findings"]], ["nested_object.json"])
        exempt_paths = [r["path"] for r in res["exemptions"]]
        self.assertEqual(
            exempt_paths,
            ["duplicate_key.json", "escaped_key.json", "nested_array.json", "taxonomy_array.json", "taxonomy_array.json"],
        )

    def test_vendor_deal_randomness_scope_warning_invalidated_on_new_caller(self):
        root_cargo = '[package]\nname = "app"\nversion = "0.1.0"\n[dependencies]\nvrng = { path = "vendor/vrng" }\n'
        vendor_cargo = '[package]\nname = "vrng"\nversion = "0.1.0"\n'
        vendor_src = "pub fn deal_hand(cards: &mut [u64]) {\n    let rng = SmallRng::seed_from_u64(7);\n    cards.shuffle(rng);\n}\n"
        caller_src = "pub fn ok() -> u64 { deal_hand() }\n"
        first = self.commit({
            "Cargo.toml": root_cargo,
            "vendor/vrng/Cargo.toml": vendor_cargo,
            "vendor/vrng/src/lib.rs": vendor_src,
            "src/caller.rs": caller_src,
        })
        second = self.commit({
            "Cargo.toml": root_cargo,
            "vendor/vrng/Cargo.toml": vendor_cargo,
            "vendor/vrng/src/lib.rs": vendor_src,
            "src/caller.rs": caller_src,
            "src/new_caller.rs": "pub fn extra() -> u64 { deal_hand() }\n",
        })
        digest = lambda s: hashlib.sha256(s.replace("\r\n", "\n").encode()).hexdigest()
        policy = {
            "version": 1,
            "warnings": [{
                "repository": "example/app",
                "path": "vendor/vrng/src/lib.rs",
                "source_sha256": digest(vendor_src),
                "evidence": {"Cargo.toml": digest(root_cargo)},
                "caller_pattern": r"\bdeal_hand\b",
                "callers": {
                    "src/caller.rs": digest(caller_src),
                    "vendor/vrng/src/lib.rs": digest(vendor_src),
                },
                "reason": "verified single non-gambling caller",
            }],
        }
        today = datetime.date(2026, 11, 16)
        with tempfile.NamedTemporaryFile("w", suffix=".json") as policy_file:
            policy_file.write(json.dumps(policy))
            policy_file.flush()
            with mock.patch.object(sc.architecture.deal_randomness, "SCOPE_POLICY", pathlib.Path(policy_file.name)):
                blob_cache, file_cache, sv_cache = {}, {}, {}
                with sc.Tree(str(self.repo), first, blob_cache=blob_cache) as first_tree, \
                        sc.Tree(str(self.repo), second, blob_cache=blob_cache) as second_tree:
                    r_first = sc.architecture.evaluate(first_tree, "example/app", None, today, file_cache=file_cache, shared_views=sv_cache)
                    r_second_cached = sc.architecture.evaluate(second_tree, "example/app", None, today, file_cache=file_cache, shared_views=sv_cache)
                    r_second_uncached = sc.architecture.evaluate(second_tree, "example/app", None, today)

        self.assertEqual(r_second_cached, r_second_uncached)
        first_vendor = [r for r in r_first["findings"] if r["path"] == "vendor/vrng/src/lib.rs" and r["rule"] == "deal-randomness"]
        second_vendor = [r for r in r_second_cached["findings"] if r["path"] == "vendor/vrng/src/lib.rs" and r["rule"] == "deal-randomness"]
        self.assertTrue(first_vendor and all(r.get("scope_warning") == "verified single non-gambling caller" for r in first_vendor))
        self.assertTrue(second_vendor and all(r.get("scope_warning") is None for r in second_vendor))


if __name__ == "__main__":
    unittest.main()
