"""Backend tests for the read-only session change summary."""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import project_session_changes as sc

PID = "11111111-1111-1111-1111-111111111111"
OTHER_PID = "22222222-2222-2222-2222-222222222222"
SID = "a" * 32


def _write_sidecar(sidecar: Path, rows):
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(sidecar))
    conn.execute(
        "CREATE TABLE session_changes (session_id TEXT PRIMARY KEY,"
        " project_id TEXT NOT NULL, repo_path TEXT NOT NULL,"
        " baseline_commit TEXT, baseline_status TEXT NOT NULL DEFAULT '',"
        " baseline_diff TEXT NOT NULL DEFAULT '',"
        " latest_commit TEXT, latest_status TEXT NOT NULL DEFAULT '',"
        " latest_diff TEXT NOT NULL DEFAULT '',"
        " has_baseline INTEGER NOT NULL DEFAULT 1,"
        " reason TEXT NOT NULL DEFAULT '',"
        " created_at_ms INTEGER NOT NULL,"
        " updated_at_ms INTEGER NOT NULL,"
        " observed_at_ms INTEGER NOT NULL)"
    )
    for r in rows:
        conn.execute(
            "INSERT INTO session_changes (session_id, project_id, repo_path,"
            " baseline_commit, baseline_status, baseline_diff,"
            " latest_commit, latest_status, latest_diff,"
            " has_baseline, reason, created_at_ms, updated_at_ms,"
            " observed_at_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", r)
    conn.commit()
    conn.close()


def _write_sidecar_v2(sidecar: Path, rows):
    """Write a v2-schema sidecar (all current columns)."""
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(sidecar))
    conn.execute(
        "CREATE TABLE session_changes (session_id TEXT PRIMARY KEY,"
        " project_id TEXT NOT NULL, repo_path TEXT NOT NULL,"
        " repo_gitdir TEXT NOT NULL DEFAULT '',"
        " repo_status TEXT NOT NULL DEFAULT 'ok',"
        " baseline_commit TEXT, baseline_status TEXT NOT NULL DEFAULT '',"
        " baseline_diff TEXT NOT NULL DEFAULT '',"
        " baseline_complete INTEGER NOT NULL DEFAULT 1,"
        " latest_commit TEXT, latest_status TEXT NOT NULL DEFAULT '',"
        " latest_diff TEXT NOT NULL DEFAULT '',"
        " latest_complete INTEGER NOT NULL DEFAULT 1,"
        " commit_meta TEXT NOT NULL DEFAULT '',"
        " commit_patch TEXT NOT NULL DEFAULT '',"
        " commit_complete INTEGER NOT NULL DEFAULT 1,"
        " capture_note TEXT NOT NULL DEFAULT '',"
        " has_baseline INTEGER NOT NULL DEFAULT 1,"
        " reason TEXT NOT NULL DEFAULT '',"
        " created_at_ms INTEGER NOT NULL,"
        " updated_at_ms INTEGER NOT NULL,"
        " observed_at_ms INTEGER NOT NULL)"
    )
    for r in rows:
        conn.execute(
            "INSERT INTO session_changes (session_id, project_id, repo_path,"
            " repo_gitdir, repo_status, baseline_commit, baseline_status,"
            " baseline_diff, baseline_complete, latest_commit, latest_status,"
            " latest_diff, latest_complete, commit_meta, commit_patch,"
            " commit_complete, capture_note, has_baseline, reason,"
            " created_at_ms, updated_at_ms, observed_at_ms)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", r)
    conn.commit()
    conn.close()


def _v2_row(sid=SID, pid=PID, **over):
    row = {
        "session_id": sid, "project_id": pid, "repo_path": "/repo",
        "repo_gitdir": "/repo/.git", "repo_status": "ok",
        "baseline_commit": "ab" * 20, "baseline_status": "",
        "baseline_diff": "", "baseline_complete": 1,
        "latest_commit": "ab" * 20, "latest_status": "",
        "latest_diff": "", "latest_complete": 1,
        "commit_meta": "", "commit_patch": "", "commit_complete": 1,
        "capture_note": "", "has_baseline": 1, "reason": "",
        "created_at_ms": 1000, "updated_at_ms": 2000,
        "observed_at_ms": 2000,
    }
    row.update(over)
    return (row["session_id"], row["project_id"], row["repo_path"],
            row["repo_gitdir"], row["repo_status"], row["baseline_commit"],
            row["baseline_status"], row["baseline_diff"],
            row["baseline_complete"], row["latest_commit"],
            row["latest_status"], row["latest_diff"],
            row["latest_complete"], row["commit_meta"], row["commit_patch"],
            row["commit_complete"], row["capture_note"], row["has_baseline"],
            row["reason"], row["created_at_ms"], row["updated_at_ms"],
            row["observed_at_ms"])


def _activity_db_with_session(db: Path, session_id: str, project_id: str):
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE sessions (session_id TEXT PRIMARY KEY,"
        " project_id TEXT, project_name TEXT, start_ms INTEGER NOT NULL,"
        " end_ms INTEGER NOT NULL, first_activity_id INTEGER NOT NULL,"
        " last_activity_id INTEGER NOT NULL, event_count INTEGER NOT NULL,"
        " status TEXT NOT NULL, ended_reason TEXT, gap_ms INTEGER NOT NULL,"
        " interruption_ms INTEGER NOT NULL, device_id TEXT NOT NULL,"
        " unresolved_start_ms INTEGER, applications_json TEXT NOT NULL)")
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (session_id, project_id, "Alpha", 0, 100, 1, 1, 1, "closed",
         "inactivity", 1000, 100, "d" * 32, None, "[]"))
    conn.commit()
    conn.close()


class ValidationTests(unittest.TestCase):
    def test_bad_ids_raise(self):
        with self.assertRaises(sc.SessionChangesError):
            sc.session_changes("not-a-uuid", SID, db="/tmp/x.db")
        with self.assertRaises(sc.SessionChangesError):
            sc.session_changes(PID, "short", db="/tmp/x.db")

    def test_cli_requires_args(self):
        for args in (["changes"], ["changes", "--project", PID],
                     ["changes", "--session", SID]):
            completed = subprocess.run(
                [sys.executable, str(ROOT / "scripts"
                                    / "project_session_changes.py"), *args],
                text=True, capture_output=True, timeout=20, check=False)
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("error:", completed.stderr)


class ReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "activity.db"
        self.sidecar = Path(str(self.db) + ".changes.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_sidecar_unknown_session(self):
        value = sc.session_changes(PID, SID, db=str(self.db))
        self.assertFalse(value["available"])
        self.assertIn("reason", value)
        self.assertEqual(value["baseline_commit"], None)

    def test_missing_sidecar_known_session_names_predate(self):
        _activity_db_with_session(self.db, SID, PID)
        value = sc.session_changes(PID, SID, db=str(self.db))
        self.assertFalse(value["available"])
        self.assertIn("predate", value["reason"].lower())

    def test_baseline_row_is_change_only_evidence(self):
        base = "ab" * 20
        late = "cd" * 20
        _write_sidecar(self.sidecar, [(
            SID, PID, "/repo", base, "", "--- a/note.md\n+one",
            late, "M note.md", "--- a/note.md\n+one\n+two",
            1, "", 1000, 2000, 2000)])
        value = sc.session_changes(PID, SID, db=str(self.db))
        self.assertTrue(value["available"])
        self.assertTrue(value["has_baseline"])
        self.assertEqual(value["baseline_commit"], base)
        self.assertEqual(value["latest_commit"], late)
        self.assertIn("..", value["summary_key"])
        # Change-only: no window/app/activity vocabulary, bounded.
        blob = json.dumps(value).lower()
        self.assertNotIn("focused_window", blob)
        self.assertNotIn("application", blob)
        self.assertLessEqual(len(value["evidence"]), 8001)
        self.assertIn("last observed", value["evidence"].lower())

    def test_dirty_baseline_distinguished(self):
        base = "ab" * 20
        _write_sidecar(self.sidecar, [(
            SID, PID, "/repo", base, "M note.md", "dirty-before",
            base, "M note.md", "dirty-before\nmore",
            1, "", 1000, 2000, 2000)])
        value = sc.session_changes(PID, SID, db=str(self.db))
        self.assertTrue(value["available"])
        self.assertIn("already dirty", value["evidence"].lower())
        self.assertIn("not new work", value["evidence"].lower())

    def test_missing_baseline_never_fabricated(self):
        _write_sidecar(self.sidecar, [(
            SID, PID, "/repo", None, "", "",
            "cd" * 20, "M note.md", "diff",
            0, "baseline unavailable: session started before change capture",
            1000, 2000, 2000)])
        value = sc.session_changes(PID, SID, db=str(self.db))
        self.assertTrue(value["available"])
        self.assertFalse(value["has_baseline"])
        self.assertIsNone(value["baseline_commit"])
        self.assertEqual(value["summary_key"], "missing-baseline")
        self.assertIn("never backfilled", value["evidence"].lower()
                      .replace("was never backfilled", "never backfilled"))

    def test_no_repo_unavailable(self):
        _write_sidecar(self.sidecar, [(
            SID, PID, "", None, "", "", None, "", "",
            0, "no linked repository for project", 1000, 1000, 1000)])
        value = sc.session_changes(PID, SID, db=str(self.db))
        self.assertFalse(value["available"])
        self.assertEqual(value["summary_key"], "no-repo")

    def test_project_mismatch_refused(self):
        _write_sidecar(self.sidecar, [(
            SID, PID, "/repo", "ab" * 20, "", "", "ab" * 20, "", "",
            1, "", 1000, 1000, 1000)])
        value = sc.session_changes(OTHER_PID, SID, db=str(self.db))
        self.assertFalse(value["available"])
        self.assertIn("another project", value["reason"].lower())

    def test_sidecar_path_derivation(self):
        self.assertEqual(
            sc.sidecar_path_for(Path("/tmp/x/activity.db")),
            Path("/tmp/x/activity.db.changes.db"))

    def test_commit_range_evidence_shows_actual_changes(self):
        base, late = "ab" * 20, "cd" * 20
        _write_sidecar_v2(self.sidecar, [_v2_row(
            baseline_commit=base, latest_commit=late,
            commit_meta=f"{late[:12]} 1700000000 t session work",
            commit_patch="diff --git a/note.md b/note.md\n+session work")])
        value = sc.session_changes(PID, SID, db=str(self.db))
        self.assertTrue(value["available"])
        self.assertIn("session work", value["evidence"])
        self.assertIn("Committed during this session", value["evidence"])
        # Explicit attribution limitation always accompanies a baseline.
        self.assertIn("cannot be mechanically attributed",
                      value["evidence"].lower())

    def test_committed_baseline_appears_in_both_sections(self):
        base, late = "ab" * 20, "cd" * 20
        _write_sidecar_v2(self.sidecar, [_v2_row(
            baseline_commit=base, latest_commit=late,
            baseline_status="M note.md",
            baseline_diff="+dirty-before",
            latest_status="", latest_diff="",
            commit_meta=f"{late[:12]} 1700000000 t commit dirty",
            commit_patch="+dirty-before")])
        value = sc.session_changes(PID, SID, db=str(self.db))
        self.assertTrue(value["available"])
        ev = value["evidence"]
        self.assertEqual(ev.count("dirty-before"), 2)
        self.assertIn("appears in BOTH", ev)

    def test_partial_capture_note_surfaces(self):
        _write_sidecar_v2(self.sidecar, [_v2_row(
            latest_complete=0,
            capture_note="diff withheld (output cap)")])
        value = sc.session_changes(PID, SID, db=str(self.db))
        self.assertTrue(value["available"])
        self.assertIn("output cap", value["evidence"])

    def test_repo_mismatch_unavailable(self):
        _write_sidecar_v2(self.sidecar, [_v2_row(repo_status="mismatch")])
        value = sc.session_changes(PID, SID, db=str(self.db))
        self.assertFalse(value["available"])
        self.assertIn("never compared", value["reason"])

    def test_public_read_against_v1_sidecar(self):
        # v1 file (no v2 columns at all): the public reader works, treats
        # all completeness as unknown/partial, and never fabricates a
        # commit range for the A..B hashes it finds.
        base, late = "ab" * 20, "cd" * 20
        _write_sidecar(self.sidecar, [(
            SID, PID, "/repo", base, "M note.md", "patch-a",
            late, "M note.md", "patch-a",
            1, "", 1000, 2000, 2000)])
        value = sc.session_changes(PID, SID, db=str(self.db))
        self.assertTrue(value["available"])
        self.assertTrue(value["has_baseline"])
        self.assertEqual(value["baseline_commit"], base)
        self.assertEqual(value["latest_commit"], late)
        ev = value["evidence"]
        self.assertIn("partial capture", ev)
        self.assertIn("cannot be mechanically attributed", ev.lower())
        self.assertNotIn("migrated", ev.lower())

    def test_public_read_after_v1_migrate(self):
        # The collector's v1->v2 migration (simulated here with the same
        # statements): the historical limitation surfaces publicly.
        base = "ab" * 20
        _write_sidecar(self.sidecar, [(
            SID, PID, "/repo", base, "M note.md", "patch-a",
            base, "M note.md", "patch-a",
            1, "", 1000, 2000, 2000)])
        conn = sqlite3.connect(str(self.sidecar))
        for col, ddl in [
                ("repo_gitdir", "TEXT NOT NULL DEFAULT ''"),
                ("repo_status", "TEXT NOT NULL DEFAULT 'ok'"),
                ("commit_meta", "TEXT NOT NULL DEFAULT ''"),
                ("commit_patch", "TEXT NOT NULL DEFAULT ''"),
                ("baseline_complete", "INTEGER NOT NULL DEFAULT 1"),
                ("latest_complete", "INTEGER NOT NULL DEFAULT 1"),
                ("commit_complete", "INTEGER NOT NULL DEFAULT 1"),
                ("capture_note", "TEXT NOT NULL DEFAULT ''")]:
            conn.execute(f"ALTER TABLE session_changes ADD COLUMN {col} {ddl}")
        conn.execute(
            "UPDATE session_changes SET baseline_complete = 0,"
            " latest_complete = 0, commit_complete = 0,"
            " capture_note = 'migrated from sidecar v1: pre-v2 snapshots"
            " lack completeness provenance, treated as historical partial;"
            " baseline/latest/commit frozen as stored'")
        conn.commit()
        conn.close()
        value = sc.session_changes(PID, SID, db=str(self.db))
        self.assertTrue(value["available"])
        self.assertIn("migrated from sidecar v1", value["evidence"])
        self.assertIn("historical partial", value["evidence"])

    def test_limitation_survives_fully_populated_sections(self):
        # Every section over budget: the mandatory limitation must lead
        # the evidence and the whole text must stay within budget.
        big_status = ("M " + "f" * 100 + "\n") * 100
        big_diff = ("+line of session work\n") * 2000
        big_meta = ("ab12cd34ef56 1700000000 t subject\n") * 100
        big_patch = ("diff --git a/x b/x\n+work\n") * 2000
        _write_sidecar_v2(self.sidecar, [_v2_row(
            baseline_commit="ab" * 20, latest_commit="cd" * 20,
            baseline_status=big_status, baseline_diff=big_diff,
            latest_status=big_status, latest_diff=big_diff,
            commit_meta=big_meta, commit_patch=big_patch,
            capture_note="note " * 200, repo_path="/repo")])
        value = sc.session_changes(PID, SID, db=str(self.db))
        self.assertTrue(value["available"])
        ev = value["evidence"]
        self.assertLessEqual(len(ev), sc.MAX_EVIDENCE_CHARS)
        self.assertTrue(
            ev.startswith("Attribution limitation:"),
            f"evidence must lead with the disclaimer, got: {ev[:120]!r}")
        self.assertIn("cannot be mechanically attributed", ev.lower())
        self.assertIn("last observed state", ev.lower())


if __name__ == "__main__":
    unittest.main()
