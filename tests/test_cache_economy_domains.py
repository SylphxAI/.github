"""Player economy scope is separate from credentials, infrastructure and leases."""
import hashlib
import json
import pathlib
import tempfile
import unittest

from test_game_cache import BAD, GitFixture, a, sc


class CacheEconomyDomainTests(unittest.TestCase):
    def rows(self, repository, path, source):
        return [row for row in a.scan_file(path, source, repository, set(), [])
                if row["rule"] == "cache-economy"]

    def test_non_player_repository_profile_is_not_a_read_waiver(self):
        self.assertFalse(self.rows("SylphxAI/spiron", "src/handler.rs", BAD["rs"]))
        self.assertTrue(self.rows("Example/game", "src/handler.rs", BAD["rs"]))
        profile = json.loads(pathlib.Path(a.__file__).with_name("cache-economy-profiles.json").read_text())
        self.assertEqual(profile["repositories"]["SylphxAI/spiron"]["applicability"], "not-applicable")
        self.assertNotIn("reads", profile)

    def test_cloud_money_and_entitlement_paths_still_refuse_cached_authority(self):
        for path in ["services/money/src/shop.rs", "crates/sylphx-money/src/grants.rs",
                     "crates/sylphx-apps-api/src/capabilities/money/purchase.rs",
                     "kernel/billing/src/rewards.rs", "src/entitlements.rs",
                     "src/entitlement/grants.rs"]:
            with self.subTest(path=path):
                self.assertTrue(self.rows("SylphxAI/cloud", path, BAD["rs"]))

    def test_infrastructure_and_authentication_are_outside_cloud_economy_scope(self):
        cases = [
            ("crates/api/src/product/auth.rs", "fn decode_signed_access_claims() { cached_verifying_key(kid, now) }"),
            ("crates/controller/src/runner.rs", "fn grant_runner() { linux_ci_runner_workspace_cache_env() }"),
            ("crates/controller/src/stream.rs", 'fn claim_callback() { redis::cmd("DEL"); }'),
            ("kernel/hands/src/github.rs", "fn installation_grants() { cache.read().get(id); }"),
        ]
        for path, source in cases:
            with self.subTest(path=path):
                self.assertFalse(self.rows("SylphxAI/cloud", path, source))

    def test_non_economy_profile_does_not_skip_other_rules(self):
        rows = a.scan_file("src/auth.ts", "if (first_party) { return true; }",
                           "SylphxAI/spiron", set(), [])
        self.assertTrue(any(row["rule"] == "privileged-branch" for row in rows))

    def test_profiles_and_money_refusal_through_actual_required_cli(self):
        cases = [("SylphxAI/spiron", "src/claims.rs", 0),
                 ("SylphxAI/cloud", "src/credentials.rs", 0),
                 ("SylphxAI/cloud", "services/money/src/shop.rs", 1),
                 ("Example/game", "src/shop.rs", 1)]
        for repository, path, expected in cases:
            with self.subTest(repository=repository, path=path), tempfile.TemporaryDirectory() as directory:
                fixture = GitFixture(directory)
                fixture.write({path: BAD["rs"]})
                fixture.commit()
                result, report = fixture.check(repository=repository)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                self.assertEqual(any(row["rule"] == "cache-economy" for row in report["findings"]), bool(expected))

    def test_domain_policy_is_in_new_profile_but_absent_from_historical_profile(self):
        policy_path = sc.adoption.PREFIX + "cache-economy-profiles.json"
        tree = sc.Tree(str(pathlib.Path(a.__file__).resolve().parents[3]), "HEAD")
        files = {path: tree.read(path) for path in tree.paths()
                 if (path.startswith(sc.adoption.PREFIX) or path == "policy/deal-randomness-scope.json")
                 and path != policy_path}
        old = sc.adoption.source_profile(sc.DictTree(files))
        self.assertNotIn(policy_path, old["sources"])
        policy = pathlib.Path(a.__file__).with_name("cache-economy-profiles.json").read_text()
        files[policy_path] = policy
        current = sc.adoption.source_profile(sc.DictTree(files))
        self.assertEqual(current["sources"][policy_path], hashlib.sha256(policy.encode()).hexdigest())
        self.assertEqual(current["rules"], old["rules"])
        self.assertNotEqual(sc.adoption.profile_id(current), sc.adoption.profile_id(old))


if __name__ == "__main__":
    unittest.main()
