"""Backend tests for the compact project overview (scripts/project_overview.py).

The change-only recap contract (owned by another worker) is
``project_session_changes.session_changes(project_id, session_id)`` ->
``{available, reason, baseline_commit, latest_commit, summary_key,
evidence}``. The module may not exist yet, so these tests mock the
contract at ``overview._psc`` and never touch real session persistence.
"""

import json
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import project_overview as overview

PID = "11111111-1111-1111-1111-111111111111"
SID = "a" * 32
BASELINE = "b" * 40
LATEST = "c" * 40


def run_cli(args):
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "project_overview.py"), *args],
        text=True, capture_output=True, timeout=20, check=False,
    )
    return completed


def make_sidecar(changes=None):
    """Stub sidecar module exposing session_changes(); records calls."""
    calls = []

    def session_changes(project_id, session_id):
        calls.append((project_id, session_id))
        if isinstance(changes, Exception):
            raise changes
        return dict(changes)

    stub = SimpleNamespace(session_changes=session_changes)
    return stub, calls


CHANGES_OK = {
    "available": True, "reason": "",
    "project_id": PID, "session_id": SID,
    "baseline_commit": BASELINE, "latest_commit": LATEST,
    "has_baseline": True, "summary_key": "key-1",
    "evidence": "M foo.py\n+M bar.py",
    "repo_path": "/repo", "observed_at_ms": 1, "updated_at_ms": 2,
    "limitations": "last observed state",
}


class ValidationTests(unittest.TestCase):
    def test_bad_project_id_rejected(self):
        with self.assertRaises(overview.OverviewError):
            overview.overview_for_project("not-a-uuid")
        with self.assertRaises(overview.OverviewError):
            overview.overview_for_project("")

    def test_bad_sessions_limit_rejected(self):
        with self.assertRaises(overview.OverviewError):
            overview.overview_for_project(PID, sessions_limit=0)
        with self.assertRaises(overview.OverviewError):
            overview.overview_for_project(PID, sessions_limit=101)

    def test_cli_requires_project(self):
        completed = run_cli(["overview"])
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("error:", completed.stderr)

    def test_cli_rejects_bad_uuid(self):
        completed = run_cli(["overview", "--project", "nope"])
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("error:", completed.stderr)


class OverviewAggregationTests(unittest.TestCase):
    def _ok_result(self, sessions_limit=20, changes=None):
        todos_view = {
            "todos": [
                {"line": 1, "task": "open one", "marker": "TODO", "done": False},
                {"line": 2, "task": "done one", "marker": "DONE", "done": True},
            ],
            "reason": "",
        }
        sessions = [
            {"session_id": SID, "start_ms": 0, "end_ms": 60000,
             "event_count": 3, "status": "closed"},
            {"session_id": "b" * 32, "start_ms": 0, "end_ms": 120000,
             "event_count": 5, "status": "closed"},
        ]
        last = {"session_id": SID, "start_ms": 0, "end_ms": 60000,
                "event_count": 3, "status": "closed"}
        stub, calls = make_sidecar(
            CHANGES_OK if changes is None else changes)
        with patch.object(overview._dp, "_resolve_explicit_identity",
                           return_value=({"id": PID, "name": "Alpha",
                                          "matched_by": ""},
                                         {"id": PID, "name": "Alpha"},
                                         "associated", "")), \
             patch.object(overview._dp, "project_todos",
                          return_value=todos_view), \
             patch.object(overview._dp, "work_sessions",
                          return_value={"sessions": sessions,
                                        "count": 2, "reason": ""}), \
             patch.object(overview._dp, "last_session",
                          return_value={"session": last, "reason": ""}), \
             patch.object(overview, "_psc", stub):
            value = overview.overview_for_project(
                PID, sessions_limit=sessions_limit)
        return value, calls

    def test_todos_reuse_parser_counts(self):
        value, _calls = self._ok_result()
        self.assertTrue(value["todos"]["available"])
        self.assertEqual(value["todos"]["open_count"], 1)
        self.assertEqual(value["todos"]["total_count"], 2)
        self.assertEqual(value["todos"]["open"][0]["task"], "open one")

    def test_work_time_sums_bounded_window_with_explicit_scope(self):
        value, _calls = self._ok_result(sessions_limit=20)
        work = value["work_time"]
        self.assertTrue(work["available"])
        self.assertEqual(work["total_ms"], 180000)
        self.assertEqual(work["count"], 2)
        # Explicit bounded scope; never a fabricated all-time claim.
        self.assertIn("last 20", work["scope_label"])
        self.assertIn("showing 2", work["scope_label"])
        self.assertNotIn("all-time", json.dumps(work).lower())
        self.assertNotIn("all time", json.dumps(work).lower())

    def test_last_session_carries_change_evidence_only(self):
        value, calls = self._ok_result()
        last = value["last_session"]
        self.assertTrue(last["available"])
        self.assertEqual(last["session"]["session_id"], SID)
        # The sidecar is called once with (project_id, session_id).
        self.assertEqual(calls, [(PID, SID)])
        changes = last["changes"]
        self.assertTrue(changes["available"])
        self.assertEqual(changes["baseline_commit"], BASELINE)
        self.assertEqual(changes["latest_commit"], LATEST)
        self.assertEqual(changes["summary_key"], "key-1")
        self.assertIn("foo.py", changes["evidence"])
        # No app/window activity in the recap path.
        self.assertNotIn("events", last)
        self.assertNotIn("snapshot", json.dumps(last))

    def test_missing_baseline_is_explicit_unavailable(self):
        value, _calls = self._ok_result(changes={
            "available": False, "reason": "no baseline commit for this session",
            "baseline_commit": "", "latest_commit": "", "has_baseline": False,
            "summary_key": "", "evidence": ""})
        changes = value["last_session"]["changes"]
        self.assertFalse(changes["available"])
        self.assertIn("baseline", changes["reason"].lower())
        # Session identity is still present; only the recap is unavailable.
        self.assertEqual(value["last_session"]["session"]["session_id"], SID)

    def test_upgrade_session_latest_only_never_recaps(self):
        # Real sidecar shape for a session that started before capture
        # shipped: available data, has_baseline false, latest-only
        # evidence. Must surface as recap-unavailable, never a baseline.
        value, _calls = self._ok_result(changes={
            "available": True, "reason": "",
            "baseline_commit": None, "latest_commit": LATEST,
            "has_baseline": False, "summary_key": "missing-baseline",
            "evidence": "Latest observed worktree status",
            "repo_path": "/repo", "limitations": "last observed state"})
        changes = value["last_session"]["changes"]
        self.assertFalse(changes["available"])
        self.assertFalse(changes["has_baseline"])
        self.assertIn("baseline", changes["reason"].lower())

    def test_sidecar_module_missing_is_explicit(self):
        with patch.object(overview._dp, "_resolve_explicit_identity",
                           return_value=({"id": PID, "name": "Alpha",
                                          "matched_by": ""},
                                         {"id": PID, "name": "Alpha"},
                                         "associated", "")), \
             patch.object(overview._dp, "project_todos",
                          return_value={"todos": [], "reason": ""}), \
             patch.object(overview._dp, "work_sessions",
                          return_value={"sessions": [], "count": 0,
                                        "reason": "empty"}), \
             patch.object(overview._dp, "last_session",
                          return_value={"session": {"session_id": SID},
                                        "reason": ""}), \
             patch.object(overview, "_psc", None):
            value = overview.overview_for_project(PID)
        changes = value["last_session"]["changes"]
        self.assertFalse(changes["available"])
        self.assertIn("reason", changes)

    def test_sidecar_exception_keeps_other_sections(self):
        value, _calls = self._ok_result(
            changes=RuntimeError("sidecar boom"))
        self.assertFalse(value["last_session"]["changes"]["available"])
        self.assertTrue(value["todos"]["available"])
        self.assertTrue(value["work_time"]["available"])
        self.assertEqual(value["requested_project_id"], PID)

    def test_no_session_is_explicit_not_empty_recap(self):
        stub, calls = make_sidecar(CHANGES_OK)
        with patch.object(overview._dp, "_resolve_explicit_identity",
                           return_value=({"id": PID, "name": "Alpha",
                                          "matched_by": ""},
                                         {"id": PID, "name": "Alpha"},
                                         "associated", "")), \
             patch.object(overview._dp, "project_todos",
                          return_value={"todos": [], "reason": ""}), \
             patch.object(overview._dp, "work_sessions",
                          return_value={"sessions": [], "count": 0,
                                        "reason": "no work sessions"}), \
             patch.object(overview._dp, "last_session",
                          return_value={"session": None,
                                        "reason": "no session recorded"}), \
             patch.object(overview, "_psc", stub):
            value = overview.overview_for_project(PID)
        self.assertFalse(value["last_session"]["available"])
        self.assertIsNone(value["last_session"]["session"])
        self.assertIn("no", value["last_session"]["reason"].lower())
        # No session means the sidecar is never consulted.
        self.assertEqual(calls, [])

    def test_partial_failure_keeps_other_sections(self):
        stub, _calls = make_sidecar(CHANGES_OK)
        with patch.object(overview._dp, "_resolve_explicit_identity",
                           return_value=({"id": PID, "name": "Alpha",
                                          "matched_by": ""},
                                         {"id": PID, "name": "Alpha"},
                                         "associated", "")), \
             patch.object(overview._dp, "project_todos",
                          side_effect=RuntimeError("boom")), \
             patch.object(overview._dp, "work_sessions",
                          return_value={"sessions": [], "count": 0,
                                        "reason": "empty"}), \
             patch.object(overview._dp, "last_session",
                          return_value={"session": None, "reason": "none"}), \
             patch.object(overview, "_psc", stub):
            value = overview.overview_for_project(PID)
        self.assertFalse(value["todos"]["available"])
        self.assertIn("reason", value["todos"])
        # Other sections still present.
        self.assertIn("work_time", value)
        self.assertIn("last_session", value)
        self.assertEqual(value["requested_project_id"], PID)

    def test_evidence_is_defensively_clipped(self):
        big = dict(CHANGES_OK, evidence="x" * 20000)
        value, _calls = self._ok_result(changes=big)
        evidence = value["last_session"]["changes"]["evidence"]
        self.assertLessEqual(len(evidence), 8001)

    def test_bounded_evidence_passes_through_whole(self):
        # Evidence at the backend bound keeps its tail (limitation
        # disclaimer): the overview layer must not prefix-clip sections.
        tail = "Limitation: last observed state while active."
        evidence = "M a.py\n" + "y\n" * 3900 + tail
        self.assertLessEqual(len(evidence), 8000)
        value, _calls = self._ok_result(
            changes=dict(CHANGES_OK, evidence=evidence))
        kept = value["last_session"]["changes"]["evidence"]
        self.assertEqual(kept, evidence)
        self.assertTrue(kept.endswith(tail))

    def test_duration_ignores_bad_bounds(self):
        self.assertEqual(overview._session_duration_ms({}), 0)
        self.assertEqual(
            overview._session_duration_ms({"start_ms": 10, "end_ms": 5}), 0)
        self.assertEqual(
            overview._session_duration_ms({"start_ms": 0, "end_ms": 60000}),
            60000)


if __name__ == "__main__":
    unittest.main()
