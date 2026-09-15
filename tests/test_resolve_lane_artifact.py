#!/usr/bin/env python3
"""Tests for the image lane's transfer-artifact resolution.

Failure mode under test (live 2026-09-15, hands run 34986604012): the publish
leg recomputed the compile leg's attempt-scoped artifact name from
`github.run_attempt`, so a partial re-run ("Re-run failed jobs") looked for
`…-34986604012-2` while the run only ever produced `…-34986604012-1`, and the
only recovery action on the release path failed closed with
`Unable to download artifact(s): Artifact not found`.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(module_name: str, relative: str):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


resolve = load("lane_resolve_artifact", "scripts/resolve-lane-artifact.py")

PREFIX = "image-lane-image-0c15bb56b29d53210b1ebf3fbbb8f67d0f91656b-34986604012"


def artifact(name: str, expired: bool = False) -> dict:
    return {"name": name, "expired": expired}


class SelectArtifactTest(unittest.TestCase):
    def test_partial_rerun_uses_the_only_attempt_that_ran(self) -> None:
        """Re-run of the failed publish job: the run still holds attempt 1."""
        artifacts = [artifact(f"{PREFIX}-1")]
        name, notes = resolve.select_artifact(artifacts, PREFIX, 2)
        self.assertEqual(name, f"{PREFIX}-1")
        self.assertTrue(notes and "attempt 1" in notes[0])

    def test_full_rerun_prefers_this_attempt(self) -> None:
        """A full re-run rebuilt the image; the publish leg must use attempt 3."""
        artifacts = [artifact(f"{PREFIX}-1"), artifact(f"{PREFIX}-3")]
        name, notes = resolve.select_artifact(artifacts, PREFIX, 3)
        self.assertEqual(name, f"{PREFIX}-3")
        self.assertEqual(notes, [])

    def test_newest_attempt_wins_when_this_attempt_produced_none(self) -> None:
        artifacts = [artifact(f"{PREFIX}-1"), artifact(f"{PREFIX}-2")]
        name, _ = resolve.select_artifact(artifacts, PREFIX, 4)
        self.assertEqual(name, f"{PREFIX}-2")

    def test_expired_this_attempt_falls_back_to_the_newest_live_one(self) -> None:
        artifacts = [artifact(f"{PREFIX}-1"), artifact(f"{PREFIX}-2", expired=True)]
        name, notes = resolve.select_artifact(artifacts, PREFIX, 2)
        self.assertEqual(name, f"{PREFIX}-1")
        self.assertTrue(any("expired" in note for note in notes))

    def test_fails_closed_when_the_only_candidate_is_expired(self) -> None:
        artifacts = [artifact(f"{PREFIX}-1", expired=True)]
        with self.assertRaises(SystemExit):
            resolve.select_artifact(artifacts, PREFIX, 2)

    def test_fails_closed_when_nothing_matches(self) -> None:
        artifacts = [
            artifact("image-lane-image-othersha-34986604012-1"),
            artifact(f"{PREFIX}-latest"),
        ]
        with self.assertRaises(SystemExit):
            resolve.select_artifact(artifacts, PREFIX, 2)

    def test_ambiguous_top_attempt_fails_closed(self) -> None:
        artifacts = [artifact(f"{PREFIX}-01"), artifact(f"{PREFIX}-1")]
        with self.assertRaises(SystemExit):
            resolve.select_artifact(artifacts, PREFIX, 2)

    def test_other_source_shas_never_match(self) -> None:
        artifacts = [
            artifact(
                "image-lane-image-1111111111111111111111111111111111111111-34986604012-9"
            ),
            artifact(f"{PREFIX}-1"),
        ]
        name, _ = resolve.select_artifact(artifacts, PREFIX, 2)
        self.assertEqual(name, f"{PREFIX}-1")


class WorkflowWiringTest(unittest.TestCase):
    """Structural guard: the publish leg must not recompute attempt names."""

    def setUp(self) -> None:
        self.workflow = (ROOT / ".github/workflows/image-lane.yml").read_text(
            encoding="utf-8"
        )

    def test_no_download_step_embeds_run_attempt(self) -> None:
        lines = self.workflow.splitlines()
        for index, line in enumerate(lines):
            if "uses: actions/download-artifact" not in line:
                continue
            block = "\n".join(lines[index : index + 8])
            self.assertNotIn(
                "github.run_attempt",
                block,
                f"download-artifact at line {index + 1} recomputes the attempt name:\n{block}",
            )

    def test_both_transfers_are_resolved_then_downloaded(self) -> None:
        for variable in ("image_transfer", "evidence_transfer"):
            self.assertIn(f"id: {variable}", self.workflow)
            self.assertIn(
                f"${{{{ steps.{variable}.outputs.name }}}}", self.workflow
            )
        self.assertEqual(self.workflow.count("resolve-lane-artifact.py"), 2)


if __name__ == "__main__":
    unittest.main()
