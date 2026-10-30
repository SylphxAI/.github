"""Domain and read/write boundaries for the economy cache recognizer."""
import tempfile
import unittest

from test_game_cache import GitFixture, findings


class CachePrecisionTests(unittest.TestCase):
    def test_typed_permission_grants_are_not_item_grants(self):
        source = '''class Gateway {
  async grantPermission(permissions: PermissionId[]): Promise<void> {
    const grant: AppPermissionGrant = { permissions };
    await this.storage.save(grant);
    this.permissionCache.delete(this.permissionCacheKey(id));
  }
  private async getPermissionGrant(id: AppId): Promise<AppPermissionGrant | null> {
    const cacheKey = this.permissionCacheKey(id);
    const cached = this.permissionCache.get(cacheKey);
    if (cached && cached.expires > Date.now()) return cached.grant;
    const grant = await this.storage.get(id);
    this.permissionCache.set(cacheKey, { grant, expires: Date.now() + Gateway.CACHE_TTL });
    return grant;
  }
}'''
        self.assertFalse(findings("src/gateway.ts", source))

    def test_typed_generation_lease_is_not_a_reward_claim(self):
        for body in [
            "let entry = s.caches.entry(key).or_insert(Entry { lease_until: 0 }); Claim::Busy",
            "match self.cached(player, key).await? { Cached::Ready { value } => Claim::Ready { value } }",
        ]:
            source = '''pub async fn claim(
                &self, player: &str, key: CacheKey, lease_ms: i64,
            ) -> Result<Claim, StoreError> { BODY }'''.replace("BODY", body)
            with self.subTest(body=body):
                self.assertFalse(findings("src/store.rs", source))

    def test_domain_boundaries_through_required_cli(self):
        cases = [
            ("ts", "function grantPermission(id: PermissionId) { return permissionCache.get(id); }",
             "function grantPermission(id: PermissionId) { return itemCache.get(id); }"),
            ("rs", "fn claim(key: CacheKey, lease_ms: i64) -> Claim { self.cached(key) }",
             "fn claim(key: CacheKey, lease_ms: i64) -> Claim { grant(self.cached(key)) }"),
        ]
        for language, clean, bad in cases:
            for expected, source in [(0, clean), (1, bad)]:
                with self.subTest(language=language, expected=expected), tempfile.TemporaryDirectory() as d:
                    fixture = GitFixture(d)
                    fixture.write({"src/handler." + language: source})
                    fixture.commit()
                    result, report = fixture.check(repository="Example/game")
                    self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                    self.assertEqual(any(row["rule"] == "cache-economy" for row in report["findings"]), bool(expected))

    def test_domain_context_does_not_hide_economy_reads(self):
        cases = [
            ("ts", "function grantPermission(id: PermissionId) { grantItems(itemCache.get(id)); }"),
            ("ts", "function grantPermission(id: PermissionId) { return cached_price; }"),
            ("ts", "function grantItems(id: ItemId) { return permissionCache.get(id); }"),
            ("rs", "fn claim(key: CacheKey, lease_ms: i64) -> Claim { grant(cache.get(key)); }"),
            ("rs", "fn claim_reward(key: CacheKey, lease_ms: i64) -> Claim { self.cached(key) }"),
            ("rs", "fn claim(key: RewardCacheKey, lease_ms: i64) -> Claim { self.cached(key) }"),
            ("rs", "fn claim(key: CacheKey) -> Claim { self.cached(key) }"),
            ("ts", "function claim() { return cache.get(id); }"),
        ]
        for language, source in cases:
            with self.subTest(source=source):
                self.assertTrue(findings("src/handler." + language, source))

    def test_cache_factory_accessor_is_not_a_purchase(self):
        source = '''class Templates {
  public static RoleBuyTemplateCache getRoleBuyTemplateCache() {
    return TemplateFactory.getTemplateCache(RoleBuyTemplateCache.class);
  }
  public void buyRole() {
    RoleBuyTemplate template = RoleBuyTemplateCache.getRoleBuyTemplateCache().getTemplate(id);
    grant(template);
  }
}'''
        rows = findings("src/templates.java", source)
        self.assertTrue(rows)
        self.assertEqual({row["line"] for row in rows}, {6})
        changed = source.replace("return TemplateFactory", "grantItems(itemCache.get(id)); return TemplateFactory")
        self.assertIn(3, {row["line"] for row in findings("src/templates.java", changed)})

    def test_cache_keys_constants_and_write_receivers_are_not_value_reads(self):
        source = '''function purchase() {
  const cacheKey = makeCacheKey(id);
  cache.set(cacheKey, value, Shop.CACHE_TTL);
  cache.delete(makeCacheKey(id));
  this.cache = new Map();
  this.cache[id] = value;
  this.cache.price = value;
  this.cache[id].price = value;
  invalidateCache(cacheKey);
}'''
        self.assertFalse(findings("src/shop.ts", source))

    def test_comparisons_and_compound_assignments_still_read_cached_values(self):
        for expression in ["cached_price === price", "cached_price == price",
                           "cached_price != price", "cached_price += price",
                           "cache[id] += price", "cache.price == price", "grant(cached_price)",
                           "cache.get(makeCacheKey(id))"]:
            with self.subTest(expression=expression):
                self.assertTrue(findings("src/shop.ts", "function purchase() { " + expression + "; }"))


if __name__ == "__main__":
    unittest.main()
