"""Tests for the sessions helper (scripts/sessions.py).

Offline, no real binaries: module tests use an injectable FakeCollector,
CLI tests use a stub qs-desktop-context script plus a temp sidecar.

Surviving surface only: list (no --state), inbox (pending content),
get, search (collector search-activity only), link (4 kinds), and the
reduced annotate (session/device_id/revision). No units, states,
rollups, focus blocks, filed URIs, or content-bearing annotate fields.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock as mock
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import annotations
import content_index
import daily_review as _review
import sessions
from sessions import LedgerError

PID1 = "11111111-1111-4111-8111-111111111111"
PID2 = "22222222-2222-4222-8222-222222222222"
DEV = "d" * 32
DEV2 = "e" * 32
NOW = 1_700_000_000_000
SID1 = "a" * 32
SID2 = "b" * 32

SCRIPT = ROOT / "scripts" / "sessions.py"
NAMES = {PID1: "Demo", PID2: "Other"}


def make_session(sid, *, device=DEV, project=PID1, pname="Demo",
                 start=1000, end=61000, status="closed",
                 effective_status="closed", active=False,
                 apps=("app-a", "app-b"), events=10,
                 ended_reason="inactivity"):
    return {
        "session_id": sid,
        "device_id": device,
        "project": {"id": project, "name": pname} if project else None,
        "start_ms": start,
        "end_ms": end,
        "event_count": events,
        "status": status,
        "effective_status": effective_status,
        "ended_reason": ended_reason if status == "closed" else None,
        "active": active,
        "applications": list(apps),
    }


class FakeCollector:
    """Injectable collector with the exact public signatures."""

    def __init__(self, sessions=(), device_id=DEV, search_sessions=(),
                 resources=(), fail=None):
        self._sessions = [dict(s) for s in sessions]
        self._device_id = device_id
        self._search_sessions = [dict(s) for s in search_sessions]
        self._resources = list(resources)
        self.fail = fail
        self.calls = []

    def work_sessions(self, project_id=None, limit=None, from_ms=None,
                      to_ms=None, registry_file=None, desktop_bin=None,
                      db=None):
        self.calls.append(("work_sessions", project_id, limit, from_ms,
                           to_ms, registry_file, desktop_bin, db))
        if isinstance(self.fail, dict) and self.fail.get("work_sessions"):
            raise self.fail["work_sessions"]
        if self.fail is not None and not isinstance(self.fail, dict):
            raise self.fail
        rows = [dict(s) for s in self._sessions]
        return {"sessions": rows, "count": len(rows)}

    def device_id(self, desktop_bin=None, db=None):
        self.calls.append(("device_id", desktop_bin, db))
        if isinstance(self.fail, dict) and self.fail.get("device_id"):
            raise self.fail["device_id"]
        if self.fail is not None and not isinstance(self.fail, dict):
            raise self.fail
        return {"device_id": self._device_id, "reason": ""}

    def get_session(self, session_id=None, resource_limit=None,
                    include_events=None, event_limit=None,
                    desktop_bin=None, db=None):
        self.calls.append(("get_session", session_id, resource_limit,
                           desktop_bin, db))
        if isinstance(self.fail, dict) and self.fail.get("get_session"):
            raise self.fail["get_session"]
        if self.fail is not None and not isinstance(self.fail, dict):
            raise self.fail
        row = None
        for item in self._sessions:
            if item.get("session_id") == str(session_id).lower():
                row = dict(item)
                break
        return {"session": row, "resources": list(self._resources),
                "events_included": False}

    def search_activity(self, project_id=None, application=None,
                        resource=None, device=None, query=None,
                        from_ms=None, to_ms=None, limit=None,
                        desktop_bin=None, db=None):
        self.calls.append(("search_activity", query, limit))
        if isinstance(self.fail, dict) and self.fail.get("search_activity"):
            raise self.fail["search_activity"]
        if self.fail is not None and not isinstance(self.fail, dict):
            raise self.fail
        rows = [dict(s) for s in self._search_sessions]
        return {"query": {}, "sessions": rows, "count": len(rows)}


def _patch_names(testcase):
    return mock.patch.object(sessions, "_registry_names",
                             return_value=dict(NAMES))


class SessionsCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = str(Path(self.temp.name) / "annotations.db")
        self.conn = annotations.connect(self.db)
        self.addCleanup(self.conn.close)
        self._names = _patch_names(self)
        self._names.start()
        self.addCleanup(self._names.stop)

    def window(self):
        return (1000, 90000)

    def save_draft(self, draft_id, sid, *, project_id=PID1, saved=None):
        annotations.save_draft(
            self.conn, draft_id=draft_id, project_id=project_id,
            session_id=sid, summary_key="k-" + draft_id,
            evidence_digest="e" * 64,
            payload={"draft_id": draft_id, "markdown": "hello"},
            created_ms=1000)
        if saved is not None:
            annotations.mark_draft_saved(
                self.conn, draft_id, saved, now_ms=2000)


class ListTests(SessionsCase):
    def test_list_joins_collector_and_sidecar(self):
        coll = FakeCollector(
            sessions=[make_session(SID1), make_session(SID2, project=PID2,
                                                       pname="Other")])
        out = sessions.list_sessions(
            self.conn, from_ms=1000, to_ms=90000, collector=coll,
            now_ms=NOW)
        self.assertEqual(out["count"], 2)
        self.assertEqual(out["total"], 2)
        self.assertEqual(out["device_id"], DEV)
        first = out["entries"][0]
        self.assertIn("session", first)
        self.assertIn("pending", first)
        # No managed-object surface on entries.
        for entry in out["entries"]:
            for banned in ("unit", "state", "filed_uri", "filed_ref",
                           "attached_to", "effective_state"):
                self.assertNotIn(banned, entry)
            self.assertNotIn("unit", entry["session"])

    def test_list_project_filter_and_limit(self):
        coll = FakeCollector(
            sessions=[make_session(SID1), make_session(SID2, project=PID2,
                                                       pname="Other")])
        out = sessions.list_sessions(
            self.conn, from_ms=1000, to_ms=90000, project_id=PID2,
            collector=coll, now_ms=NOW)
        self.assertEqual(out["total"], 1)
        self.assertEqual(
            out["entries"][0]["session"]["project"]["id"], PID2)
        limited = sessions.list_sessions(
            self.conn, from_ms=1000, to_ms=90000, limit=1,
            collector=coll, now_ms=NOW)
        self.assertEqual(limited["count"], 1)
        self.assertEqual(limited["total"], 2)

    def test_list_range_must_be_paired(self):
        coll = FakeCollector(sessions=[])
        with self.assertRaises(LedgerError):
            sessions.list_sessions(
                self.conn, from_ms=1000, to_ms=None, collector=coll,
                now_ms=NOW)
        with self.assertRaises(LedgerError):
            sessions.list_sessions(
                self.conn, from_ms=2000, to_ms=1000, collector=coll,
                now_ms=NOW)

    def test_list_foreign_device_excluded_by_default(self):
        coll = FakeCollector(
            sessions=[make_session(SID1, device=DEV2)])
        out = sessions.list_sessions(
            self.conn, from_ms=1000, to_ms=90000, collector=coll,
            now_ms=NOW)
        self.assertEqual(out["total"], 0)
        inc = sessions.list_sessions(
            self.conn, from_ms=1000, to_ms=90000, include_foreign=True,
            collector=coll, now_ms=NOW)
        self.assertEqual(inc["total"], 1)

    def test_list_collector_failure_is_bounded(self):
        coll = FakeCollector(fail=RuntimeError("boom"))
        with self.assertRaises(LedgerError) as ctx:
            sessions.list_sessions(
                self.conn, from_ms=1000, to_ms=90000, collector=coll,
                now_ms=NOW)
        self.assertNotIn("boom", str(ctx.exception))


class InboxTests(SessionsCase):
    def test_inbox_returns_only_pending_drafts(self):
        coll = FakeCollector(
            sessions=[make_session(SID1), make_session(SID2)])
        self.save_draft("dwl_pending", SID1)
        self.save_draft("dwl_saved", SID2, saved="page")
        out = sessions.inbox(
            self.conn, from_ms=1000, to_ms=90000, collector=coll,
            now_ms=NOW)
        self.assertEqual(out["total"], 1)
        self.assertEqual(
            out["entries"][0]["session"]["session_id"], SID1)
        self.assertTrue(out["entries"][0]["pending"])

    def test_inbox_empty_without_drafts(self):
        coll = FakeCollector(sessions=[make_session(SID1)])
        out = sessions.inbox(
            self.conn, from_ms=1000, to_ms=90000, collector=coll,
            now_ms=NOW)
        self.assertEqual(out["total"], 0)
        self.assertEqual(out["entries"], [])


class InboxPhase2cTests(SessionsCase):
    """Phase-2c inbox: attended==0 AND (new capture OR unsaved draft)."""

    def add_capture(self, *, project_id=PID1, created_ms=5000,
                    status="new", text="Fix the retry backoff"):
        return annotations.insert_capture(
            self.conn, project_id=project_id, session_file="sess-file",
            message_ref="m-%d" % created_ms, kind="todo", text=text,
            actionable=None, durable=None, created_ms=created_ms)

    def attend(self, sid, attended=True):
        row = annotations.upsert_session_meta(
            self.conn, session_id=sid, now_ms=NOW)
        return annotations.upsert_session_meta(
            self.conn, session_id=sid, now_ms=NOW + 1,
            expect_revision=row["revision"], attended=attended)

    def test_capture_present_no_draft_needs_attention(self):
        coll = FakeCollector(
            sessions=[make_session(SID1), make_session(SID2, project=PID2,
                                                       pname="Other")])
        self.add_capture(project_id=PID1, created_ms=5000)
        out = sessions.inbox(
            self.conn, from_ms=1000, to_ms=90000, collector=coll,
            now_ms=NOW)
        self.assertEqual(out["total"], 1)
        entry = out["entries"][0]
        self.assertEqual(entry["session"]["session_id"], SID1)
        self.assertTrue(entry["pending"])
        self.assertFalse(entry["pending_draft"])
        self.assertTrue(entry["pending_capture"])
        self.assertFalse(entry["attended"])

    def test_draft_present_no_capture_needs_attention(self):
        coll = FakeCollector(sessions=[make_session(SID1)])
        self.save_draft("dwl_pending", SID1)
        out = sessions.inbox(
            self.conn, from_ms=1000, to_ms=90000, collector=coll,
            now_ms=NOW)
        self.assertEqual(out["total"], 1)
        entry = out["entries"][0]
        self.assertTrue(entry["pending_draft"])
        self.assertFalse(entry["pending_capture"])

    def test_capture_outside_window_is_not_attention(self):
        coll = FakeCollector(sessions=[make_session(SID1)])
        self.add_capture(project_id=PID1, created_ms=500)
        out = sessions.inbox(
            self.conn, from_ms=1000, to_ms=90000, collector=coll,
            now_ms=NOW)
        self.assertEqual(out["total"], 0)

    def test_accepted_capture_is_not_attention(self):
        coll = FakeCollector(sessions=[make_session(SID1)])
        self.add_capture(project_id=PID1, created_ms=5000,
                         status="new")
        annotations.set_capture_status(self.conn, 1, "accepted")
        out = sessions.inbox(
            self.conn, from_ms=1000, to_ms=90000, collector=coll,
            now_ms=NOW)
        self.assertEqual(out["total"], 0)

    def test_attended_session_leaves_inbox_but_stays_in_list(self):
        coll = FakeCollector(sessions=[make_session(SID1)])
        self.save_draft("dwl_pending", SID1)
        self.attend(SID1, True)
        boxed = sessions.inbox(
            self.conn, from_ms=1000, to_ms=90000, collector=coll,
            now_ms=NOW)
        self.assertEqual(boxed["total"], 0)
        self.assertEqual(boxed["entries"], [])
        listed = sessions.list_sessions(
            self.conn, from_ms=1000, to_ms=90000, collector=coll,
            now_ms=NOW)
        self.assertEqual(listed["total"], 1)
        self.assertTrue(listed["entries"][0]["attended"])

    def test_inbox_subset_of_list_with_identical_entries(self):
        coll = FakeCollector(
            sessions=[make_session(SID1),
                      make_session(SID2, project=PID2, pname="Other")])
        self.save_draft("dwl_pending", SID1)
        self.add_capture(project_id=PID2, created_ms=6000)
        listed = sessions.list_sessions(
            self.conn, from_ms=1000, to_ms=90000, collector=coll,
            now_ms=NOW)
        boxed = sessions.inbox(
            self.conn, from_ms=1000, to_ms=90000, collector=coll,
            now_ms=NOW)
        list_by_sid = {e["session"]["session_id"]: e
                       for e in listed["entries"]}
        self.assertEqual(
            {e["session"]["session_id"] for e in boxed["entries"]},
            {SID1, SID2})
        for entry in boxed["entries"]:
            # Same entry identity: inbox rows equal their list rows, so
            # the card and the badge can never disagree.
            self.assertEqual(entry, list_by_sid[
                entry["session"]["session_id"]])
        # Badge count is the inbox total.
        self.assertEqual(boxed["count"], boxed["total"])
        self.assertEqual(boxed["count"], len(boxed["entries"]))

    def test_inbox_capped_at_999(self):
        # NOTE: the sidecar bulk map caps at 1000 ids, so 1000 pending
        # sessions is the largest observable attention set here.
        count = 1000
        rows = []
        for index in range(count):
            sid = "%032x" % (index + 1)
            rows.append(make_session(sid))
            annotations.save_draft(
                self.conn, draft_id="dwl_%d" % index, project_id=PID1,
                session_id=sid, summary_key="k",
                evidence_digest="e" * 64,
                payload={"draft_id": "dwl_%d" % index}, created_ms=1000)
        coll = FakeCollector(sessions=rows)
        out = sessions.inbox(
            self.conn, from_ms=1000, to_ms=90000, collector=coll,
            now_ms=NOW)
        self.assertEqual(out["total"], count)
        self.assertEqual(out["count"], 50)  # default limit still 50
        self.assertEqual(len(out["entries"]), 50)
        explicit = sessions.inbox(
            self.conn, from_ms=1000, to_ms=90000, limit=1000,
            collector=coll, now_ms=NOW)
        self.assertEqual(explicit["count"], 999)

    def test_inbox_silent_when_captures_unavailable(self):
        coll = FakeCollector(sessions=[make_session(SID1)])
        self.save_draft("dwl_pending", SID1)
        self.conn.execute("DROP TABLE captures")
        self.conn.commit()
        out = sessions.inbox(
            self.conn, from_ms=1000, to_ms=90000, collector=coll,
            now_ms=NOW)
        # Degrades to draft-only instead of failing.
        self.assertEqual(out["total"], 1)
        self.assertFalse(out["entries"][0]["pending_capture"])

    def test_inbox_without_sidecar_reads_empty(self):
        coll = FakeCollector(sessions=[make_session(SID1)])
        out = sessions.inbox(
            None, from_ms=1000, to_ms=90000, collector=coll,
            now_ms=NOW)
        self.assertEqual(out["total"], 0)
        self.assertEqual(out["entries"], [])

    def test_detail_carries_pending_split_and_attended(self):
        coll = FakeCollector(sessions=[make_session(SID1)])
        self.add_capture(project_id=PID1, created_ms=5000)
        out = sessions.get_session_detail(
            self.conn, session_id=SID1, collector=coll, now_ms=NOW)
        self.assertTrue(out["pending"])
        self.assertTrue(out["pending_capture"])
        self.assertFalse(out["pending_draft"])
        self.assertFalse(out["attended"])

    def test_annotate_phase2c_fields_roundtrip(self):
        first = sessions.annotate(
            self.conn, {"session": SID1,
                        "thought_ref": "journal:2026_09_21.md:11",
                        "todo_refs": ["Demo.md:4"],
                        "refs": ["file:///tmp/a.py"],
                        "attended": True},
            now_ms=NOW)
        self.assertTrue(first["ok"])
        meta = annotations.get_session_meta(self.conn, SID1)
        assert meta is not None
        self.assertEqual(meta["thought_ref"], "journal:2026_09_21.md:11")
        self.assertEqual(meta["todo_refs"], ["Demo.md:4"])
        self.assertEqual(meta["refs"], ["file:///tmp/a.py"])
        self.assertEqual(meta["attended"], 1)
        second = sessions.annotate(
            self.conn, {"session": SID1, "revision": first["revision"],
                        "attended": False},
            now_ms=NOW + 1)
        self.assertNotEqual(second["revision"], first["revision"])
        meta = annotations.get_session_meta(self.conn, SID1)
        assert meta is not None
        self.assertEqual(meta["attended"], 0)
        # Untouched fields survive the CAS update.
        self.assertEqual(meta["thought_ref"], "journal:2026_09_21.md:11")

    def test_annotate_rejects_bad_phase2c_shapes(self):
        sessions.annotate(self.conn, {"session": SID1}, now_ms=NOW)
        for payload in ({"session": SID1, "thought_ref": "free text"},
                        {"session": SID1, "todo_refs": "not-json"},
                        {"session": SID1, "refs": ["ok", 7]},
                        {"session": SID1, "attended": "yes"}):
            with self.subTest(payload=str(payload)[:48]):
                with self.assertRaises(ValueError):
                    sessions.annotate(self.conn, payload, now_ms=NOW)


class DetailTests(SessionsCase):
    def test_get_returns_detail_shape(self):
        coll = FakeCollector(
            sessions=[make_session(SID1)], resources=[{"kind": "file"}])
        self.save_draft("dwl_one", SID1)
        annotations.add_session_link(
            self.conn, session_id=SID1, kind="draft",
            target="dwl_one", now_ms=NOW)
        out = sessions.get_session_detail(
            self.conn, session_id=SID1, collector=coll, now_ms=NOW)
        self.assertEqual(out["session"]["session_id"], SID1)
        self.assertTrue(out["pending"])
        self.assertIsNotNone(out["draft"])
        self.assertEqual(len(out["drafts"]), 1)
        self.assertEqual(out["links"][0]["kind"], "draft")
        self.assertEqual(len(out["resources"]), 1)

    def test_get_unknown_session_rejected(self):
        coll = FakeCollector(sessions=[])
        with self.assertRaises(LedgerError):
            sessions.get_session_detail(
                self.conn, session_id=SID1, collector=coll, now_ms=NOW)

    def test_get_bad_id_rejected_boundedly(self):
        coll = FakeCollector(sessions=[])
        with self.assertRaises(LedgerError):
            sessions.get_session_detail(
                self.conn, session_id="not-a-session", collector=coll,
                now_ms=NOW)


class SearchTests(SessionsCase):
    def _search_row(self, sid, **over):
        row = {"session_id": sid, "device_id": DEV,
               "start_ms": 1000, "end_ms": 2000,
               "project": {"id": PID1, "name": "Demo"},
               "resources": []}
        row.update(over)
        return row

    def test_search_returns_collector_matches(self):
        coll = FakeCollector(
            search_sessions=[self._search_row(SID1),
                             self._search_row(SID2)])
        out = sessions.search(
            self.conn, query="login bug", collector=coll, now_ms=NOW)
        self.assertEqual(out["query"], "login bug")
        self.assertEqual(out["count"], 2)
        first = out["entries"][0]
        self.assertEqual(first["matched"], "collector")
        self.assertEqual(first["project_id"], PID1)
        self.assertIn("resource_copy", first)

    def test_search_dedupes_by_session(self):
        coll = FakeCollector(
            search_sessions=[self._search_row(SID1),
                             self._search_row(SID1)])
        out = sessions.search(
            self.conn, query="dup", collector=coll, now_ms=NOW)
        self.assertEqual(out["count"], 1)

    def test_search_reports_total_and_truncated(self):
        # S-041: the session: source reports truncated/total so the
        # palette can render "showing X of Y".
        coll = FakeCollector(
            search_sessions=[self._search_row(SID1),
                             self._search_row(SID2)])
        full = sessions.search(
            self.conn, query="login bug", collector=coll, now_ms=NOW)
        self.assertEqual(full["count"], 2)
        self.assertEqual(full["total"], 2)
        self.assertFalse(full["truncated"])
        capped = sessions.search(
            self.conn, query="login bug", limit=1, collector=coll,
            now_ms=NOW)
        self.assertEqual(capped["count"], 1)
        self.assertEqual(capped["total"], 2)
        self.assertTrue(capped["truncated"])

    def test_search_collector_unavailable_degrades(self):
        coll = FakeCollector(fail=RuntimeError("down"))
        out = sessions.search(
            self.conn, query="x", collector=coll, now_ms=NOW)
        self.assertEqual(out["count"], 0)
        self.assertEqual(out["reason"], "collector unavailable")

    def test_search_rejects_blank_query(self):
        coll = FakeCollector(search_sessions=[])
        with self.assertRaises(LedgerError):
            sessions.search(self.conn, query="  ", collector=coll,
                            now_ms=NOW)


class SearchUnionTests(SessionsCase):
    """Phase-2c search union: content MATCH ∪ collector, content first.

    Additive only: the foundation ``SearchTests`` class above is
    untouched. These pin the union ranking, dedupe, degradation, and
    literal-quoting rules.
    """

    def _search_row(self, sid, **over):
        row = {"session_id": sid, "device_id": DEV,
               "start_ms": 1000, "end_ms": 2000,
               "project": {"id": PID1, "name": "Demo"},
               "resources": []}
        row.update(over)
        return row

    def _index(self, text, sid, kind="thought", project_id=PID1):
        return content_index.index_block(
            self.conn, text=text, page="Demo", line=1, kind=kind,
            session_id=sid, project_id=project_id)

    def test_content_matches_rank_first(self):
        self._index("uniquecontentword alpha", SID2)
        coll = FakeCollector(
            search_sessions=[self._search_row(SID1),
                             self._search_row(SID2)])
        out = sessions.search(
            self.conn, query="uniquecontentword", collector=coll,
            now_ms=NOW)
        self.assertEqual(out["count"], 2)
        self.assertEqual(out["entries"][0]["session_id"], SID2)
        self.assertEqual(out["entries"][0]["matched"], "content")
        self.assertEqual(out["entries"][1]["session_id"], SID1)
        self.assertEqual(out["entries"][1]["matched"], "collector")

    def test_dedupe_content_row_wins(self):
        self._index("sharedword beta", SID1)
        coll = FakeCollector(
            search_sessions=[self._search_row(SID1)])
        out = sessions.search(
            self.conn, query="sharedword", collector=coll, now_ms=NOW)
        self.assertEqual(out["count"], 1)
        entry = out["entries"][0]
        self.assertEqual(entry["session_id"], SID1)
        self.assertEqual(entry["matched"], "content")
        # Collector facts still enrich the winning row.
        self.assertEqual(entry["start_ms"], 1000)
        self.assertEqual(entry["end_ms"], 2000)
        self.assertEqual(entry["project_id"], PID1)
        self.assertIn("sharedword", entry["resource_copy"])

    def test_content_only_entry_shape(self):
        self._index("lonelythought gamma", SID2)
        coll = FakeCollector(search_sessions=[])
        out = sessions.search(
            self.conn, query="lonelythought", collector=coll,
            now_ms=NOW)
        self.assertEqual(out["count"], 1)
        entry = out["entries"][0]
        self.assertEqual(entry["matched"], "content")
        self.assertEqual(entry["project_id"], PID1)
        self.assertEqual(entry["project_name"], "Demo")
        self.assertIsNone(entry["device_id"])
        self.assertIsNone(entry["start_ms"])
        self.assertIsNone(entry["end_ms"])
        self.assertIn("lonelythought", entry["resource_copy"])
        self.assertEqual(out["reason"], "")

    def test_collector_failure_yields_content_only(self):
        self._index("fallbackthought delta", SID1)
        coll = FakeCollector(fail=RuntimeError("down"))
        out = sessions.search(
            self.conn, query="fallbackthought", collector=coll,
            now_ms=NOW)
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["entries"][0]["matched"], "content")
        self.assertEqual(out["reason"], "collector unavailable")

    def test_content_failure_yields_collector_only(self):
        coll = FakeCollector(
            search_sessions=[self._search_row(SID1)])
        out = sessions.search(
            None, query="anything", collector=coll, now_ms=NOW)
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["entries"][0]["matched"], "collector")
        self.assertEqual(out["reason"], "")

    def test_both_fail_reads_empty_with_reason(self):
        coll = FakeCollector(fail=RuntimeError("down"))
        out = sessions.search(
            None, query="anything", collector=coll, now_ms=NOW)
        self.assertEqual(out["count"], 0)
        self.assertEqual(out["entries"], [])
        self.assertEqual(out["reason"], "collector unavailable")

    def test_fts_operators_are_literal(self):
        self._index("plain backoff note", SID1)
        coll = FakeCollector(search_sessions=[])
        for query in ('backoff OR "unmatched-token-zz"',
                      '"backoff',
                      "OR",
                      "--help"):
            with self.subTest(query=query):
                out = sessions.search(
                    self.conn, query=query, collector=coll,
                    now_ms=NOW)
                self.assertLessEqual(out["count"], 1)
        out = sessions.search(
            self.conn, query='"backoff"', collector=coll, now_ms=NOW)
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["entries"][0]["matched"], "content")

    def test_union_truncates_to_bound_content_first(self):
        sids = ["%032x" % index for index in range(1, 7)]
        for sid in sids[:3]:
            self._index("truncateword item", sid)
        coll = FakeCollector(
            search_sessions=[self._search_row(sid) for sid in sids[3:]])
        out = sessions.search(
            self.conn, query="truncateword", limit=4, collector=coll,
            now_ms=NOW)
        self.assertEqual(out["count"], 4)
        self.assertEqual(
            [e["matched"] for e in out["entries"]],
            ["content", "content", "content", "collector"])
        self.assertEqual(out["limit"], 4)


class LinkTests(SessionsCase):
    def test_link_four_kinds_roundtrip(self):
        for kind in ("capture", "draft", "agent_session",
                     "continued_from"):
            with self.subTest(kind=kind):
                added = sessions.set_link(
                    self.conn, session_id=SID1, kind=kind,
                    target="tgt-" + kind, now_ms=NOW)
                self.assertTrue(added["added"])
                removed = sessions.set_link(
                    self.conn, session_id=SID1, kind=kind,
                    target="tgt-" + kind, remove=True, now_ms=NOW)
                self.assertTrue(removed["removed"])

    def test_link_rejects_attached_to_and_bad_kinds(self):
        for kind in ("attached_to", "unit", "bogus", "", None):
            with self.subTest(kind=kind):
                with self.assertRaises((LedgerError, ValueError)):
                    sessions.set_link(
                        self.conn, session_id=SID1, kind=kind,
                        target="tgt", now_ms=NOW)

    def test_link_rejects_bad_session_and_target(self):
        with self.assertRaises((LedgerError, ValueError)):
            sessions.set_link(self.conn, session_id="bad", kind="draft",
                              target="tgt", now_ms=NOW)
        with self.assertRaises((LedgerError, ValueError)):
            sessions.set_link(self.conn, session_id=SID1, kind="draft",
                              target="", now_ms=NOW)


class AnnotateTests(SessionsCase):
    def test_annotate_create_and_revision_update(self):
        first = sessions.annotate(
            self.conn, {"session": SID1}, now_ms=NOW)
        self.assertTrue(first["ok"])
        self.assertEqual(first["session_id"], SID1)
        second = sessions.annotate(
            self.conn, {"session": SID1, "device_id": DEV,
                        "revision": first["revision"]},
            now_ms=NOW + 1)
        self.assertTrue(second["ok"])
        self.assertNotEqual(second["revision"], first["revision"])

    def test_annotate_stale_revision_rejected(self):
        first = sessions.annotate(
            self.conn, {"session": SID1}, now_ms=NOW)
        sessions.annotate(
            self.conn, {"session": SID1,
                        "revision": first["revision"]},
            now_ms=NOW + 1)
        with self.assertRaises(LedgerError):
            sessions.annotate(
                self.conn, {"session": SID1,
                            "revision": first["revision"]},
                now_ms=NOW + 2)

    def test_annotate_rejects_content_fields(self):
        for field in ("title", "intent", "outcome", "tags",
                      "next_step", "ignore_reason", "project_override",
                      "state"):
            with self.subTest(field=field):
                with self.assertRaises(LedgerError):
                    sessions.annotate(
                        self.conn, {"session": SID1, field: "x"},
                        now_ms=NOW)

    def test_annotate_rejects_bad_payload_boundedly(self):
        secret = "SECRET-MARKER-annotate"
        for payload in (None, [], "x", {}, {"device_id": DEV},
                        {"session": "bad", "extra": secret}):
            with self.subTest(payload=repr(payload)[:40]):
                with self.assertRaises(LedgerError) as ctx:
                    sessions.annotate(self.conn, payload, now_ms=NOW)
                self.assertNotIn(secret, str(ctx.exception))


STUB = """#!/usr/bin/env python3
import sys
from pathlib import Path
base = Path(__file__).resolve().parent
FILES = {"device-id": "device-id.json", "sessions": "sessions.json",
         "session-detail": "session-detail.json", "search": "search.json"}
cmd = None
for tok in sys.argv[1:]:
    if tok in FILES:
        cmd = tok
        break
if cmd is None:
    print("unknown collector command", file=sys.stderr)
    sys.exit(1)
sys.stdout.write((base / FILES[cmd]).read_text(encoding="utf-8"))
"""


def run_cli(args, env_extra=None, stdin_data=None):
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if env_extra:
        env.update(env_extra)
    if isinstance(stdin_data, (bytes, bytearray)):
        stdin_text = bytes(stdin_data).decode("utf-8")
    else:
        stdin_text = stdin_data
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        input=stdin_text, capture_output=True, text=True, timeout=30,
        check=False, env=env)


class SessionsCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.db = str(self.base / "annotations.db")
        self.stub = self.base / "qs-desktop-context"
        self.stub.write_text(STUB, encoding="utf-8")
        self.stub.chmod(0o755)
        self.activity = self.base / "activity.db"
        self.activity.write_bytes(os.urandom(64))
        registry = self.base / "registry.toml"
        registry.write_text(
            'version = 1\n'
            '[[projects]]\n'
            f'id = "{PID1}"\n'
            'name = "Demo"\n'
            'logseq_path = "pages/Demo.md"\n'
            'local_folder = ""\n'
            'github_url = ""\n'
            '[[projects]]\n'
            f'id = "{PID2}"\n'
            'name = "Other"\n'
            'logseq_path = "pages/Other.md"\n'
            'local_folder = ""\n'
            'github_url = ""\n', encoding="utf-8")
        self.env = {"QS_DESKTOP_DB": str(self.activity),
                    "QUICKSHELL_PROJECTS_FILE": str(registry),
                    "QUICKSHELL_SETTINGS": str(self.base / "settings.json")}
        (self.base / "settings.json").write_text(json.dumps({}),
                                                 encoding="utf-8")
        (self.base / "device-id.json").write_text(
            json.dumps(DEV), encoding="utf-8")

    def _write(self, name, value):
        (self.base / name).write_text(json.dumps(value), encoding="utf-8")

    def _args(self, *rest):
        return ["--db", self.db, "--desktop-bin", str(self.stub), *rest]

    def test_list_and_inbox_happy_paths(self):
        start_w, end_w = _review._day_window(
            _review._local_dt(NOW).date())
        session = make_session(SID1, start=start_w + 10,
                               end=start_w + 70010)
        self._write("sessions.json", [session])
        listed = run_cli(self._args(
            "list", "--from", str(start_w), "--to", str(end_w)),
            env_extra=self.env)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        payload = json.loads(listed.stdout)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["device_id"], DEV)
        # No --state flag: the parser rejects it without echo.
        bad_state = run_cli(self._args(
            "list", "--from", str(start_w), "--to", str(end_w),
            "--state", "new"), env_extra=self.env)
        self.assertNotEqual(bad_state.returncode, 0)
        self.assertIn("error:", bad_state.stderr)
        self.assertNotIn("Traceback", bad_state.stderr)
        # Inbox is empty with no drafts; pending once a draft exists.
        inbox = run_cli(self._args(
            "inbox", "--from", str(start_w), "--to", str(end_w)),
            env_extra=self.env)
        self.assertEqual(inbox.returncode, 0, inbox.stderr)
        self.assertEqual(json.loads(inbox.stdout)["count"], 0)
        conn = annotations.connect(self.db)
        try:
            annotations.save_draft(
                conn, draft_id="dwl_cli", project_id=PID1,
                session_id=SID1, summary_key="k",
                evidence_digest="e" * 64,
                payload={"draft_id": "dwl_cli"}, created_ms=NOW)
        finally:
            conn.close()
        inbox2 = run_cli(self._args(
            "inbox", "--from", str(start_w), "--to", str(end_w)),
            env_extra=self.env)
        self.assertEqual(inbox2.returncode, 0, inbox2.stderr)
        self.assertEqual(json.loads(inbox2.stdout)["count"], 1)

    def test_get_and_search_cli(self):
        session = make_session(SID1, start=1000, end=61000)
        self._write("sessions.json", [session])
        self._write("session-detail.json",
                    {"session": session, "resources": [],
                     "events_included": False})
        self._write("search.json",
                    {"query": {}, "sessions": [], "count": 0})
        detail = run_cli(
            self._args("get", "--session", SID1), env_extra=self.env)
        self.assertEqual(detail.returncode, 0, detail.stderr)
        body = json.loads(detail.stdout)
        self.assertEqual(body["session"]["session_id"], SID1)
        found = run_cli(
            self._args("search", "--query", "alpha"), env_extra=self.env)
        self.assertEqual(found.returncode, 0, found.stderr)
        self.assertEqual(json.loads(found.stdout)["query"], "alpha")

    def test_annotate_reduced_and_link_cli(self):
        annotated = run_cli(
            self._args("annotate"), env_extra=self.env,
            stdin_data=json.dumps({"session": SID1}).encode())
        self.assertEqual(annotated.returncode, 0, annotated.stderr)
        self.assertTrue(json.loads(annotated.stdout)["ok"])
        revision = json.loads(annotated.stdout)["revision"]
        # Content fields are rejected, not stored.
        refused = run_cli(
            self._args("annotate"), env_extra=self.env,
            stdin_data=json.dumps(
                {"session": SID1, "title": "CLI",
                 "revision": revision}).encode())
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("error:", refused.stderr)
        linked = run_cli(
            self._args("link", "--session", SID1, "--kind", "draft",
                       "--target", "dwl_cli"), env_extra=self.env)
        self.assertEqual(linked.returncode, 0, linked.stderr)
        self.assertTrue(json.loads(linked.stdout)["added"])
        bad_kind = run_cli(
            self._args("link", "--session", SID1, "--kind", "attached_to",
                       "--target", "dwl_cli"), env_extra=self.env)
        self.assertNotEqual(bad_kind.returncode, 0)
        self.assertIn("error:", bad_kind.stderr)

    def test_deleted_subcommands_rejected(self):
        for argv in (["unit-keep"], ["unit-split"], ["rollup"],
                     ["focus-list"], ["mark-filed"]):
            with self.subTest(argv=argv):
                bad = run_cli(self._args(*argv), env_extra=self.env)
                self.assertNotEqual(bad.returncode, 0)
                self.assertIn("error:", bad.stderr)
                self.assertNotIn("Traceback", bad.stderr)
                self.assertEqual(bad.stdout, "")

    def test_invalid_args_bounded(self):
        bad = run_cli(self._args("list", "--limit", "0"), env_extra=self.env)
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("error:", bad.stderr)
        self.assertNotIn("Traceback", bad.stderr)
        self.assertEqual(bad.stdout, "")
        lines = [line for line in bad.stderr.strip().splitlines() if line]
        self.assertEqual(len(lines), 1)

    def test_invalid_stdin_never_echoed(self):
        secret = "SECRET-MARKER-9f8eSESSIONS"
        bad = run_cli(self._args("annotate"), env_extra=self.env,
                      stdin_data=("{not json " + secret).encode())
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("error:", bad.stderr)
        self.assertNotIn(secret, bad.stdout + bad.stderr)
        self.assertNotIn("Traceback", bad.stderr)

    def test_read_commands_leave_dbs_untouched(self):
        start_w, end_w = _review._day_window(
            _review._local_dt(NOW).date())
        session = make_session(SID1, start=start_w + 10,
                               end=start_w + 70010)
        self._write("sessions.json", [session])
        self._write("session-detail.json",
                    {"session": session, "resources": [],
                     "events_included": False})
        self._write("search.json",
                    {"query": {}, "sessions": [], "count": 0})
        conn = annotations.connect(self.db)
        conn.close()
        before_sidecar = Path(self.db).read_bytes()
        before_activity = self.activity.read_bytes()
        for argv in (["list", "--from", str(start_w), "--to", str(end_w)],
                     ["inbox", "--from", str(start_w), "--to", str(end_w)],
                     ["get", "--session", SID1],
                     ["search", "--query", "alpha"]):
            completed = run_cli(self._args(*argv), env_extra=self.env)
            self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(Path(self.db).read_bytes(), before_sidecar)
        self.assertEqual(self.activity.read_bytes(), before_activity)


if __name__ == "__main__":
    unittest.main()
