"""Deal-source regressions, secure controls and cosmetic counterexamples."""
import importlib.util
import pathlib
import unittest
import hashlib
import json
import tempfile

from test_stack_conformance import sc

ACTION = pathlib.Path(__file__).resolve().parents[1] / ".github/actions/stack-conformance"
SPEC = importlib.util.spec_from_file_location("deal_randomness", ACTION / "deal_randomness.py")
deal = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deal)


def scan(suffix, source, files=None, path=None):
    return deal.scan(path or "src/game" + suffix, source, sc.architecture.source_views,
                     sc.architecture.named_bodies, sc.architecture.matching_end,
                     tree=sc.DictTree(files) if files is not None else None)


class DealRandomnessTests(unittest.TestCase):
    def test_expression_arrow_and_class_helpers_keep_lexical_ownership(self):
        self.assertTrue(scan('.ts', 'const dealCards = (cards) => cards.sort(()=>Math.random()-.5);'))
        source = '''class View { pick() { return Math.random(); } }
class Game { pick() { return crypto.randomInt(52); }
 dealCards() { return this.pick(); } }'''
        self.assertEqual(scan('.ts', source), [])
        self.assertTrue(scan('.ts', source.replace('crypto.randomInt(52)', 'Math.random()')))
        self.assertTrue(scan('.ts', 'function choose(cards) { return Math.random(); }\nclass Game { dealCards() { return choose(cards); } }'))
        self.assertTrue(scan('.cs', 'class Chooser { public int Pick() { return new Random().Next(); } }\nclass Game { public void DealCards() { Chooser picker = new Chooser(); picker.Pick(); } }'))

    def test_runtime_simulation_and_literal_false_have_different_reachability(self):
        self.assertTrue(scan('.java', 'class Game {\n void dealCards() { simulateDeal(); }\n void simulateDeal() { Collections.shuffle(cards); }\n}'))
        self.assertEqual(scan('.java', 'class Game {\n void dealCards() { if (false) { simulateDeal(); } }\n void simulateDeal() { Collections.shuffle(cards); }\n}'), [])
        self.assertEqual(scan('.lua', 'function deal(cards)\n if (false and #cards > 0) then\n return cards[math.random(52)]\n else\n return server.deal(cards)\n end\nend'), [])
        self.assertTrue(scan('.lua', 'function deal(cards)\n if false and false or true then\n return cards[math.random(52)]\n end\nend'))

    def test_shadowed_alias_and_uncalled_nested_helper_do_not_taint_a_deal(self):
        self.assertEqual(scan('.ts', 'const rand=Math.random;\nfunction dealCards(rand=()=>crypto.randomInt(52)){return cards[rand()];}'), [])
        self.assertEqual(scan('.ts', 'function dealCards(){function choose(){return Math.random();}\n return crypto.randomInt(52);}'), [])
        self.assertTrue(scan('.ts', 'function dealCards(){function choose(){return Math.random();}\n return choose();}'))

    def test_switch_cases_are_blocks_and_do_not_create_helper_edges(self):
        source = '''class Game {
 void dealCards() { switch(card) { case(1): { Math.random(); break; } } }
 void simulateDeal() { switch(card) { case(2): { Math.random(); break; } } }
}'''
        rows = scan('.cs', source)
        self.assertEqual([source.count('\n',0,a)+1 for a,_,_ in rows],[2])

    def test_scope_warning_binds_source_callers_and_selection_inputs(self):
        source = 'function rollDice() { return Math.random(); }'
        files = {'src/demo.ts': source, 'src/main.ts': 'if (demoMode) rollDice();', 'Cargo.toml': '[package]\nname="game"\n'}
        digest = lambda value: hashlib.sha256(value.encode()).hexdigest()
        entry = dict(repository='example/game', path='src/demo.ts', source_sha256=digest(source),
                     caller_pattern=r'\brollDice\b', callers={name:digest(value) for name,value in files.items() if 'rollDice' in value},
                     evidence={'Cargo.toml':digest(files['Cargo.toml'])}, reason='Only the demo branch calls this helper.')
        with tempfile.TemporaryDirectory() as directory:
            policy = pathlib.Path(directory) / 'scope.json'
            policy.write_text(json.dumps(dict(version=1,warnings=[entry])))
            check = lambda values: deal.scope_reason('example/game','src/demo.ts',source,sc.DictTree(values),policy)
            self.assertEqual(check(files), entry['reason'])
            self.assertIsNone(check({**files,'src/live.ts':'rollDice();'}))
            self.assertIsNone(check({**files,'Cargo.toml':'[package]\nname="changed"\n'}))
            self.assertIsNone(check({**files,'src/main.ts':'rollDice();'}))

    def test_must_fail_sources(self):
        cases = [
            (".rs", "fn deal() { let mut rng = StdRng::seed_from_u64(42); deck.shuffle(&mut rng); }"),
            (".rs", "fn deal() { let seed = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_millis() as u64; let mut rng = StdRng::seed_from_u64(seed); deck.shuffle(&mut rng); }"),
            (".rs", "fn shuffle() { let mut rng = SmallRng::seed_from_u64(7); cards.shuffle(&mut rng); }"),
            (".rs", "const SEED: [u8;32] = [7;32];\nfn deal() { let mut rng=StdRng::from_seed(SEED); cards.shuffle(&mut rng); }"),
            (".ts", "export function shuffle(deck: Card[]) { return deck.sort(() => Math.random() - .5); }"),
            (".js", "function dealCards() { return Math.random(); }"),
            (".java", "class Game {\n public void deal() { Random rng = new Random(); cards.shuffle(rng); }\n}"),
            (".lua", "math.randomseed(os.time())\nfunction deal(cards)\n local index = math.random(#cards)\n return cards[index]\nend"),
            (".cs", "class Game {\n public void DrawCards() { Random.InitState((int)DateTime.Now.Ticks); var card = deck[Random.Range(0,52)]; }\n}"),
        ]
        for suffix, source in cases:
            with self.subTest(suffix=suffix, source=source):
                self.assertTrue(scan(suffix, source))

    def test_secure_sources_pass(self):
        cases = [
            (".rs", "fn deal() { let mut rng = OsRng; cards.shuffle(&mut rng); }"),
            (".rs", "fn shuffle() { cards.shuffle(&mut rand::rng()); }"),
            (".rs", "fn deal() { getrandom::fill(&mut seed).unwrap(); let mut rng = ChaCha20Rng::from_seed(seed); cards.shuffle(&mut rng); }"),
            (".rs", "fn deal(seed:u64) { let mut rng=StdRng::seed_from_u64(seed); cards.shuffle(&mut rng); }"),
            (".ts", "function shuffle(cards:Card[]) { crypto.getRandomValues(words); return cards[words[0]]; }"),
            (".js", "function deal() { return cards[crypto.randomInt(cards.length)]; }"),
            (".java", "class Game {\n void deal() { Collections.shuffle(cards, new SecureRandom()); }\n}"),
            (".lua", "function deal(cards)\n return server.deal(cards)\nend"),
            (".cs", "class Game {\n public void DrawCards() { var card = deck[RandomNumberGenerator.GetInt32(deck.Count)]; }\n}"),
        ]
        for suffix, source in cases:
            with self.subTest(suffix=suffix, source=source):
                self.assertEqual(scan(suffix, source), [])

    def test_cosmetics_and_unrelated_generator_do_not_refuse_secure_deal(self):
        cases = [
            (".ts", "function animateParticles() { return Math.random(); }\nfunction deal() { return crypto.randomInt(52); }"),
            (".js", "function shuffleParticles() { return Math.random(); }"),
            (".java", "class View {\n void drawParticles() { Random r = new Random(); float yaw = r.nextFloat(); }\n void deal() { Collections.shuffle(cards, new SecureRandom()); }\n}"),
            (".rs", "fn animate() { let mut rng = SmallRng::seed_from_u64(1); particle(rng.random()); }\nfn deal() { cards.shuffle(&mut rand::rng()); }"),
            (".lua", "math.randomseed(os.time())\nfunction animate() return math.random() end\nfunction deal() return server.deal() end"),
            (".cs", "class View {\n void AnimateDice() { var yaw = Random.Range(0,360); }\n void DrawCards() { server.GetCards(); }\n}"),
            (".cs", "class View {\n void DrawNoiseQuad(Material fx) { float texTile = TILE_AMOUNT; GL.Begin(GL.QUADS); var yaw = Random.Range(0,1); }\n}"),
            (".java", "class Room {\n void handleRobotQueue() { handleExpired(); Collections.shuffle(idlePlayers); }\n}"),
            (".rs", "fn measure_deal_weight_gain() { let rng=SmallRng::seed_from_u64(7); rng.random(); }"),
        ]
        for suffix, source in cases:
            with self.subTest(suffix=suffix):
                self.assertEqual(scan(suffix, source), [])

    def test_same_file_helper_and_global_random_binding_are_checked(self):
        self.assertTrue(scan(".ts", "const random = Math.random;\nfunction choose() { return random(); }\nfunction dealCards() { return choose(); }"))
        self.assertTrue(scan(".rs", "fn next_card() { let mut x = self.seed; x ^= x >> 12; self.seed=x; x }\nfn deal() { next_card(); }"))
        self.assertTrue(scan(".java", "class Game {\n Random r = new Random();\n void deal() { cards.add(r.nextInt(52)); }\n}"))
        self.assertTrue(scan(".lua", "local rand = math.random\nfunction deal(cards)\n if #cards > 0 then\n return cards[rand(#cards)]\n end\nend"))
        self.assertTrue(scan(".cs", "class Core {\n public static void ShuffleCards() { Random(0,52); }\n internal static int Random(int min,int max) { if (_random == null) _random = new Random(); return _random.Next(min,max); }\n}"))
        self.assertTrue(scan(".lua", "local mj_tile_maker = function()\n local tiles={}\n for i=1,144 do local j=math.random(i,144); tiles[i],tiles[j]=tiles[j],tiles[i] end\n return tiles\nend\nM.new=function(self) self.tiles=mj_tile_maker() end"))

    def test_unseeded_java_shuffle_fails(self):
        self.assertTrue(scan(".java", "class Game {\n void startHand() { Collections.shuffle(cards); }\n}"))

    def test_insecure_default_is_an_entrypoint_even_with_secure_sibling(self):
        source = "function dealDeck(rng:()=>number = Math.random) { return rng(); }\nfunction dealHands() { return dealDeck(()=>crypto.randomInt(52)); }"
        self.assertTrue(scan(".ts", source))

    def test_comments_strings_and_inline_rust_tests_do_not_count(self):
        self.assertEqual(scan(".rs", '#[cfg(test)]\nmod tests { fn deal() { let r=SmallRng::seed_from_u64(1); } }\nfn deal() { /* SmallRng */ cards.shuffle(&mut rand::rng()); }'), [])
        self.assertEqual(scan(".ts", 'function deal() { const note="Math.random"; /* Math.random() */ return crypto.randomInt(52); }'), [])
        self.assertEqual(scan(".lua", '-- math.randomseed(os.time())\nfunction deal()\n local note="math.random()"\n return server.deal()\nend'), [])

    def test_secure_seed_does_not_clear_small_rng_or_keel_visual_rng(self):
        self.assertTrue(scan(".rs", "fn deal() { let mut rng = SmallRng::from_os_rng(); deck.shuffle(&mut rng); }"))
        self.assertTrue(scan(".rs", "use keel_random::Rng;\nfn shuffled(seed:u64) { let mut rng = Rng::new(seed); tiles.swap(0,rng.below_u32(52)); }"))

    def test_explicit_custom_rng_module_resolves_without_repo_wide_taint(self):
        rng = "pub struct Rng { state:u64 }\nimpl Rng { fn next_u64(&mut self) { self.state = self.state.wrapping_mul(17); } }"
        wall = "use crate::fun::rng::Rng;\nfn shuffled(seed:u64) { let mut rng=Rng::new(seed); tiles.swap(0,rng.gen_range_u32(52)); }"
        files = {"crates/rules/src/fun/rng.rs": rng}
        self.assertTrue(scan(".rs", wall, files, "crates/rules/src/fun/wall.rs"))
        secure = "pub struct Rng(ChaCha20Rng);\nimpl Rng { fn next_u64(&mut self) { self.0.next_u64() } }"
        self.assertEqual(scan(".rs", wall, {"crates/rules/src/fun/rng.rs":secure}, "crates/rules/src/fun/wall.rs"), [])

    def test_offsets_bind_actual_violation_and_preserve_duplicates(self):
        source = "// heading\nfunction deal() { Math.random(); Math.random(); }"
        rows = scan(".ts", source)
        self.assertEqual([source[a:b] for a,b,_ in rows], ["Math.random", "Math.random"])
        self.assertEqual([source.count("\n",0,a)+1 for a,_,_ in rows], [2,2])

    def test_vendor_runtime_dependency_is_checked_but_unused_copy_is_not(self):
        source = "fn deal() { let rng = SmallRng::seed_from_u64(7); cards.shuffle(rng); }"
        files = {"Cargo.toml": '[package]\nname="game"\nversion="0.1.0"\n',
                 "vendor/engine/Cargo.toml": '[package]\nname="engine"\nversion="0.1.0"\n'}
        path = "vendor/engine/src/lib.rs"
        self.assertEqual(scan(".rs", source, files, path), [])
        files["Cargo.toml"] += '[dependencies]\nengine={path="vendor/engine"}\n'
        self.assertTrue(scan(".rs", source, files, path))

    def test_transitive_workspace_path_dependency_reaches_vendor(self):
        source = "fn deal() { let rng = SmallRng::seed_from_u64(7); cards.shuffle(rng); }"
        files = {"Cargo.toml": '[workspace]\nmembers=["game"]\n[workspace.dependencies]\nengine={path="vendor/engine"}\n',
                 "game/Cargo.toml": '[package]\nname="game"\nversion="0.1.0"\n[dependencies]\nengine.workspace=true\n',
                 "vendor/engine/Cargo.toml": '[package]\nname="engine"\nversion="0.1.0"\n'}
        self.assertTrue(scan(".rs", source, files, "vendor/engine/src/lib.rs"))

    def test_root_patch_path_is_relative_to_patch_manifest(self):
        source = "fn deal() { let rng = SmallRng::seed_from_u64(7); cards.shuffle(rng); }"
        files = {"Cargo.toml": '[workspace]\nmembers=["game"]\n[patch.crates-io]\nengine={path="vendor/engine"}\n',
                 "game/Cargo.toml": '[package]\nname="game"\nversion="0.1.0"\n[dependencies]\nengine="1"\n',
                 "vendor/engine/Cargo.toml": '[package]\nname="engine"\nversion="0.1.0"\n'}
        self.assertTrue(scan(".rs", source, files, "vendor/engine/src/lib.rs"))

    def test_dice_face_is_checked_separately_from_force_and_duration(self):
        source = '''local function rollDice(object)
 local function randomForce(object)
  object:applyTorque(math.random(100,200))
 end
 randomForce(object)
 local function onEnterFrame(event)
  object:setFrame(math.random(6))
 end
 object:addEventListener("enterFrame", onEnterFrame)
 transition.from(object, {
  time=math.random(400,800),
 })
end'''
        rows = scan(".lua", source)
        self.assertEqual([source.count("\n",0,a)+1 for a,_,_ in rows], [7])
        self.assertTrue(scan(".lua", "function rollDice()\n local time=math.random(6)\n return time\nend"))


if __name__ == "__main__":
    unittest.main()
