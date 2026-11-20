"""The platform rename preserves exact policy scope, not just happy-path names."""
import copy
import datetime
import json
import pathlib
import tempfile
import unittest

from test_apply_merge_queue import FakeGh, SHIPPED_POLICY, amq, ruleset
from test_chat_senders import run_check
from test_game_cache import BAD, GitFixture, a, sc

NAMES = ("SylphxAI/cloud", "SylphxAI/sylphx")
OTHERS = ("Example/sylphx", "SylphxAI/sylphx-other", "SylphxAI/other")


class PlatformRenameTests(unittest.TestCase):
    def test_shipped_ownership_policies_accept_both_names_and_no_other_repository(self):
        today = datetime.date(2026, 11, 3)
        for path, decide, finding in (
            (sc.AGENT_RUNTIME_POLICY, sc.decide_agent_runtime,
             "agent-runtime-table services/agents/m/1.sql session_entries"),
            (sc.KNOWLEDGE_POLICY, sc.decide_knowledge,
             "knowledge-table services/knowledge/m/1.sql knowledge_nodes"),
        ):
            policy = json.loads(path.read_text())
            for repo in NAMES + OTHERS:
                with self.subTest(path=path, repo=repo):
                    errors, _ = decide([finding], repo, policy, today)
                    self.assertEqual(bool(errors), repo not in NAMES)

    def test_chat_senders_keep_both_path_and_repository_scope(self):
        for repo in NAMES + OTHERS:
            for path in ("services/notify/src/send.rs",
                         "services/ai/src/telegram_operator_bot/send.rs", "services/other/send.rs"):
                with self.subTest(repo=repo, path=path):
                    result = run_check('const API: &str = "https://api.telegram.org";\n',
                                       path, repository=repo)
                    self.assertEqual(result.returncode == 0,
                                     repo in NAMES and not path.startswith("services/other/"), result.stdout)

    def test_model_catalog_source_and_exemptions_remain_exact(self):
        for repo in NAMES + OTHERS:
            with self.subTest(repo=repo):
                data = {"version": 1, "aliases": ["example/model-latest"],
                        "source": {"repository": repo, "commit": "a" * 40,
                                   "path": "catalog/aliases.json", "sha256": "b" * 64}}
                with tempfile.TemporaryDirectory() as directory:
                    path = pathlib.Path(directory) / "aliases.json"
                    path.write_text(json.dumps(data))
                    if repo in NAMES:
                        self.assertEqual(sc.model_pins.load_aliases(path), frozenset(data["aliases"]))
                    else:
                        with self.assertRaises(ValueError):
                            sc.model_pins.load_aliases(path)
                self.assertEqual(bool(sc.model_pins.exemption(repo, "services/ai/migrations/1.sql")),
                                 repo in NAMES)
                self.assertIsNone(sc.model_pins.exemption(repo, "services/other/src/model.rs"))
                self.assertEqual(bool(a.downstream_pattern(repo)), repo in NAMES)
                self.assertEqual(a.owns_provider(repo, "services/auth/src/provider.rs", "auth"), repo in NAMES)

    def test_cache_domain_scope_through_required_cli_before_and_after_rename(self):
        for repo, path, expected in ((repo, path, expected) for repo in NAMES for path, expected in
                (("src/credentials.rs", 0), ("services/money/src/shop.rs", 1))):
            with self.subTest(repo=repo, path=path), tempfile.TemporaryDirectory() as directory:
                fixture = GitFixture(directory)
                fixture.write({path: BAD["rs"]})
                fixture.commit()
                result, report = fixture.check(repository=repo)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                self.assertEqual(any(row["rule"] == "cache-economy" for row in report["findings"]), bool(expected))
        for repo in OTHERS:
            self.assertTrue(a.cache_economy_applicability(repo, "src/credentials.rs"))

    def test_shipped_queue_settings_keep_the_platform_override_on_both_names(self):
        settings = {**SHIPPED_POLICY['settings'], 'max_entries_to_build': 8,
                    'min_entries_to_merge': 4, 'max_entries_to_merge': 8,
                    'min_entries_to_merge_wait_minutes': 2, 'check_response_timeout_minutes': 60}
        for repo in NAMES + OTHERS:
            with self.subTest(repo=repo):
                self.assertEqual(amq.desired_settings(SHIPPED_POLICY, repo, 'sylphx-merge-queue'),
                                 settings if repo in NAMES else SHIPPED_POLICY['settings'])
                self.assertEqual(amq.desired_settings(SHIPPED_POLICY, repo, 'unrelated-ruleset'),
                                 SHIPPED_POLICY['settings'])

    def test_queue_bindings_survive_rename_without_a_phantom_missing_target(self):
        policy = copy.deepcopy(SHIPPED_POLICY)
        # Mechanism fixture: one aliased, scoped target.
        target = {"repo": NAMES[0], "repo_aliases": [NAMES[1]], "ruleset": "sylphx-merge-queue",
                  "ruleset_id": 20321070, "reason": "fixture",
                  "checks": [{"context": "ci-ok", "integration_id": 15368},
                             {"context": "external-ok", "integration_id": 5155317}]}
        policy["required_checks"] = [target]
        for repo in NAMES:
            with self.subTest(repo=repo):
                gh = FakeGh({repo: ruleset(target["ruleset_id"], target["ruleset"], policy["settings"], reviews=0)})
                fleet = amq.read_org(gh, "SylphxAI")
                rows = amq.plan(fleet, policy, {}, {repo})
                self.assertFalse(any(row["status"] == "MISSING" for row in rows), rows)
                row = next(row for row in rows if row["id"] == target["ruleset_id"])
                self.assertEqual(row["changes"]["required_status_checks"][1], target["checks"])
                with tempfile.TemporaryDirectory() as directory:
                    results = amq.apply(gh, rows, policy, pathlib.Path(directory), pause=0)
                self.assertEqual(results[0]["result"], "APPLIED")
                self.assertEqual(amq.desired_checks(policy, repo, target["ruleset_id"], target["ruleset"]), target["checks"])
                self.assertEqual(amq.expected_gate(policy, repo, "sylphx-deploy-pins"), "pin-ok")
        for repo in OTHERS:
            self.assertEqual(amq.desired_checks(policy, repo, target["ruleset_id"], target["ruleset"]), [])
            self.assertEqual(amq.expected_gate(policy, repo, "sylphx-deploy-pins"), "ci-ok")
        self.assertEqual(amq.desired_checks(policy, NAMES[1], -1, target["ruleset"]), [])
        with self.assertRaises(ValueError):
            amq.desired_checks(policy, NAMES[1], target["ruleset_id"], "unrelated-ruleset")


if __name__ == "__main__":
    unittest.main()
