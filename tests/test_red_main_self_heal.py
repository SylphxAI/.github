#!/usr/bin/env python3
"""Tests for the red-main self-heal rules (INFRA_PY and CONFIRM_PY in red-main.yml)."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/red-main.yml"


def embedded(name: str) -> str:
    lines = WORKFLOW.read_text().splitlines()
    start = lines.index(f"      {name}: |") + 1
    body = []
    for line in lines[start:]:
        if line.strip() and not line.startswith("        "):
            break
        body.append(line[8:])
    return "\n".join(body) + "\n"


INFRA = embedded("INFRA_PY")
CONFIRM = embedded("CONFIRM_PY")
VERDICT = embedded("VERDICT_PY")
MIGRATION = embedded("MIGRATION_PY")
HOLD = embedded("HOLD_PY")
PREV = embedded("PREV_PY")


def job(name, failed_steps=(), annotations=(), conclusion="failure"):
    steps = [{"name": "Checkout", "conclusion": "success"}]
    steps += [{"name": s, "conclusion": "failure"} for s in failed_steps]
    return {"name": name, "conclusion": conclusion, "steps": steps, "annotations": list(annotations)}


def classify(*jobs) -> str:
    return subprocess.run(
        [sys.executable, "-c", INFRA],
        input=json.dumps({"jobs": list(jobs)}),
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def confirm(prev_conclusion: str, prev_rows: str, cur_rows: str) -> str:
    with tempfile.TemporaryDirectory() as d:
        prev, cur = Path(d, "prev.tsv"), Path(d, "cur.tsv")
        prev.write_text(prev_rows)
        cur.write_text(cur_rows)
        return subprocess.run(
            [sys.executable, "-c", CONFIRM, prev_conclusion, str(prev), str(cur)],
            capture_output=True, text=True, check=True,
        ).stdout.strip()


class InfraTest(unittest.TestCase):
    def test_token_mint(self):
        self.assertEqual(classify(job("a", ["Mint installation token"])), "infra installation-token-mint")

    def test_upload_artifact(self):
        self.assertEqual(classify(job("a", ["Upload artifact"])), "infra upload-artifact")

    def test_cache_needs_403(self):
        self.assertEqual(classify(job("a", ["Restore cache"])), "none")
        self.assertEqual(
            classify(job("a", ["Restore cache"], ["Failed to restore: 403 Forbidden"])), "infra cache-403"
        )

    def test_runner_lost(self):
        self.assertEqual(
            classify(job("a", [], ["The hosted runner lost communication with the server. Verify the machine is running."])),
            "infra runner-lost",
        )
        self.assertEqual(
            classify(job("a", [], ["The self-hosted runner: sylphx-1 lost communication with the server. Verify."])),
            "infra runner-lost",
        )
        self.assertEqual(classify(job("a", [], ["The runner has received a shutdown signal."])), "infra runner-lost")

    def test_runner_lost_like_text_is_not_enough(self):
        self.assertEqual(classify(job("a", [], ["runner foo went offline"])), "none")
        self.assertEqual(classify(job("a", [], ["the test lost communication with the server"])), "none")

    def test_failed_test_step_plus_runner_lost_text_is_not_infra(self):
        for note in (
            "The runner has received a shutdown signal.",
            "The hosted runner lost communication with the server.",
            "runner x went offline",
        ):
            self.assertEqual(classify(job("a", ["cargo nextest"], [note])), "none")

    def test_test_failure_is_not_infra(self):
        self.assertEqual(classify(job("a", ["cargo nextest"])), "none")

    def test_mixed_jobs_are_not_infra(self):
        self.assertEqual(classify(job("a", ["Upload artifact"]), job("b", ["cargo nextest"])), "none")

    def test_test_step_beside_infra_step_is_not_infra(self):
        self.assertEqual(classify(job("a", ["cargo nextest", "Upload artifact"])), "none")

    def test_no_failed_jobs(self):
        self.assertEqual(classify(), "none")


def verdict(infra, state, conclusion="") -> str:
    return subprocess.run(
        [sys.executable, "-c", VERDICT, infra, state, conclusion], capture_output=True, text=True, check=True
    ).stdout.strip()


def previous(runs, created="2026-09-30T10:00:00Z") -> str:
    return subprocess.run(
        [sys.executable, "-c", PREV, created],
        input=json.dumps({"workflow_runs": runs}), capture_output=True, text=True, check=True,
    ).stdout.strip()


def run(id, conclusion, created, status="completed"):
    return {"id": id, "conclusion": conclusion, "created_at": created, "status": status, "html_url": f"u{id}"}


class VerdictTest(unittest.TestCase):
    def test_infra_rerun_passes_means_no_flake_and_no_revert(self):
        self.assertEqual(verdict("yes", "completed", "success"), "infra")

    def test_infra_rerun_fails_means_stop(self):
        self.assertEqual(verdict("yes", "completed", "failure"), "unknown")

    def test_infra_rerun_refused_or_timeout_means_stop(self):
        self.assertEqual(verdict("yes", "refused"), "unknown")
        self.assertEqual(verdict("yes", "timeout"), "unknown")

    def test_code_failure_paths_unchanged(self):
        self.assertEqual(verdict("no", "completed", "success"), "flake")
        self.assertEqual(verdict("no", "completed", "failure"), "real")
        self.assertEqual(verdict("no", "refused"), "real")
        self.assertEqual(verdict("no", "timeout"), "unknown")


class PreviousRunTest(unittest.TestCase):
    def test_cancelled_and_skipped_are_ignored(self):
        runs = [
            run(3, "cancelled", "2026-09-30T09:50:00Z"),
            run(2, "skipped", "2026-09-30T09:40:00Z"),
            run(1, "failure", "2026-09-30T09:00:00Z"),
        ]
        self.assertEqual(previous(runs), "1\tfailure\tu1")

    def test_newest_completed_before_this_run(self):
        runs = [
            run(4, "failure", "2026-09-30T10:30:00Z"),
            run(2, "success", "2026-09-30T09:40:00Z"),
            run(1, "failure", "2026-09-30T09:00:00Z"),
        ]
        self.assertEqual(previous(runs), "2\tsuccess\tu2")

    def test_unfinished_run_is_ignored(self):
        self.assertEqual(previous([run(2, None, "2026-09-30T09:40:00Z", "in_progress")]), "")

    def test_none_found(self):
        self.assertEqual(previous([]), "")


class ConfirmTest(unittest.TestCase):
    def test_previous_success(self):
        self.assertTrue(confirm("success", "", "test\tt1\tj\n").startswith("no "))

    def test_no_previous_run(self):
        self.assertTrue(confirm("", "", "test\tt1\tj\n").startswith("no "))

    def test_same_test_same_job(self):
        self.assertTrue(confirm("failure", "test\tt1\tj\n", "test\tt1\tj\n").startswith("yes "))

    def test_different_test(self):
        self.assertTrue(confirm("failure", "test\tt1\tj\n", "test\tt2\tj\n").startswith("no "))

    def test_same_test_different_job(self):
        self.assertTrue(confirm("failure", "test\tt1\tj1\n", "test\tt1\tj2\n").startswith("no "))

    def test_lane_row_matches_same_job(self):
        self.assertTrue(confirm("failure", "lane\tj\tj\n", "test\tt1\tj\n").startswith("yes "))

    def test_lane_rows_different_job(self):
        self.assertTrue(confirm("failure", "lane\tj1\tj1\n", "lane\tj2\tj2\n").startswith("no "))


GATE = embedded("GATE_PY")


def gate(on_red: str | None) -> str:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d, "sylphx.toml")
        line = f'on_red = "{on_red}"\n' if on_red else ""
        path.write_text(f'version = "1"\n[ci]\nmerge = "optimistic"\n{line}')
        return subprocess.run(
            [sys.executable, "-c", GATE, str(path), "yes"], capture_output=True, text=True, check=True
        ).stdout.strip()


class GateModeTest(unittest.TestCase):
    def test_modes(self):
        self.assertTrue(gate(None).startswith("act\tnotify"))
        self.assertTrue(gate("revert").startswith("act\trevert\t"))
        self.assertTrue(gate("revert_pr_unarmed").startswith("act\trevert_pr_unarmed\t"))

    def test_unknown_mode_is_an_error(self):
        self.assertTrue(gate("auto").startswith("error"))



def gate_full(extra: str) -> list[str]:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d, "sylphx.toml")
        path.write_text(f'version = "1"\n[ci]\nmerge = "optimistic"\n{extra}')
        return subprocess.run(
            [sys.executable, "-c", GATE, str(path), "yes"], capture_output=True, text=True, check=True
        ).stdout.strip().split("\t")


class OwnerLabelTest(unittest.TestCase):
    def test_default(self):
        self.assertEqual(gate_full("")[2], "owner:ops")

    def test_configured(self):
        self.assertEqual(gate_full('red_main_owner = "owner:cubeage-live"\n')[2], "owner:cubeage-live")

    def test_malformed_falls_back(self):
        for bad in ("cubeage-live", "owner:Bad Label", "owner:", "owner:a/b"):
            self.assertEqual(gate_full(f'red_main_owner = "{bad}"\n')[2], "owner:ops", bad)


def step_text(name: str) -> str:
    text = WORKFLOW.read_text()
    start = text.index(f"      - name: {name}\n")
    end = text.find("\n      - name: ", start + 1)
    return text[start:end if end != -1 else len(text)]


class UnarmedStaticTest(unittest.TestCase):
    def test_arming_and_enqueue_calls_are_exactly_where_expected(self):
        text = WORKFLOW.read_text()
        merges = [m.start() for m in re.finditer(r"gh pr merge", text)]
        enqueues = [m.start() for m in re.finditer(r"\{enqueuePullRequest\(", text)]
        self.assertEqual(len(merges), 2)
        self.assertEqual(len(enqueues), 1)
        quarantine = step_text("Mark the flaky units in their own source")
        revert = step_text("Revert the culprit or report it")
        q_start = text.index(quarantine)
        r_start = text.index(revert)
        q_merge = [m for m in merges if q_start <= m < q_start + len(quarantine)]
        r_merge = [m for m in merges if r_start <= m < r_start + len(revert)]
        self.assertEqual((len(q_merge), len(r_merge)), (1, 1))
        # quarantine: after its guard, and the guard exits
        guard = text.index('if [ "$MODE" != revert ] || [ "${REPO%%/*}" != SylphxAI ]; then', q_start)
        self.assertLess(guard, q_merge[0])
        self.assertIn("exit 0", text[guard:q_merge[0]])
        # revert: after the unarmed branch, which exits
        unarmed = text.index('if [ "$MODE" = revert_pr_unarmed ]; then\n            state_set revert-pr', r_start)
        self.assertLess(unarmed, r_merge[0])
        self.assertIn("exit 0", text[unarmed:r_merge[0]])
        self.assertLess(r_merge[0], enqueues[0])
        self.assertTrue(r_start <= enqueues[0] < r_start + len(revert))

    def test_unarmed_label_does_not_silence_other_breakage(self):
        body = step_text("Stop when the work is already done or already in hand")
        self.assertIn('if [ "$MODE" != revert_pr_unarmed ]; then', body)

    def test_standing_revert_for_same_culprit_stops_before_push(self):
        body = step_text("Revert the culprit or report it")
        guard = body.index('if [ "$MODE" = revert_pr_unarmed ]; then\n            open_prs=')
        self.assertIn('contains($sha)', body[guard:])
        self.assertLess(guard, body.index("push_branch \"$branch\""))
        self.assertLess(guard, body.index("git_c revert"))
        self.assertIn("exit 0", body[guard:body.index('branch="auto-revert/$SHORT_SHA"')])

    def test_standing_searches_trust_only_the_apps_own_items(self):
        body = step_text("Revert the culprit or report it")
        self.assertEqual(body.count("author,isCrossRepository"), 2)
        self.assertIn(".isCrossRepository == false", body)
        self.assertIn(".author.is_bot", body)
        self.assertIn('"app/" + $slug', body)
        self.assertIn('($slug + "[bot]")', body)
        self.assertIn('--arg slug "$APP_SLUG"', body)
        self.assertIn('has(\\"pull_request\\") | not', body)
        self.assertIn('.user.login == (\\"$APP_SLUG\\" + \\"[bot]\\")', body)
        self.assertEqual(body.count('(.body // '), 3)

    def test_alert_dedupe_is_keyed_on_the_culprit(self):
        body = step_text("Revert the culprit or report it")
        self.assertIn('contains(\\"$first\\")', body)
        self.assertNotIn('contains(\\"$HEAD_SHA\\")', body)



def migration(globs: str, *files: str) -> str:
    return subprocess.run(
        [sys.executable, "-c", MIGRATION, globs], input="\n".join(files) + "\n",
        capture_output=True, text=True, check=True,
    ).stdout.strip()


DEFAULT_GLOBS = "atlas/migrations/**,atlas.sum,**/atlas.sum"


class MigrationPathTest(unittest.TestCase):
    def test_migration_file(self):
        self.assertEqual(
            migration(DEFAULT_GLOBS, "src/a.ts", "atlas/migrations/20260930_add.sql"),
            "migration atlas/migrations/20260930_add.sql",
        )

    def test_atlas_sum_anywhere(self):
        self.assertEqual(migration(DEFAULT_GLOBS, "atlas.sum"), "migration atlas.sum")
        self.assertEqual(migration(DEFAULT_GLOBS, "svc/atlas.sum"), "migration svc/atlas.sum")

    def test_nested_migration_dir(self):
        self.assertEqual(migration(DEFAULT_GLOBS, "atlas/migrations/sub/x.sql"), "migration atlas/migrations/sub/x.sql")

    def test_ordinary_files_are_not_migrations(self):
        self.assertEqual(migration(DEFAULT_GLOBS, "src/atlas.ts", "docs/atlas/migrations.md", "atlas/other/x.sql"), "none")

    def test_repo_declared_dirs(self):
        self.assertEqual(migration("db/migrate/**", "db/migrate/001.rb"), "migration db/migrate/001.rb")
        self.assertEqual(migration("db/migrate/**", "atlas/migrations/x.sql"), "none")

    def test_single_star_stays_in_one_directory(self):
        self.assertEqual(migration("sql/*.sql", "sql/a.sql"), "migration sql/a.sql")
        self.assertEqual(migration("sql/*.sql", "sql/deep/a.sql"), "none")

    def test_no_files(self):
        self.assertEqual(migration(DEFAULT_GLOBS), "none")


class InfraRule2Test(unittest.TestCase):
    def test_runner_setup_and_checkout_are_infra(self):
        self.assertEqual(classify(job("a", ["Set up job"])), "infra runner-setup")
        self.assertEqual(classify(job("a", ["Checkout"])), "infra checkout")

    def test_infra_step_beside_a_test_failure_is_a_real_failure(self):
        self.assertEqual(classify(job("a", ["Checkout", "cargo nextest"])), "none")

    def test_infra_only_run_is_never_a_revert_verdict(self):
        # The rerun that fails again after an infra failure stops; it never
        # reaches `real`, so nothing is reverted.
        self.assertEqual(verdict("yes", "completed", "failure"), "unknown")


def hold(window: str, now: str) -> str:
    return subprocess.run(
        [sys.executable, "-c", HOLD, window, now], capture_output=True, text=True, check=True
    ).stdout.strip()


class HoldWindowTest(unittest.TestCase):
    def test_inside_and_outside(self):
        self.assertEqual(hold("19:00-22:00", "20:30"), "in")
        self.assertEqual(hold("19:00-22:00", "22:00"), "out")
        self.assertEqual(hold("19:00-22:00", "18:59"), "out")

    def test_wraps_midnight(self):
        self.assertEqual(hold("22:00-02:00", "23:30"), "in")
        self.assertEqual(hold("22:00-02:00", "01:00"), "in")
        self.assertEqual(hold("22:00-02:00", "12:00"), "out")

    def test_no_window(self):
        self.assertEqual(hold("", "20:00"), "out")


class GateFieldsTest(unittest.TestCase):
    def test_defaults_and_overrides(self):
        row = gate_full("")
        self.assertEqual(row[3], "atlas/migrations/**,atlas.sum,**/atlas.sum")
        self.assertEqual(row[4], "")
        row = gate_full('migration_paths = "db/migrate/**"\nrevert_hold_utc = "19:00-22:00"\n')
        self.assertEqual((row[3], row[4]), ("db/migrate/**", "19:00-22:00"))

    def test_malformed_values_fall_back(self):
        row = gate_full('migration_paths = "a b;rm"\nrevert_hold_utc = "evening"\n')
        self.assertEqual((row[3], row[4]), ("atlas/migrations/**,atlas.sum,**/atlas.sum", ""))


class RulesStaticTest(unittest.TestCase):
    def test_migration_hold_precedes_any_push_or_revert(self):
        body = step_text("Revert the culprit or report it")
        hold_at = body.index("migration.py")
        for needle in ('git_c revert', 'push_branch "$branch"', "gh pr merge", "{enqueuePullRequest("):
            self.assertLess(hold_at, body.index(needle), needle)
        self.assertIn("exit 0", body[hold_at:body.index("git_c revert")])

    def test_unreadable_files_hold(self):
        body = step_text("Revert the culprit or report it")
        self.assertIn('[ "$files_ok" = no ]', body)

    def test_notify_never_pushes(self):
        trace = step_text("Trace the culprit among the unverified commits")
        guard = trace.index('if [ "$MODE" = notify ]')
        self.assertLess(guard, trace.index('"repos/$REPO/git/refs"'))
        self.assertIn("exit 0", trace[guard:trace.index('"repos/$REPO/git/refs"')])
        cond = step_text("Mark the flaky units in their own source")
        self.assertIn("!= 'notify'", cond)

    def test_revert_notifies_owner_and_ops_and_files_followup(self):
        body = step_text("Revert the culprit or report it")
        self.assertIn("re-land after auto-revert", body)
        self.assertIn('OPS_ISSUE#\\#}/comments" --field body="$notice"', body)


class DispatchWithNothingRedTest(unittest.TestCase):
    def test_no_failed_run_is_quiet_not_an_error(self):
        body = step_text("Resolve the verify run that failed")
        self.assertIn('quiet "no failed run of $VERIFY_WORKFLOW on main was found to handle"', body)
        self.assertNotIn("was found to handle\"\n            exit 1", body)


if __name__ == "__main__":
    unittest.main()
