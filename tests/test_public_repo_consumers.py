#!/usr/bin/env python3
"""Tests for scripts/public_repo_consumers.py, on fixture repository lists (no network)."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("public_repo_consumers", ROOT / "scripts" / "public_repo_consumers.py")
prc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prc)


def repo(name, visibility="public", archived=False, fork=False, description=None, **props):
    return {"name": name, "visibility": visibility, "archived": archived, "fork": fork,
            "description": description, "pushed_at": "2026-10-06T00:00:00Z",
            "html_url": f"https://github.com/SylphxAI/{name}", "custom_properties": props}


def pkg(name, repo_url, registry="npm", marked=False):
    return {"registry": registry, "name": name, "repo_url": repo_url, "marked": marked}


USED = repo("infra-tool", sylphx_consumer="SylphxAI/infra infra/addons/x")


def saved(data):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(data, fh)
    return fh.name


def run(repos, *extra, packages=(), readmes=None):
    files = [saved(repos), saved(list(packages)), saved(readmes or {})]
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = prc.main(["--input", files[0], "--packages", files[1], "--readmes", files[2], *extra])
    for f in files:
        pathlib.Path(f).unlink()
    return code, out.getvalue(), err.getvalue()


class MissingConsumers(unittest.TestCase):
    def test_all_recorded_is_green(self):
        code, out, _ = run([USED, repo("site", sylphx_consumer="SylphxAI/cloud platform.projects.toml")])
        self.assertEqual(code, 0)
        self.assertIn("0 public SylphxAI repositories", out)

    def test_public_repo_without_consumer_is_listed(self):
        code, out, _ = run([USED, repo("lonely-tool"), repo("blank", sylphx_consumer="  ")])
        self.assertEqual(code, 1)
        self.assertIn("MISSING SylphxAI/blank", out)
        self.assertIn("MISSING SylphxAI/lonely-tool", out)
        self.assertNotIn("infra-tool", out)

    def test_private_archived_and_delivered_are_out_of_scope(self):
        repos = [USED, repo("secret", visibility="private"), repo("old", archived=True),
                 repo("customer-site", sylphx_delivery="delivered")]
        self.assertEqual(prc.missing_consumers(repos), [])

    def test_fork_is_listed_and_marked(self):
        code, out, _ = run([USED, repo("upstream-fork", fork=True)])
        self.assertEqual(code, 1)
        self.assertIn("MISSING SylphxAI/upstream-fork (fork) pushed", out)

    def test_json_output_and_paged_input(self):
        code, out, _ = run([[USED], [repo("b-tool"), repo("A-tool")]], "--json")
        self.assertEqual(code, 1)
        self.assertEqual([r["name"] for r in json.loads(out)["missing"]], ["A-tool", "b-tool"])

    def test_unreadable_properties_fail_closed(self):
        # A credential without custom-property access sees {} everywhere: never "all missing", never green.
        code, out, err = run([repo("a"), repo("b")])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("UNREADABLE", err)

    def test_empty_list_fails_closed(self):
        code, _, err = run([repo("secret", visibility="private")])
        self.assertEqual(code, 2)
        self.assertIn("no public repositories", err)


def no_resolve(owner, name):
    raise AssertionError(f"unexpected GitHub read for {owner}/{name}")


class ArchivedNotices(unittest.TestCase):
    def test_archived_repo_with_status_in_description_is_green(self):
        repos = [USED, repo("lens", archived=True, description="Archived 2026-09-24 - no longer maintained"),
                 repo("locus", archived=True, description="Merged into repomap (github.com/SylphxAI/repomap)"),
                 repo("rag", archived=True, description="DEPRECATED - use coderag instead")]
        code, out, _ = run(repos)
        self.assertEqual(code, 0)
        self.assertIn("0 archived with no notice", out)

    def test_archived_repo_without_notice_is_listed(self):
        code, out, _ = run([USED, repo("craft", archived=True, description="The fastest immutable state library"),
                            repo("molt", archived=True)])
        self.assertEqual(code, 1)
        self.assertIn("NO-NOTICE SylphxAI/craft", out)
        self.assertIn("NO-NOTICE SylphxAI/molt", out)

    def test_readme_notice_counts_only_near_the_top(self):
        archived = [repo("a", archived=True, description="A tool"), repo("b", archived=True, description="B tool")]
        readmes = {"a": "# A\n\n> **This project is no longer maintained.**\n",
                   "b": "# B\n" + "x" * prc.README_HEAD + "\narchived"}
        self.assertEqual([r["name"] for r in prc.missing_notices(archived, readmes.get)], ["b"])

    def test_readme_is_read_only_without_a_description_notice(self):
        archived = [repo("a", archived=True, description="Retired 2026-09-25")]

        def readme(name):
            raise AssertionError("README read although the description has a notice")

        self.assertEqual(prc.missing_notices(archived, readme), [])

    def test_words_inside_other_words_are_not_a_notice(self):
        self.assertFalse(prc.has_notice("Unarchivedness and resunset tools"))
        self.assertTrue(prc.has_notice("Sunset: use the platform SDK"))

    def test_forks_private_and_delivered_archives_are_out_of_scope(self):
        repos = [repo("f", archived=True, fork=True), repo("p", archived=True, visibility="private"),
                 repo("d", archived=True, sylphx_delivery="delivered")]
        self.assertEqual(prc.archived_public(repos), [])


class UndeprecatedPackages(unittest.TestCase):
    ARCHIVED = [repo("silk", archived=True), repo("effect", archived=True)]

    def test_live_package_from_archived_repo_is_listed(self):
        packages = [pkg("@sylphx/silk", "git+https://github.com/SylphxAI/silk.git"),
                    pkg("@sylphx/lens", "git+https://github.com/SylphxAI/silk.git", marked=True),
                    pkg("@sylphx/sdk", "https://github.com/SylphxAI/cloud"),
                    pkg("kernox", "https://github.com/SylphxAI/silk", registry="crates")]
        got = prc.undeprecated(packages, "SylphxAI", self.ARCHIVED, no_resolve)
        self.assertEqual([(p["registry"], p["name"], p["repo"]) for p in got],
                         [("crates", "kernox", "silk"), ("npm", "@sylphx/silk", "silk")])

    def test_renamed_source_is_resolved_through_the_redirect(self):
        packages = [pkg("effect_dart", "https://github.com/sylphxltd/effect", registry="pub"),
                    pkg("personal", "https://github.com/someone/effect")]
        seen = []

        def resolve(owner, name):
            seen.append(owner)
            return "SylphxAI/effect" if owner == "sylphxltd" else f"{owner}/{name}"

        got = prc.undeprecated(packages, "SylphxAI", self.ARCHIVED, resolve)
        self.assertEqual([p["name"] for p in got], ["effect_dart"])
        self.assertEqual(seen, ["sylphxltd", "someone"])

    def test_unrelated_other_owner_costs_no_github_read(self):
        packages = [pkg("x", "https://github.com/someone/unrelated"), pkg("y", None), pkg("z", "https://example.com")]
        self.assertEqual(prc.undeprecated(packages, "SylphxAI", self.ARCHIVED, no_resolve), [])

    def test_github_url_forms(self):
        for url in ("git+https://github.com/SylphxAI/silk.git", "git@github.com:SylphxAI/silk.git",
                    "https://github.com/SylphxAI/silk/tree/main/packages/a", "github.com/SylphxAI/silk#readme"):
            self.assertEqual(prc.github_repo(url), ("SylphxAI", "silk"), url)

    def test_report_lines_and_exit(self):
        code, out, _ = run([USED, repo("silk", archived=True, description="Archived: no longer maintained.")],
                           packages=[pkg("@sylphx/silk", "https://github.com/SylphxAI/silk")])
        self.assertEqual(code, 1)
        self.assertIn("UNDEPRECATED npm @sylphx/silk from archived SylphxAI/silk", out)
        self.assertIn("1 packages from archived repositories not deprecated", out)

    def test_json_carries_all_three_lists(self):
        code, out, _ = run([USED, repo("molt", archived=True)], "--json",
                           packages=[pkg("@sylphx/molt", "https://github.com/SylphxAI/molt")])
        self.assertEqual(code, 1)
        doc = json.loads(out)
        self.assertEqual(doc["missing"], [])
        self.assertEqual([r["name"] for r in doc["archived_without_notice"]], ["molt"])
        self.assertEqual([p["name"] for p in doc["undeprecated_packages"]], ["@sylphx/molt"])


class YankCrates(unittest.TestCase):
    LISTED = [{"registry": "crates", "name": "kernox", "repo": "kernox"},
              {"registry": "pub", "name": "effect_dart", "repo": "effect"}]

    def test_yanks_each_unyanked_version_and_keeps_other_registries(self):
        calls = []

        def send(method, url, token):
            calls.append((method, url, token))
            return 200

        versions = lambda name: [{"num": "0.0.1", "yanked": False}, {"num": "0.0.0", "yanked": True}]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            left = prc.yank_crates(self.LISTED, "tok", versions, send, pause=0)
        self.assertEqual([p["name"] for p in left], ["effect_dart"])
        self.assertEqual(calls, [("DELETE", "https://crates.io/api/v1/crates/kernox/0.0.1/yank", "tok")])
        self.assertIn("YANKED crates kernox 0.0.1", out.getvalue())

    def test_failed_yank_stays_listed(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            left = prc.yank_crates(self.LISTED, "tok", lambda n: [{"num": "0.0.1", "yanked": False}],
                                   lambda m, u, t: 403, pause=0)
        self.assertEqual([p["name"] for p in left], ["kernox", "effect_dart"])
        self.assertIn("HTTP 403", err.getvalue())

    def test_no_token_calls_nothing_and_stays_listed(self):
        def boom(*a):
            raise AssertionError("no call without a token")

        with contextlib.redirect_stderr(io.StringIO()):
            left = prc.yank_crates(self.LISTED, None, boom, boom, pause=0)
        self.assertEqual(len(left), 2)


class Workflow(unittest.TestCase):
    def test_reads_with_the_job_token_not_an_app_key(self):
        # Public repositories, their custom properties and READMEs are public; an App
        # key is not shared with this repository, and the mint step failed.
        text = (ROOT / ".github" / "workflows" / "public-repo-consumers.yml").read_text()
        self.assertIn("GH_TOKEN: ${{ github.token }}", text)
        self.assertNotIn("create-github-app-token", text)
        self.assertNotIn("SYLPHX_BUILDER", text)

    def test_weekly_run_yanks_crates_with_the_org_token(self):
        text = (ROOT / ".github" / "workflows" / "public-repo-consumers.yml").read_text()
        self.assertIn("CARGO_REGISTRY_TOKEN: ${{ secrets.CARGO_REGISTRY_TOKEN }}", text)
        self.assertIn("--fix-crates", text)


if __name__ == "__main__":
    unittest.main()
