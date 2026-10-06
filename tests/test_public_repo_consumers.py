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


def repo(name, visibility="public", archived=False, fork=False, **props):
    return {"name": name, "visibility": visibility, "archived": archived, "fork": fork,
            "pushed_at": "2026-10-06T00:00:00Z", "html_url": f"https://github.com/SylphxAI/{name}",
            "custom_properties": props}


USED = repo("infra-tool", sylphx_consumer="SylphxAI/infra infra/addons/x")


def run(repos, *extra):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(repos, fh)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = prc.main(["--input", fh.name, *extra])
    pathlib.Path(fh.name).unlink()
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


if __name__ == "__main__":
    unittest.main()
