"""Cache reads cannot quietly become economy authority in any supported language."""
import importlib.util
import pathlib
import tempfile
import unittest

from test_architecture_conformance import GitFixture

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "cache_sc", ROOT / ".github/actions/stack-conformance/stack_conformance.py")
sc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sc)
a = sc.architecture

BAD = {
    "java": """class Shop {
  public void buyLimitedGifts(Request req) {
    Gift template = GiftTemplateCache.getCache().getTemplate(req.id());
    grant(template);
  }
}
""",
    "ts": """export async function purchaseComplete(c: Context): Promise<Response> {
  const offer = await offerCache.get(c.id);
  return grant(offer);
}
""",
    "rs": """pub async fn apply_purchase(req: Request) -> Result<()> {
  let offer = state.offer_cache.get(&req.id).await;
  grant(offer).await
}
""",
}
CLEAN = {
    "java": """class Shop {
  public void buyLimitedGifts(Request req) {
    database.transaction(tx -> {
      Gift authoritative = tx.lockGift(req.id());
      tx.grant(authoritative);
    });
  }
}
""",
    "ts": """export async function purchaseComplete(c: Context): Promise<Response> {
  return db.transaction(async tx => {
    const authoritative = await tx.lockOffer(c.id);
    return tx.grant(authoritative);
  });
}
""",
    "rs": """pub async fn apply_purchase(req: Request) -> Result<()> {
  let mut tx = store.begin().await?;
  let authoritative = lock_offer(&mut tx, req.id).await?;
  grant(&mut tx, authoritative).await?;
  tx.commit().await
}
""",
}
EVIDENCE = {
    "java": """class ShopTest {
  @Test
  public void stale_price_reloads_authority() {
    cache.put(offer(1));
    database.setPrice(2);
    buy();
    assertEquals(2, database.lastCharge());
  }
}
""",
    "ts": """test('stale_price_reloads_authority', async () => {
  cache.set('offer', { price: 1 });
  await database.setPrice(2);
  await purchaseComplete(request);
  expect(await database.lastCharge()).toBe(2);
});
""",
    "rs": """#[tokio::test]
async fn stale_price_reloads_authority() {
  cache.insert(offer(1));
  database.set_price(2).await;
  apply_purchase(request).await.unwrap();
  assert_eq!(database.last_charge().await, 2);
}
""",
}


def findings(path, source):
    return [r for r in a.scan_file(path, source, "Example/game", set(), [])
            if r["rule"] == "cache-economy"]


class CacheRuleTests(unittest.TestCase):
    def test_positive_and_negative_per_language_through_required_cli(self):
        for language in BAD:
            for bad, examples in [(True, BAD), (False, CLEAN)]:
                with self.subTest(language=language, bad=bad), tempfile.TemporaryDirectory() as d:
                    f = GitFixture(d)
                    f.write({"src/shop." + language: examples[language]})
                    f.commit()
                    result, report = f.check(repository="Example/game")
                    self.assertEqual(result.returncode, int(bad), result.stdout + result.stderr)
                    self.assertEqual(any(r["rule"] == "cache-economy" for r in report["findings"]), bad)

    def test_each_handler_spelling_and_recharge_callback(self):
        for name in ["purchaseComplete", "claimReward", "grantItems", "buyLimitedGifts",
                     "redeemVoucher", "rechargeCallBackReq"]:
            with self.subTest(name=name):
                self.assertTrue(findings("src/shop.java", BAD["java"].replace("buyLimitedGifts", name)))

    def test_display_reads_comments_strings_and_neighbouring_handlers_pass(self):
        for language, body in BAD.items():
            with self.subTest(language=language):
                name = {"java": "buyLimitedGifts", "ts": "purchaseComplete", "rs": "apply_purchase"}[language]
                self.assertFalse(findings("src/shop." + language, body.replace(name, "displayOffer")))
                self.assertFalse(findings("tests/shop." + language, body))
        source = '''export function display() { return offerCache.get(id); }
export function buy() {
  // offerCache.get(id);
  const example = "offerCache.get(id)";
  return database.lockOffer(id);
}
'''
        self.assertFalse(findings("src/shop.ts", source))

    def test_arrow_method_route_typed_alias_and_plain_cached_field(self):
        cases = [
            ("ts", "const redeem = async (c) => { return offerCache.get(c.id); };"),
            ("ts", "const actions = { claim: async (c) => { return cache.get(c.id); } };"),
            ("ts", "class Shop {\n async buy(id) { return this.cache.get(id); }\n}"),
            ("ts", "app.post('/purchase', async (c) => { return cache.get(c.id); });"),
            ("ts", "function buy(offers: OfferCache) { return offers.get(id); }"),
            ("java", "class Shop {\n private OfferCache offers;\n public void buy() { offers.get(id); }\n}"),
            ("rs", "struct State { offers: Cache<Id, Offer> }\nfn buy() { self.offers.get(&id); }"),
            ("rs", "fn redeem() { grant(self.cached_price); }"),
        ]
        for language, source in cases:
            with self.subTest(source=source):
                self.assertTrue(findings("src/shop." + language, source))

    def test_invalidation_and_write_only_cache_access_passes(self):
        source = '''function buy() {
  cache.invalidate(id);
  cache.set(id, value);
  invalidateCache(id);
  database.grant(id);
}'''
        self.assertFalse(findings("src/shop.ts", source))
        self.assertFalse(findings("src/shop.ts", "function claim() { return fetch(url, {cache: 'no-store'}); }"))
        self.assertEqual(len(findings("src/shop.ts", "function buy() { cache.get(a); cache.get(b); }")), 2)

    def test_marker_requires_reason_and_existing_named_assertion_bearing_test(self):
        for language, source in BAD.items():
            with self.subTest(language=language):
                path = "src/shop." + language
                row = findings(path, source)[0]
                proof_path = "tests/shop." + language
                marker = " // cache-display-only: response label only; test=" + proof_path + "#stale_price_reloads_authority"
                lines = source.splitlines()
                lines[row["line"] - 1] += marker
                marked = "\n".join(lines) + "\n"
                tree = sc.DictTree({path: marked, proof_path: EVIDENCE[language]})
                self.assertIsNotNone(a.cache_display_proof(tree, path, marked, row["line"]))
                for test in [None, "// stale_price_reloads_authority assertEquals(2, charge)\n",
                             EVIDENCE[language].replace("stale_price_reloads_authority", "another_test")]:
                    tree = sc.DictTree({proof_path: test} if test else {})
                    self.assertIsNone(a.cache_display_proof(tree, path, marked, row["line"]))
                self.assertIsNone(a.cache_display_proof(tree, path, marked.replace("response label only", ""), row["line"]))

    def test_unregistered_typescript_helper_cannot_authorize_a_read(self):
        source = "function buy() { cache.get(id); // cache-display-only: UI label; test=tests/shop.ts#stale_price\n}"
        tree = sc.DictTree({"tests/shop.ts": "function stale_price() { expect(true).toBe(true); }"})
        self.assertIsNone(a.cache_display_proof(tree, "src/shop.ts", source, 1))

    def test_deleting_test_invalidates_unchanged_inline_exception(self):
        with tempfile.TemporaryDirectory() as d:
            f = GitFixture(d)
            source = BAD["ts"].replace("const offer = await offerCache.get(c.id);", "const offer = await offerCache.get(c.id); // cache-display-only: response label only; test=tests/shop.ts#stale_price_reloads_authority")
            f.write({"src/shop.ts": source, "tests/shop.ts": EVIDENCE["ts"]})
            f.commit()
            result, _ = f.check(repository="Example/game")
            self.assertEqual(result.returncode, 0, result.stdout)
            before = f.git("rev-parse", "HEAD").strip()
            (f.root / "tests/shop.ts").unlink()
            f.commit()
            result, report = f.check(base=before, repository="Example/game")
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertTrue(report["findings"][0]["changed"])

    def test_cache_baseline_records_use_existing_rule_scoped_ratchet(self):
        row = findings("src/shop.ts", BAD["ts"])[0]
        key = a.baseline_key(row)
        rule = "architecture/C1/cache-economy"
        self.assertFalse(a.baseline_errors([row], {key}, set(), [row], {rule}))
        self.assertTrue(a.baseline_errors([row], {key}, set(), [], {rule}))
        self.assertTrue(a.baseline_errors([], {key}, {key}, [row], set()))
        self.assertIn(rule, sc.adoption.current_rules())

    def test_inline_rust_test_can_supply_evidence(self):
        source = BAD["rs"].replace("let offer = state.offer_cache.get(&req.id).await;", "let offer = state.offer_cache.get(&req.id).await; // cache-display-only: response label only; test=src/shop.rs#stale_price_reloads_authority") + EVIDENCE["rs"]
        row = findings("src/shop.rs", source)[0]
        tree = sc.DictTree({"src/shop.rs": source})
        self.assertIsNotNone(a.cache_display_proof(tree, "src/shop.rs", source, row["line"]))

    def test_legacy_java_comment_bytes_are_preserved_and_checked(self):
        with tempfile.TemporaryDirectory() as d:
            f = GitFixture(d)
            f.write({"src/shop.java": BAD["java"]})
            with (f.root / "src/shop.java").open("ab") as out:
                out.write(b"// legacy comment: \xff\xfe\n")
            f.commit()
            result, report = f.check(repository="Example/game")
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertFalse(report["unreadable"])
            self.assertTrue(any(r["rule"] == "cache-economy" for r in report["findings"]))


if __name__ == "__main__":
    unittest.main()
