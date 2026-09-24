"""Backend tests for capture inbox (scripts/session_capture.py).

Offline: injected evidence, temp PI session dirs, real JSONL fixtures.
The scan is local-regex-only: no network, no model call, no API key.
No real network, ever.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import annotations
import session_capture as cap

PID = "11111111-1111-4111-8111-111111111111"
NOW = 5_000_000


def write_session_file(scope: Path, name="sess-1.jsonl", lines=()):
    header = {"type": "session", "id": "sess-1", "version": 1,
              "timestamp": "2026-09-19T00:00:00Z", "cwd": "/tmp"}
    path = scope / name
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(header) + "\n")
        for record in lines:
            if isinstance(record, str):
                handle.write(record + "\n")
            else:
                handle.write(json.dumps(record) + "\n")
    return path


def msg(mid, role, texts):
    content = []
    for item in texts:
        if isinstance(item, str):
            content.append({"type": "text", "text": item})
        else:
            content.append(item)
    return {"type": "message", "id": mid,
            "message": {"role": role, "content": content}}


class FakeEvidence:
    def __init__(self, pid=PID):
        self._pid = pid

    def current_project(self):
        if self._pid is None:
            return {"project": None}
        return {"project": {"id": self._pid, "name": "Demo"}}


class CaptureCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.db = str(self.base / "annotations.db")
        self.conn = annotations.connect(self.db)
        self.addCleanup(self.conn.close)
        self.pi_dir = self.base / "pi-sessions"
        self.pi_dir.mkdir(parents=True)
        self._env = patch.dict(os.environ,
                               {"PI_CODING_AGENT_SESSION_DIR": str(self.pi_dir),
                                "OPENROUTER_API_KEY": "test-key"})
        self._env.start()
        self.addCleanup(self._env.stop)
        import project_sessions as _ps
        self.scope = _ps.scope_for_id(None, PID)

    def tearDown(self):
        pass

    def settings(self, **over):
        s = {"enabled": True, "sessionCapture": True}
        s.update(over)
        return s

    def ctx(self, settings=None, evidence=None,
            requested_project=PID, now_ms=NOW):
        settings = settings or self.settings()
        evidence = evidence if evidence is not None else FakeEvidence()
        # Local-regex scan: no API key, no model callback, no network.
        return {"conn": self.conn, "settings": settings, "now_ms": now_ms,
                "evidence": evidence,
                "requested_project": requested_project}

    def ref(self, session_file):
        return annotations.get_state(
            self.conn, f"last_capture_ref:{session_file}", default="")


class ScanTests(CaptureCase):
    def test_markers_stored_directly_no_network(self):
        lines = [
            msg("m1", "user", ["TODO: fix the login bug"]),
            msg("m2", "assistant", ["DECISION: use sqlite wal mode"]),
            msg("m3", "user", ["Next: write the migration"]),
            msg("m4", "assistant", ["FIXME: handle null session"]),
            msg("m5", "user", ["- [ ] add inbox docs"]),
            # Never stored (wrong roles / non-text content):
            msg("m6", "toolResult",
                [{"type": "text", "text": "TODO: tool output secret"}]),
            {"type": "message", "id": "m7",
             "message": {"role": "assistant",
                         "content": [{"type": "thinking",
                                      "text": "TODO: secret plan"}]}},
        ]
        path = write_session_file(self.scope, lines=lines)
        out = cap.scan(self.ctx())
        self.assertTrue(out["changed"])
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["jev_calls"], 0)
        # Stored rows carry kind/text, all new.
        rows = self.conn.execute(
            "SELECT kind, text, status FROM captures"
            " ORDER BY id").fetchall()
        self.assertEqual(len(rows), 5)
        kinds = [r["kind"] for r in rows]
        self.assertEqual(kinds, ["todo", "decision", "next", "fixme",
                                 "checkbox"])
        for r in rows:
            self.assertEqual(r["status"], "new")
            self.assertLessEqual(len(r["text"]), 400)
        texts = [r["text"] for r in rows]
        self.assertIn("TODO: fix the login bug", texts[0])
        # No transcript/thinking/tool text stored.
        dumped = json.dumps(texts)
        self.assertNotIn("tool output secret", dumped)
        self.assertNotIn("secret plan", dumped)
        # Ref advanced past the scanned window.
        self.assertNotEqual(self.ref(str(path)), "")

    def test_needs_no_key_and_no_decide_callback(self):
        # The ctx carries neither has_key nor decide: the scan must not
        # require them (local-regex-only, no network).
        write_session_file(
            self.scope, lines=[msg("m1", "user", ["TODO: one thing"])])
        ctx = self.ctx()
        self.assertNotIn("has_key", ctx)
        self.assertNotIn("decide", ctx)
        out = cap.scan(ctx)
        self.assertTrue(out["changed"])
        self.assertEqual(out["jev_calls"], 0)

    def test_second_scan_no_candidates_offset_hash_dedupe(self):
        path = write_session_file(
            self.scope,
            lines=[msg("m1", "user", ["TODO: one thing"])])
        first = cap.scan(self.ctx())
        self.assertTrue(first["changed"])
        before = self.conn.execute(
            "SELECT COUNT(*) AS n FROM captures").fetchone()["n"]
        second = cap.scan(self.ctx(now_ms=NOW + 61_000))
        self.assertEqual(second["jev_calls"], 0)
        after = self.conn.execute(
            "SELECT COUNT(*) AS n FROM captures").fetchone()["n"]
        self.assertEqual(before, after)
        self.assertEqual(second["reason"], "no_candidates")

    def test_flag_off_skipped(self):
        write_session_file(
            self.scope, lines=[msg("m1", "user", ["TODO: x"])])
        out = cap.scan(self.ctx(settings=self.settings(sessionCapture=False)))
        self.assertEqual(out["reason"], "disabled")
        self.assertEqual(out["status"], "skipped")

    def test_sensitive_dropped_locally(self):
        write_session_file(self.scope, lines=[
            msg("m1", "user", ["TODO: read /home/u/.ssh/id_rsa now"]),
            msg("m2", "user", ["TODO: rotate token=abc123 now"]),
            msg("m3", "user", ["TODO: legitimate follow-up"]),
        ])
        out = cap.scan(self.ctx())
        self.assertTrue(out["changed"])
        rows = self.conn.execute(
            "SELECT text FROM captures").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIn("legitimate", rows[0]["text"])

    def test_sensitive_only_no_candidates(self):
        write_session_file(self.scope, lines=[
            msg("m1", "user", ["TODO: check /home/u/.ssh/id_rsa"]),
        ])
        out = cap.scan(self.ctx())
        self.assertEqual(out["reason"], "no_candidates")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM captures")
            .fetchone()["n"], 0)

    def test_over_eight_pagination(self):
        lines = [msg(f"m{i}", "user", [f"TODO: item number {i}"])
                 for i in range(12)]
        write_session_file(self.scope, lines=lines)
        first = cap.scan(self.ctx())
        self.assertTrue(first["changed"])
        n1 = self.conn.execute(
            "SELECT COUNT(*) AS n FROM captures").fetchone()["n"]
        self.assertEqual(n1, 8)
        second = cap.scan(self.ctx(now_ms=NOW + 61_000))
        self.assertTrue(second["changed"])
        n2 = self.conn.execute(
            "SELECT COUNT(*) AS n FROM captures").fetchone()["n"]
        self.assertEqual(n2, 12)

    def test_malformed_jsonl_skipped(self):
        path = write_session_file(self.scope, lines=[])
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("not json at all\n")
            handle.write(json.dumps(msg("m1", "user",
                                            ["TODO: after garbage"])) + "\n")
        out = cap.scan(self.ctx())
        self.assertTrue(out["changed"])

    def test_missing_session_no_session(self):
        # Fresh project with no session file at all.
        other = "22222222-2222-4222-8222-222222222222"
        out = cap.scan(self.ctx(requested_project=other))
        self.assertEqual(out["reason"], "no_session")

    def test_no_project_skipped(self):
        out = cap.scan(self.ctx(requested_project=None,
                                evidence=FakeEvidence(pid=None)))
        self.assertEqual(out["reason"], "no_project")

    def test_current_project_resolution(self):
        write_session_file(self.scope, lines=[
            msg("m1", "user", ["TODO: via current project"])])
        ctx = self.ctx(requested_project=None, evidence=FakeEvidence(pid=PID))
        out = cap.scan(ctx)
        self.assertTrue(out["changed"])

    def test_db_failure_deferred_failed_ref_not_advanced(self):
        path = write_session_file(self.scope, lines=[
            msg("m1", "user", ["TODO: db will fail"])])
        real = annotations.insert_capture

        def broken(*a, **k):
            raise annotations.AnnotationsError("database is unavailable")

        with patch.object(annotations, "insert_capture", side_effect=broken):
            out = cap.scan(self.ctx())
        self.assertEqual(out["status"], "deferred")
        self.assertEqual(out["reason"], "failed")
        self.assertEqual(out["jev_calls"], 0)
        # Ref not advanced: empty default means never written.
        self.assertEqual(self.ref(str(path)), "")


class ExportTests(CaptureCase):
    def setUp(self):
        super().setUp()
        from datetime import datetime as _dt
        today = date.today()
        noon_ms = int(_dt(today.year, today.month, today.day, 12).timestamp() * 1000)
        self.capture_id = annotations.insert_capture(
            self.conn, project_id=PID, session_file="/tmp/s.jsonl",
            message_ref="0:m1", kind="todo", text="TODO: ship it",
            actionable=0.9, durable=0.8, created_ms=noon_ms)

    def _page_stubs(self, content="# Demo\n", revision="rev1"):
        seen = {}

        def fake_read(graph, req):
            self.assertEqual(req, {"project_id": PID})
            return {"path": "pages/Demo.md", "page": "Demo",
                    "graphName": "g", "revision": revision,
                    "content": content}

        def fake_update(graph, path, rev, new_content):
            seen.update({"path": path, "revision": rev,
                         "content": new_content})
            self.assertEqual(rev, revision)
            return {"path": path, "page": "Demo", "graphName": "g",
                    "revision": "rev2", "content": new_content,
                    "todos": []}

        return fake_read, fake_update, seen

    def test_prepare_apply_page_exact(self):
        fake_read, fake_update, seen = self._page_stubs()
        with patch("project_planner.read_page", side_effect=fake_read), \
                patch("project_planner.update_page", side_effect=fake_update):
            prepped = cap.prepare_export(self.conn, self.capture_id,
                                         graph=self.base, now_ms=NOW)
        self.assertEqual(prepped["target"], "page")
        preview = prepped["preview"]
        self.assertEqual(preview["target"], "page")
        self.assertEqual(preview["block"], "- ## Session logs\n\t- TODO TODO: ship it")
        self.assertEqual(preview["revision"], "rev1")
        self.assertEqual(prepped["revision"], "rev1")
        with patch("project_planner.read_page", side_effect=fake_read), \
                patch("project_planner.update_page", side_effect=fake_update):
            applied = cap.apply_export(self.conn, prepped["prepared"],
                                       graph=self.base, now_ms=NOW + 5)
        self.assertTrue(applied["applied"])
        self.assertEqual(applied["target"], "page")
        self.assertEqual(applied["todo_ref"], "")
        self.assertEqual(seen["content"],
                         "# Demo\n- ## Session logs\n\t- TODO TODO: ship it\n")
        row = self.conn.execute(
            "SELECT status, applied_target FROM captures WHERE id = ?",
            (self.capture_id,)).fetchone()
        self.assertEqual(row["status"], "accepted")
        self.assertEqual(row["applied_target"], "page")
        # Degraded (no session): no session_meta row, no index rows.
        self.assertIsNone(annotations.get_session_meta(
            self.conn, "a" * 32))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM content_index")
            .fetchone()["n"], 0)
        # Second apply of the same token is refused.
        with patch("project_planner.update_page", side_effect=fake_update):
            with self.assertRaises(cap.CaptureError):
                cap.apply_export(self.conn, prepped["prepared"],
                                 graph=self.base, now_ms=NOW + 6)

    def test_page_nests_under_existing_session_logs_heading(self):
        fake_read, fake_update, seen = self._page_stubs(
            content="- ## Session logs\n\t- old\n- ## Next\n")

        with patch("project_planner.read_page", side_effect=fake_read):
            prepped = cap.prepare_export(self.conn, self.capture_id,
                                         graph=self.base, now_ms=NOW)
        with patch("project_planner.update_page", side_effect=fake_update):
            cap.apply_export(self.conn, prepped["prepared"],
                             graph=self.base, now_ms=NOW + 1)
        self.assertEqual(
            seen["content"],
            "- ## Session logs\n\t- old\n\t- TODO TODO: ship it\n- ## Next\n")
        self.assertEqual(prepped["preview"]["block"],
                         "\t- TODO TODO: ship it")

    def test_stale_revision_burns_token(self):
        def fake_read(graph, req):
            return {"path": "pages/Demo.md", "page": "Demo",
                    "graphName": "g", "revision": "old",
                    "content": "# Demo\n"}

        def stale_update(graph, path, revision, content):
            raise ValueError("page revision is stale; reload the page")

        with patch("project_planner.read_page", side_effect=fake_read):
            prepped = cap.prepare_export(self.conn, self.capture_id,
                                         graph=self.base, now_ms=NOW)
        with patch("project_planner.update_page", side_effect=stale_update):
            with self.assertRaises(cap.CaptureError):
                cap.apply_export(self.conn, prepped["prepared"],
                                 graph=self.base, now_ms=NOW + 1)
        with patch("project_planner.update_page", side_effect=stale_update):
            with self.assertRaises(cap.CaptureError) as ctx:
                cap.apply_export(self.conn, prepped["prepared"],
                                 graph=self.base, now_ms=NOW + 2)
        self.assertIn("unknown or expired", str(ctx.exception))

    def test_capture_already_accepted_refused(self):
        def fake_read(graph, req):
            return {"path": "pages/Demo.md", "page": "Demo",
                    "graphName": "g", "revision": "r1", "content": ""}

        with patch("project_planner.read_page", side_effect=fake_read):
            prepped = cap.prepare_export(self.conn, self.capture_id,
                                         graph=self.base, now_ms=NOW)
            annotations.set_capture_status(self.conn, self.capture_id,
                                           "accepted")
            with self.assertRaises(cap.CaptureError):
                cap.apply_export(self.conn, prepped["prepared"],
                                 graph=self.base, now_ms=NOW + 1)

    def test_set_status_guards(self):
        with self.assertRaises(cap.CaptureError):
            cap.set_status(self.conn, self.capture_id, "new")
        with self.assertRaises(cap.CaptureError):
            cap.set_status(self.conn, 999999, "accepted")
        result = cap.set_status(self.conn, self.capture_id, "accepted")
        self.assertTrue(result["ok"])

    def test_list_day_status_filters(self):
        today = date.today().isoformat()
        out = cap.list_captures(self.conn, day=today, status="new", limit=20)
        self.assertEqual(len(out["captures"]), 1)
        self.assertIn("project_name", out["captures"][0])
        # S-041: the day total rides along for the "showing X of Y"
        # line even when the rows fit.
        self.assertEqual(out["total"], 1)
        annotations.set_capture_status(self.conn, self.capture_id,
                                       "dismissed")
        self.assertEqual(
            len(cap.list_captures(self.conn, day=today, status="new")
                ["captures"]), 0)
        self.assertEqual(
            len(cap.list_captures(self.conn, day=today, status="dismissed")
                ["captures"]), 1)
        self.assertEqual(
            len(cap.list_captures(self.conn, day=today, status="all")
                ["captures"]), 1)
        # A different day finds nothing.
        self.assertEqual(
            len(cap.list_captures(self.conn, day="1970-01-01",
                                  status="all")["captures"]), 0)

    def test_set_status_guards(self):
        with self.assertRaises(cap.CaptureError):
            cap.set_status(self.conn, self.capture_id, "new")
        with self.assertRaises(cap.CaptureError):
            cap.set_status(self.conn, 999999, "accepted")
        result = cap.set_status(self.conn, self.capture_id, "accepted")
        self.assertTrue(result["ok"])


SID = "b" * 32


class TodoProvenanceTests(CaptureCase):
    """Phase 2c §5.4: page-only block with session provenance + writeback."""

    def setUp(self):
        super().setUp()
        from datetime import datetime as _dt
        today = date.today()
        noon_ms = int(_dt(today.year, today.month, today.day, 12).timestamp() * 1000)
        self.capture_id = annotations.insert_capture(
            self.conn, project_id=PID, session_file="/tmp/s.jsonl",
            message_ref="0:m1", kind="todo", text="TODO: ship it",
            actionable=0.9, durable=0.8, created_ms=noon_ms)

    def _stubs(self, content="# Demo\n", revision="rev1"):
        seen = {}

        def fake_read(graph, req):
            return {"path": "pages/Demo.md", "page": "Demo",
                    "graphName": "g", "revision": revision,
                    "content": content}

        def fake_update(graph, path, rev, new_content):
            seen["content"] = new_content
            return {"path": path, "page": "Demo", "graphName": "g",
                    "revision": "rev2", "content": new_content,
                    "todos": []}

        return fake_read, fake_update, seen

    def _prepare_apply(self, **kwargs):
        fake_read, fake_update, seen = self._stubs()
        with patch("project_planner.read_page", side_effect=fake_read):
            prepped = cap.prepare_export(
                self.conn, self.capture_id, graph=self.base,
                now_ms=NOW, **kwargs)
        with patch("project_planner.update_page", side_effect=fake_update):
            applied = cap.apply_export(self.conn, prepped["prepared"],
                                       graph=self.base, now_ms=NOW + 5)
        return prepped, applied, seen

    def test_session_markers_shape_and_order(self):
        prepped, _applied, _seen = self._prepare_apply(
            session_id=SID, ref="file:///tmp/work.py")
        block = prepped["preview"]["block"]
        lines = block.split("\n")
        self.assertEqual(lines[0], "- ## Session logs")
        self.assertEqual(lines[1], "\t- TODO TODO: ship it")
        self.assertEqual(
            lines[2], "\t  quickshell-session:: " + SID)
        self.assertEqual(
            lines[3], "\t  quickshell-ref:: file:///tmp/work.py")
        self.assertEqual(len(lines), 4)

    def test_apply_writes_todo_refs_refs_and_index(self):
        _prepped, applied, _seen = self._prepare_apply(
            session_id=SID, ref="file:///tmp/work.py")
        self.assertEqual(applied["todo_ref"], "pages/Demo.md:2")
        meta = annotations.get_session_meta(self.conn, SID)
        self.assertEqual(meta["todo_refs"], ["pages/Demo.md:2"])
        self.assertEqual(meta["refs"], ["file:///tmp/work.py"])
        self.assertEqual(meta["thought_ref"], "")
        self.assertEqual(meta["attended"], 0)
        rows = self.conn.execute(
            "SELECT text, page, line, kind, session_id, project_id"
            " FROM content_index WHERE content_index MATCH 'ship'").fetchall()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertIn("TODO TODO: ship it", row["text"])
        self.assertIn("quickshell-session:: " + SID, row["text"])
        self.assertEqual(row["page"], "Demo")
        self.assertEqual(row["line"], 2)
        self.assertEqual(row["kind"], "todo")
        self.assertEqual(row["session_id"], SID)
        self.assertEqual(row["project_id"], PID)

    def test_second_accept_appends_ref_without_duplicates(self):
        self._prepare_apply(session_id=SID, ref="file:///tmp/work.py")
        second = annotations.insert_capture(
            self.conn, project_id=PID, session_file="/tmp/s.jsonl",
            message_ref="0:m2", kind="todo", text="TODO: second",
            actionable=0.5, durable=0.5, created_ms=NOW)
        fake_read, fake_update, _seen = self._stubs(
            content="# Demo\n- ## Session logs\n\t- TODO TODO: ship it\n",
            revision="rev9")

        def read2(graph, req):
            return {"path": "pages/Demo.md", "page": "Demo",
                    "graphName": "g", "revision": "rev9",
                    "content": "# Demo\n- ## Session logs\n\t- TODO TODO: ship it\n"}

        with patch("project_planner.read_page", side_effect=read2):
            prepped = cap.prepare_export(
                self.conn, second, graph=self.base, now_ms=NOW + 100,
                session_id=SID, ref="file:///tmp/work.py")
        with patch("project_planner.update_page", side_effect=fake_update):
            cap.apply_export(self.conn, prepped["prepared"],
                             graph=self.base, now_ms=NOW + 105)
        meta = annotations.get_session_meta(self.conn, SID)
        self.assertEqual(len(meta["todo_refs"]), 2)
        # Same ref identity is not duplicated.
        self.assertEqual(meta["refs"], ["file:///tmp/work.py"])

    def test_sensitive_ref_drops_line_keeps_session(self):
        prepped, applied, _seen = self._prepare_apply(
            session_id=SID, ref="/home/u/.ssh/id_rsa")
        block = prepped["preview"]["block"]
        self.assertIn("quickshell-session:: " + SID, block)
        self.assertNotIn("quickshell-ref", block)
        meta = annotations.get_session_meta(self.conn, SID)
        self.assertEqual(meta["refs"], [])
        self.assertEqual(len(meta["todo_refs"]), 1)

    def test_overlong_ref_drops_line(self):
        prepped, _applied, _seen = self._prepare_apply(
            session_id=SID, ref="x" * 161)
        self.assertNotIn("quickshell-ref", prepped["preview"]["block"])
        self.assertIn("quickshell-session:: " + SID,
                      prepped["preview"]["block"])

    def test_malformed_session_fails_closed(self):
        fake_read, _fake_update, _seen = self._stubs()
        with patch("project_planner.read_page", side_effect=fake_read):
            with self.assertRaises(cap.CaptureError) as ctx:
                cap.prepare_export(self.conn, self.capture_id,
                                   graph=self.base, now_ms=NOW,
                                   session_id="not-hex")
        self.assertIn("session id is invalid", str(ctx.exception))

    def test_nonstring_ref_fails_closed(self):
        fake_read, _fake_update, _seen = self._stubs()
        with patch("project_planner.read_page", side_effect=fake_read):
            with self.assertRaises(cap.CaptureError):
                cap.prepare_export(self.conn, self.capture_id,
                                   graph=self.base, now_ms=NOW,
                                   session_id=SID, ref=123)

    def test_dismissed_rows_write_no_index(self):
        cap.set_status(self.conn, self.capture_id, "dismissed")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM content_index")
            .fetchone()["n"], 0)
        # Dedupe history survives: the row is still there as dismissed.
        row = self.conn.execute(
            "SELECT status FROM captures WHERE id = ?",
            (self.capture_id,)).fetchone()
        self.assertEqual(row["status"], "dismissed")


class TodayTokenRejectionTests(CaptureCase):
    """D1: the deleted today branch leaves no compatibility read path."""

    def setUp(self):
        super().setUp()
        from datetime import datetime as _dt
        today = date.today()
        noon_ms = int(_dt(today.year, today.month, today.day, 12).timestamp() * 1000)
        self.capture_id = annotations.insert_capture(
            self.conn, project_id=PID, session_file="/tmp/s.jsonl",
            message_ref="0:m1", kind="todo", text="TODO: ship it",
            actionable=0.9, durable=0.8, created_ms=noon_ms)

    def _store_today_token(self):
        payload = {"capture_id": self.capture_id, "target": "today",
                   "path": "pages/Demo.md", "revision": "r1",
                   "content": "# Demo\n- TODO x\n"}
        preview = {"target": "today", "path": "pages/Demo.md",
                   "page": "Demo", "graphName": "g", "revision": "r1",
                   "block": "- TODO x"}
        annotations.create_prepared(
            self.conn, token="capt_" + "c" * 16, kind="capture-today",
            payload=payload, preview=preview, revision="r1",
            expires_ms=NOW + 600_000)
        return "capt_" + "c" * 16

    def test_old_today_token_rejected_and_burned(self):
        token = self._store_today_token()
        with self.assertRaises(cap.CaptureError) as ctx:
            cap.apply_export(self.conn, token, graph=self.base,
                             now_ms=NOW + 1)
        self.assertIn("invalid", str(ctx.exception))
        # Single-use: the rejected token is burned.
        with self.assertRaises(cap.CaptureError) as ctx2:
            cap.apply_export(self.conn, token, graph=self.base,
                             now_ms=NOW + 2)
        self.assertIn("unknown or expired", str(ctx2.exception))
        # The capture itself is untouched (still new, no page write).
        row = self.conn.execute(
            "SELECT status FROM captures WHERE id = ?",
            (self.capture_id,)).fetchone()
        self.assertEqual(row["status"], "new")

    def test_unknown_kind_rejected(self):
        annotations.create_prepared(
            self.conn, token="capp_" + "d" * 16, kind="journal",
            payload={"capture_id": self.capture_id},
            preview={"block": "x"}, revision="r1",
            expires_ms=NOW + 600_000)
        with self.assertRaises(cap.CaptureError):
            cap.apply_export(self.conn, "capp_" + "d" * 16,
                             graph=self.base, now_ms=NOW + 1)


def run_cli(args, env_extra=None):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "session_capture.py"), *args],
        text=True, capture_output=True, timeout=30, check=False, env=env)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.db = str(self.base / "annotations.db")
        self.settings_file = str(self.base / "settings.json")
        self.settings_file = str(self.settings_file)
        Path(self.settings_file).write_text(json.dumps({"memory": {}}),
                                             encoding="utf-8")
        self.env = {"QS_ANNOTATIONS_DB": self.db,
                    "QUICKSHELL_SETTINGS": self.settings_file,
                    "PI_CODING_AGENT_SESSION_DIR": str(self.base / "pi"),
                    "OPENROUTER_API_KEY": ""}

    def test_cli_validation_errors_bounded(self):
        bad = run_cli(["--db", self.db, "scan", "--limit", "99"],
                      env_extra=self.env)
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("error:", bad.stderr)
        self.assertNotIn("Traceback", bad.stderr)
        self.assertEqual(bad.stdout, "")
        bad2 = run_cli(["--db", self.db, "list", "--limit", "0"],
                       env_extra=self.env)
        self.assertNotEqual(bad2.returncode, 0)
        self.assertIn("error:", bad2.stderr)
        self.assertNotIn("Traceback", bad2.stderr)
        bad3 = run_cli(["--db", self.db, "set-status", "--id", "1",
                        "--status", "bogus"], env_extra=self.env)
        self.assertNotEqual(bad3.returncode, 0)
        self.assertIn("error:", bad3.stderr)
        self.assertNotIn("Traceback", bad3.stderr)
        # The --target branch is deleted (L1 single save target).
        bad4 = run_cli(["--db", self.db, "prepare", "--capture", "1",
                        "--target", "page"], env_extra=self.env)
        self.assertNotEqual(bad4.returncode, 0)
        self.assertIn("error:", bad4.stderr)
        self.assertNotIn("Traceback", bad4.stderr)

    def test_cli_list_empty_ok(self):
        completed = run_cli(["--db", self.db, "list"], env_extra=self.env)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertIn("captures", payload)

    def test_cli_scan_disabled_ok(self):
        completed = run_cli(
            ["--db", self.db, "scan", "--project", PID], env_extra=self.env)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertIn(payload["status"], ("skipped", "deferred"))


if __name__ == "__main__":
    unittest.main()
