"""The shipped CLI enforces deal sources and the exact inherited-debt ratchet."""
import pathlib
import tempfile
import unittest
import contextlib
import hashlib
import io
import json
from unittest import mock

from test_rule_adoption import Fixture, OLD, UNKNOWN, WORKFLOW, ROOT, sc, workflow

SOURCE = "src/deck.ts"
BAD = "export function dealCards() { return Math.random(); }\n"
SAFE = "export function dealCards() { return crypto.randomInt(52); }\n"
RULE = "architecture/C1/deal-randomness"


class DealRandomnessGateTests(unittest.TestCase):
    def test_new_live_caller_cannot_adopt_a_previously_audited_warning(self):
        f = self.fixture()
        caller = 'if (demoMode) dealCards();\n'
        base = f.commit({WORKFLOW: workflow(OLD), SOURCE: BAD, 'src/main.ts': caller})
        f.commit({WORKFLOW: workflow(UNKNOWN)})
        digest = lambda value: hashlib.sha256(value.encode()).hexdigest()
        entry = dict(repository='example/app', path=SOURCE, source_sha256=digest(BAD),
                     evidence={}, caller_pattern=r'\bdealCards\b',
                     callers={SOURCE: digest(BAD), 'src/main.ts': digest(caller)},
                     reason='The only caller is a demo branch.')
        policy = f.root.parent / (f.root.name + '-adoption-scope.json')
        policy.write_text(json.dumps(dict(version=1, warnings=[entry])))
        with mock.patch.object(sc.architecture.deal_randomness, 'SCOPE_POLICY', policy), \
                mock.patch.object(sc.adoption, '_checker', return_value=sc):
            result = f.plan(base)
            self.assertFalse(any(line.startswith('architecture C1 deal-randomness ')
                                 for line in result['baseline_text'].splitlines()))
            f.commit({'src/live.ts': 'dealCards();\n'})
            with self.assertRaisesRegex(ValueError, 'new head debt'):
                f.plan(base)

    def test_warning_reports_raw_count_without_refusal_until_new_caller(self):
        f = self.fixture()
        caller = 'if (demoMode) dealCards();\n'
        base = f.commit({WORKFLOW:workflow(OLD),SOURCE:BAD,'src/main.ts':caller})
        f.commit({'README.md':'context\n'})
        digest = lambda value: hashlib.sha256(value.encode()).hexdigest()
        entry = dict(repository='example/app',path=SOURCE,source_sha256=digest(BAD),
                     evidence={},caller_pattern=r'\bdealCards\b',
                     callers={SOURCE:digest(BAD),'src/main.ts':digest(caller)},reason='The only caller is a demo branch.')
        policy = f.root.parent / (f.root.name + '-scope.json')
        policy.write_text(json.dumps(dict(version=1,warnings=[entry])))
        report = f.root.parent / (f.root.name + '-warning.json')
        arguments = ['check','--repo',str(f.root),'--base',base,'--head','HEAD','--repository','example/app',
                     '--today','2026-10-30','--report',str(report),'--shared-source',str(ROOT)]
        with mock.patch.object(sc.architecture.deal_randomness,'SCOPE_POLICY',policy), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sc.main(arguments),0)
            self.assertEqual(json.loads(report.read_text())['deal_randomness'],dict(raw_emissions=1,warnings=1,refusals=0))
            f.commit({'src/live.ts':'dealCards();\n'})
            self.assertEqual(sc.main(arguments),1)
            self.assertEqual(json.loads(report.read_text())['deal_randomness'],dict(raw_emissions=1,warnings=0,refusals=1))

    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Fixture(temporary.name)

    def test_registered_capability_binds_its_source_module(self):
        self.assertIn(RULE, sc.adoption.current_rules())
        action = pathlib.Path(sc.__file__).parent
        files = {sc.adoption.PREFIX + p.name: p.read_text() for p in action.glob("*.py")}
        files[sc.adoption.PREFIX + "action.yml"] = (action / "action.yml").read_text()
        files["policy/deal-randomness-scope.json"] = (action.parents[2] / "policy/deal-randomness-scope.json").read_text()
        for dependency in ("scripts/source_provenance.py", "policy/source-relocations.json"):
            files[dependency] = (ROOT / dependency).read_text()
        profile = sc.adoption.source_profile(sc.DictTree(files))
        self.assertIn(RULE, profile["rules"])
        self.assertIn(sc.adoption.PREFIX + "deal_randomness.py", profile["sources"])
        self.assertIn("policy/deal-randomness-scope.json", profile["sources"])
        del files[sc.adoption.PREFIX + "deal_randomness.py"]
        with self.assertRaisesRegex(ValueError, "deal randomness checker source missing"):
            sc.adoption.source_profile(sc.DictTree(files))

    def test_unbaselined_inherited_deal_fails_then_secure_source_passes(self):
        f = self.fixture()
        base = f.commit({WORKFLOW: workflow(OLD), SOURCE: BAD})
        f.commit({"README.md": "unrelated\n"})
        output, report = f.check(base, 1)
        self.assertIn("deal-randomness", output)
        self.assertEqual([r["disposition"] for r in report["findings"] if r["rule"] == "deal-randomness"], ["regression"])
        f.commit({SOURCE: SAFE})
        f.check(base, 0)

    def test_inherited_adoption_can_only_shrink_and_duplicate_fails(self):
        f = self.fixture()
        base = f.commit({WORKFLOW: workflow(OLD), SOURCE: BAD})
        result = f.plan(base)
        self.assertIn(RULE, result["introduced_rules"])
        baseline = result["baseline_text"]
        adopted = f.commit({WORKFLOW: workflow(UNKNOWN), sc.BASELINE: baseline})
        f.check(base, 0)
        f.commit({"README.md": "line movement preserves the exact violation span\n", SOURCE: "// context\n" + BAD})
        f.check(adopted, 0)
        f.commit({SOURCE: BAD + BAD})
        self.assertIn("deal-randomness", f.check(adopted, 1)[0])
        f.commit({SOURCE: SAFE})
        self.assertIn("stale architecture baseline", f.check(adopted, 1)[0])
        records = [line for line in baseline.splitlines() if not line.startswith("architecture C1 deal-randomness ")]
        f.commit({sc.BASELINE: "\n".join(records) + "\n"})
        clean = f.git("rev-parse", "HEAD")
        f.check(adopted, 0)
        f.commit({SOURCE: BAD, sc.BASELINE: baseline})
        self.assertIn("cannot grow", f.check(clean, 1)[0])

    def test_new_head_violation_cannot_be_adopted(self):
        f = self.fixture()
        base = f.commit({WORKFLOW: workflow(OLD), SOURCE: SAFE})
        f.commit({SOURCE: BAD})
        with self.assertRaisesRegex(ValueError, "new head debt"):
            f.plan(base)

    def test_runtime_vendor_is_checked_and_fixture_is_excluded(self):
        rows = sc.architecture.scan_file("runtime/vendor/core/deck.ts", BAD, "example/app", set(), [])
        self.assertTrue(any(row["rule"] == "deal-randomness" for row in rows))
        self.assertEqual(sc.architecture.scan_file("tests/deck.ts", BAD, "example/app", set(), []), [])

    def test_requested_language_fixtures_reach_the_shipped_cli(self):
        cases = [
            (".rs", "fn deal() { let rng=StdRng::seed_from_u64(42); cards.shuffle(rng); }",
             "fn deal() { cards.shuffle(&mut rand::rng()); }"),
            (".js", "function shuffle(cards) { return cards.sort(()=>Math.random()-.5); }",
             "function shuffle(cards) { return cards[crypto.randomInt(cards.length)]; }"),
            (".java", "class Game {\n void deal() { Random rng=new Random(); cards.shuffle(rng); }\n}",
             "class Game {\n void deal() { Collections.shuffle(cards,new SecureRandom()); }\n}"),
            (".lua", "math.randomseed(os.time())\nfunction deal(cards)\n return cards[math.random(#cards)]\nend",
             "function deal(cards)\n return server.deal(cards)\nend"),
        ]
        for suffix, bad, safe in cases:
            with self.subTest(suffix=suffix):
                f = self.fixture()
                path = "src/deck" + suffix
                base = f.commit({WORKFLOW: workflow(OLD), path: safe})
                f.commit({path: bad})
                self.assertIn("deal-randomness", f.check(base, 1)[0])
                f.commit({path: safe})
                f.check(base, 0)


if __name__ == "__main__":
    unittest.main()
