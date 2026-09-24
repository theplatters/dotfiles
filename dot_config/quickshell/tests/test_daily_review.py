"""Backend tests for evening review / morning plan (scripts/daily_review.py).

Offline: injected runner/evidence, patched graph helpers (list_agenda,
todos, session_changes, journal_assistant, read_page), temp XDG dirs,
``tick.make_decider``, no network in tests.
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import annotations
import daily_review as rev
import memory_tick as tick

PID = "11111111-1111-4111-8111-111111111111"
PID2 = "22222222-2222-4222-8222-222222222222"
DAY = datetime.date(2026, 9, 18)


def ms(day, hour, minute=0):
    return int(datetime.datetime(day.year, day.month, day.day, hour,
                                 minute).timestamp() * 1000)


def day_window(day=DAY):
    return rev._day_window(day)


def make_session(index, start, end, project=PID, apps=("app-a", "app-b"),
                 events=10, status="closed"):
    effective = "closed" if status == "closed" else "active"
    return {
        "session_id": "%032x" % index,
        "device_id": "d" * 32,
        "project": {"id": project} if project else None,
        "start_ms": start,
        "end_ms": end,
        "event_count": events,
        "status": status,
        "ended_reason": "inactivity" if status == "closed" else None,
        "active": status != "closed",
        "effective_status": effective,
        "applications": list(apps),
        "resources": [],
    }


class FakeEvidence:
    """Overlap-range evidence stub (oldest-first, like the collector)."""

    def __init__(self, sessions=(), fail_with=None):
        self.sessions = [dict(s) for s in sessions]
        self.fail_with = fail_with
        self.calls = []

    def list_sessions(self, *, limit, from_ms=None, to_ms=None):
        self.calls.append({"limit": limit, "from_ms": from_ms,
                           "to_ms": to_ms})
        if self.fail_with is not None:
            raise self.fail_with("boom-detail")
        lo = 0 if from_ms is None else from_ms
        hi = tick.I64_MAX if to_ms is None else to_ms
        rows = [dict(s) for s in self.sessions
                if s["end_ms"] >= lo and s["start_ms"] < hi]
        rows.sort(key=lambda s: (s["start_ms"], s["end_ms"],
                                 s["session_id"]))
        return rows[:limit]

    def get_session(self, session_id):
        for session in self.sessions:
            if session["session_id"] == session_id.lower():
                return dict(session)
        return None


class ReviewCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.db = str(self.base / "annotations.db")
        self.conn = annotations.connect(self.db)
        self.addCleanup(self.conn.close)
        self.graph = self.base / "graph"
        (self.graph / "pages").mkdir(parents=True)
        (self.graph / "journals").mkdir(parents=True)
        self._env = patch.dict(os.environ,
                               {"OPENROUTER_API_KEY": "test-key"},
                               clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)
        os.environ.pop("LOGSEQ_GRAPH", None)
        self.calls = []
        self.answers = {}
        # Patched graph helpers (module attributes, like capture tests).
        self.agenda_tasks = []
        self.todos_rows = []
        self.changes_map = {}
        self.journal_content = ""
        self.journal_rev = "rev0"
        patches = [
            patch("daily_agenda.list_agenda",
                  side_effect=lambda g, d=None: {"tasks": list(
                      self.agenda_tasks)}),
            patch("logseq_todos.todos",
                  side_effect=lambda g, q=None: list(self.todos_rows)),
            patch("project_session_changes.session_changes",
                  side_effect=self._fake_changes),
            patch("journal_assistant._current_journal",
                  side_effect=lambda g, date=None, create=False:
                  (b"", None, None)),
            patch("journal_assistant.context",
                  side_effect=self._fake_context),
            patch("journal_assistant.prepare",
                  side_effect=self._fake_prepare),
            patch("journal_assistant.append",
                  side_effect=self._fake_append),
        ]
        for entered in patches:
            entered.start()
            self.addCleanup(entered.stop)

    # -- fakes ---------------------------------------------------------
    def _fake_changes(self, project_id, session_id):
        entry = self.changes_map.get(str(project_id))
        if entry is None:
            return {"available": False, "reason": "no linked repository",
                    "project_id": project_id, "session_id": session_id,
                    "summary_key": "no-repo"}
        return dict(entry)

    def _fake_context(self, graph, query=None):
        return {"content": self.journal_content,
                "revision": self.journal_rev, "date": "2026_09_18",
                "path": "journals/2026_09_18.md"}

    def _fake_prepare(self, graph, date, revision, text):
        if revision != self.journal_rev:
            raise ValueError("journal revision is stale;"
                             " reload the journal")
        return {"date": date, "path": "journals/" + date + ".md",
                "revision": revision, "addition": text}

    def _fake_append(self, graph, date, revision, addition):
        if revision != self.journal_rev:
            raise ValueError("journal revision is stale;"
                             " reload the journal")
        self.journal_content += addition
        self.journal_rev = "rev-applied"
        return {"date": date, "path": "journals/" + date + ".md",
                "revision": self.journal_rev, "line": 1}

    def runner(self, url, body, headers, timeout):
        payload = json.loads(body)
        self.calls.append(payload)
        answers = {}
        for name in payload.get("questions", {}):
            answers[name] = {"noul": self.answers.get(name, 0.85)}
        return 200, json.dumps({"answers": answers,
                                "usage": {"cost": 0.00001}}).encode()

    def settings(self, **over):
        s = {"enabled": True, "dailyReview": True,
             "reviewTime": "18:00", "morningTime": "07:00"}
        s.update(over)
        return s

    def ctx(self, evidence, settings=None, now=None,
            graph="sentinel"):
        settings = settings or self.settings()
        stamp = ms(DAY, 19) if now is None else now
        graph_value = self.graph if graph == "sentinel" else graph
        # Local deterministic review: no API key, no model callback.
        return {"conn": self.conn, "settings": settings, "now_ms": stamp,
                "evidence": evidence, "manual": False,
                "graph": graph_value}

    def sessions_today(self, project=PID, count=1):
        start, _ = day_window()
        return [make_session(i + 1, start + 3_600_000 + i * 1000,
                             start + 5_400_000 + i * 1000,
                             project=project)
                for i in range(count)]

    def last_review(self, kind):
        return annotations.get_state(
            self.conn, f"last_review_day:{kind}", default="")

    def todo_row(self, index, task, page="Notes"):
        path = str(self.graph / "pages" / f"{page}.md")
        return {"task": task, "path": path, "line": index,
                "page": page, "graph": "g"}


class ReviewStorageTests(ReviewCase):
    def test_save_get_roundtrip(self):
        payload = {"kind": "evening", "day": "2026-09-18",
                   "sections": {}, "top": [], "markdown": "# hi"}
        rev_now = ms(DAY, 19)
        annotations.save_review(self.conn, "2026-09-18", "evening",
                                payload, rev_now)
        row = annotations.get_review(self.conn, "2026-09-18", "evening")
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["json"], payload)
        self.assertEqual(row["generated_ms"], rev_now)
        self.assertFalse(row["polished"])
        self.assertIsNone(row["saved_ms"])
        self.assertIsNone(annotations.get_review(
            self.conn, "2026-09-18", "morning"))

    def test_save_refresh_keeps_polished_and_saved(self):
        payload = {"sections": {}}
        annotations.save_review(self.conn, "2026-09-18", "morning",
                                payload, 100)
        self.assertTrue(annotations.mark_review_polished(
            self.conn, "2026-09-18", "morning"))
        self.assertTrue(annotations.mark_review_saved(
            self.conn, "2026-09-18", "morning", 200))
        annotations.save_review(self.conn, "2026-09-18", "morning",
                                {"sections": {}, "v": 2}, 300)
        row = annotations.get_review(self.conn, "2026-09-18", "morning")
        assert row is not None
        self.assertTrue(row["polished"])
        self.assertEqual(row["saved_ms"], 200)
        self.assertEqual(row["json"]["v"], 2)

    def test_marks_missing_are_false(self):
        self.assertFalse(annotations.mark_review_polished(
            self.conn, "2026-09-18", "evening"))
        self.assertFalse(annotations.mark_review_saved(
            self.conn, "2026-09-18", "evening", 1))

    def test_validation_bounded(self):
        for day, kind in (("not-a-day", "evening"),
                          (("2026-13-45"), "evening"),
                          (("2026-09-18"), "noon"),
                          (("2026-09-18"), "")):
            with self.assertRaises(annotations.AnnotationsError):
                annotations.save_review(self.conn, day, kind, {}, 1)
            with self.assertRaises(annotations.AnnotationsError):
                annotations.get_review(self.conn, day, kind)
        with self.assertRaises(annotations.AnnotationsError):
            annotations.save_review(self.conn, "2026-09-18", "evening",
                                    {"x": "y"}, -1)
        with self.assertRaises(annotations.AnnotationsError):
            annotations.save_review(self.conn, "2026-09-18", "evening",
                                    ["not", "a", "dict"], 1)
        with self.assertRaises(annotations.AnnotationsError):
            annotations.save_review(
                self.conn, "2026-09-18", "evening",
                {"blob": "x" * (64 * 1024)}, 1)

    def test_prepared_review_journal_kind(self):
        annotations.create_prepared(
            self.conn, token="rev_abc123", kind="review-journal",
            payload={"kind": "evening"}, preview={"target": "journal"},
            revision="r1", expires_ms=5000)
        row = annotations.get_prepared(self.conn, "rev_abc123")
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["kind"], "review-journal")
        with self.assertRaises(annotations.AnnotationsError):
            annotations.create_prepared(
                self.conn, token="rev_xyz", kind="review-bogus",
                payload={}, preview={}, revision=None, expires_ms=1)


class AdapterTests(ReviewCase):
    def test_not_due_before_times(self):
        out = rev.review(self.ctx(FakeEvidence(self.sessions_today()),
                                  now=ms(DAY, 6)))
        self.assertEqual(out["reason"], "not_due")
        self.assertEqual(out["status"], "skipped")
        self.assertEqual(out["jev_calls"], 0)
        self.assertFalse(out["changed"])
        self.assertEqual(self.last_review("morning"), "")
        self.assertEqual(self.last_review("evening"), "")

    def test_morning_due_once_idempotent(self):
        # Morning generates even with zero sessions (popup-open path).
        first = rev.review(self.ctx(FakeEvidence([]), now=ms(DAY, 8)))
        self.assertTrue(first["changed"], first)
        self.assertEqual(first["jev_calls"], 0)
        self.assertEqual(self.last_review("morning"), "2026-09-18")
        self.assertEqual(self.last_review("evening"), "")
        row = annotations.get_review(self.conn, "2026-09-18", "morning")
        self.assertIsNotNone(row)
        second = rev.review(
            self.ctx(FakeEvidence(self.sessions_today()),
                     now=ms(DAY, 8, 1)))
        self.assertEqual(second["reason"], "not_due")
        self.assertFalse(second["changed"])

    def test_evening_due_once_idempotent(self):
        annotations.set_state(self.conn, "last_review_day:morning",
                              "2026-09-18")
        first = rev.review(self.ctx(FakeEvidence(self.sessions_today()),
                                    now=ms(DAY, 19)))
        self.assertTrue(first["changed"], first)
        self.assertEqual(self.last_review("evening"), "2026-09-18")
        row = annotations.get_review(self.conn, "2026-09-18", "evening")
        assert row is not None
        self.assertIn("projects", row["json"]["sections"])
        second = rev.review(self.ctx(FakeEvidence(self.sessions_today()),
                                     now=ms(DAY, 20)))
        self.assertEqual(second["reason"], "not_due")
        self.assertFalse(second["changed"])

    def test_both_due_morning_first(self):
        out = rev.review(self.ctx(FakeEvidence(self.sessions_today()),
                                  now=ms(DAY, 19)))
        self.assertTrue(out["changed"], out)
        self.assertEqual(self.last_review("morning"), "2026-09-18")
        self.assertEqual(self.last_review("evening"), "2026-09-18")
        morning = annotations.get_review(self.conn, "2026-09-18",
                                         "morning")
        evening = annotations.get_review(self.conn, "2026-09-18",
                                         "evening")
        assert morning is not None and evening is not None
        self.assertEqual(morning["json"]["top"][0]["target_date"] \
            if morning["json"]["top"] else "2026-09-18", "2026-09-18")
        for item in evening["json"]["top"]:
            self.assertEqual(item["target_date"], "2026-09-19")

    def test_evening_no_activity_retries_later(self):
        skipped = rev.review(self.ctx(FakeEvidence([]), now=ms(DAY, 8)))
        # Morning still generated; evening skipped quietly below.
        self.assertTrue(skipped["changed"])
        self.assertEqual(self.last_review("evening"), "")
        annotations.set_state(self.conn, "last_review_day:morning",
                              "2026-09-18")
        quiet = rev.review(self.ctx(FakeEvidence([]), now=ms(DAY, 19)))
        self.assertEqual(quiet["reason"], "no_activity")
        self.assertFalse(quiet["changed"])
        self.assertEqual(self.last_review("evening"), "")
        # Late-evening work is still reviewed on the next tick.
        later = rev.review(self.ctx(FakeEvidence(self.sessions_today()),
                                    now=ms(DAY, 21)))
        self.assertTrue(later["changed"], later)
        self.assertEqual(self.last_review("evening"), "2026-09-18")

    def test_disabled_flag(self):
        out = rev.review(self.ctx(
            FakeEvidence(self.sessions_today()),
            settings=self.settings(dailyReview=False), now=ms(DAY, 19)))
        self.assertEqual(out["reason"], "disabled")
        self.assertEqual(self.calls, [])
        self.assertFalse(out["changed"])

    def test_evidence_failure_is_graph_unavailable(self):
        broken = FakeEvidence(fail_with=RuntimeError)
        out = rev.review(self.ctx(broken, now=ms(DAY, 19)))
        self.assertEqual(out["reason"], "graph_unavailable")
        self.assertEqual(out["status"], "skipped")
        self.assertEqual(self.last_review("morning"), "")
        self.assertEqual(self.last_review("evening"), "")

    def test_manual_run_job_review(self):
        start, _ = day_window()
        sessions = [make_session(1, start + 3_600_000,
                                 start + 5_400_000)]
        code, payload = tick._do_run(
            self.db, "review", None, None,
            settings_injected=self.settings(),
            clock=lambda: ms(DAY, 19),
            evidence=FakeEvidence(sessions))
        self.assertEqual(code, 0)
        self.assertTrue(payload["changed"], payload)
        self.assertEqual(payload["jev_calls"], 0)
        # Deterministic order only: no network, still stores both.
        self.assertIsNotNone(annotations.get_review(
            self.conn, "2026-09-18", "morning"))
        self.assertIsNotNone(annotations.get_review(
            self.conn, "2026-09-18", "evening"))


class DeterministicTopTests(ReviewCase):
    def setUp(self):
        super().setUp()
        self.todos_rows = [self.todo_row(i + 1, f"task number {i + 1}")
                           for i in range(4)]

    def test_deterministic_order_no_model(self):
        out = rev.review(self.ctx(
            FakeEvidence(self.sessions_today()), now=ms(DAY, 8)))
        self.assertTrue(out["changed"])
        self.assertEqual(out["jev_calls"], 0)
        row = annotations.get_review(self.conn, "2026-09-18", "morning")
        assert row is not None
        self.assertFalse(row["json"]["prioritized"])
        top = row["json"]["top"]
        self.assertEqual(len(top), 3)
        self.assertEqual([t["task"] for t in top],
                         ["task number 1", "task number 2",
                          "task number 3"])
        self.assertTrue(all(t["score"] is None for t in top))


class SectionTests(ReviewCase):
    def test_projects_bounds_order_and_other(self):
        start, _ = day_window()
        sessions = []
        for i in range(10):
            pid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"proj-{i}"))
            sessions.append(make_session(i + 1, start + 3_600_000,
                                         start + 3_600_000
                                         + (10 - i) * 600_000,
                                         project=pid))
        sessions.append(make_session(99, start + 3_600_000,
                                     start + 3_600_000 + 200 * 600_000,
                                     project=None))
        payload = rev.generate_review(
            self.conn, kind="evening", day="2026-09-18",
            settings=self.settings(), now_ms=ms(DAY, 19),
            evidence=FakeEvidence(sessions), graph=self.graph)
        projects = payload["sections"]["projects"]
        self.assertLessEqual(len(projects), 8)
        minutes = [p["minutes"] for p in projects]
        self.assertEqual(minutes, sorted(minutes, reverse=True))
        others = [p for p in projects if p["project_id"] is None]
        self.assertEqual(len(others), 1)
        self.assertEqual(others[0]["name"], "other")

    def test_changes_short_hashes_and_unavailable(self):
        start, _ = day_window()
        sessions = [make_session(1, start + 3_600_000,
                                 start + 5_400_000, project=PID),
                    make_session(2, start + 3_600_000,
                                 start + 5_400_000, project=PID2)]
        self.changes_map[PID] = {
            "available": True, "project_id": PID, "session_id": "x",
            "baseline_commit": "abc1234567890",
            "latest_commit": "def5678901234", "has_baseline": True,
            "summary_key": "k"}
        payload = rev.generate_review(
            self.conn, kind="evening", day="2026-09-18",
            settings=self.settings(), now_ms=ms(DAY, 19),
            evidence=FakeEvidence(sessions), graph=self.graph)
        changes = {c["project_id"]: c
                   for c in payload["sections"]["changes"]}
        self.assertEqual(changes[PID]["commits"], "abc1234..def5678")
        self.assertEqual(changes[PID2]["commits"], "none")
        self.assertEqual(changes[PID2]["note"], "unavailable")

    def test_captures_counts(self):
        start, _ = day_window()
        new_id = annotations.insert_capture(
            self.conn, project_id=PID, session_file="/tmp/s.jsonl",
            message_ref="0:m1", kind="todo", text="TODO: new",
            actionable=0.9, durable=0.8, created_ms=start + 1000)
        acc_id = annotations.insert_capture(
            self.conn, project_id=PID, session_file="/tmp/s.jsonl",
            message_ref="0:m2", kind="todo", text="TODO: acc",
            actionable=0.9, durable=0.8, created_ms=start + 2000)
        annotations.set_capture_status(self.conn, acc_id, "accepted")
        app_id = annotations.insert_capture(
            self.conn, project_id=PID, session_file="/tmp/s.jsonl",
            message_ref="0:m3", kind="todo", text="TODO: app",
            actionable=0.9, durable=0.8, created_ms=start + 3000)
        annotations.mark_capture_applied(self.conn, app_id, "page",
                                         now_ms=start + 4000)
        _ = new_id
        payload = rev.generate_review(
            self.conn, kind="evening", day="2026-09-18",
            settings=self.settings(), now_ms=ms(DAY, 19),
            evidence=FakeEvidence([]), graph=self.graph)
        self.assertEqual(payload["sections"]["captures"],
                         {"new": 1, "accepted": 2, "applied": 1})

    def test_todos_scheduled_completed_mapping(self):
        iso = "2026-09-18"
        self.agenda_tasks = [
            {"scheduledDate": iso, "done": False},
            {"scheduledDate": iso, "done": True},
            {"scheduledDate": "2026-09-19", "done": False},
            {"scheduledDate": "2026-09-10", "done": True},
        ]
        payload = rev.generate_review(
            self.conn, kind="evening", day=iso,
            settings=self.settings(), now_ms=ms(DAY, 19),
            evidence=FakeEvidence([]), graph=self.graph)
        # scheduled counts rows on the day (done or not); completed is
        # the done subset on the day.
        self.assertEqual(payload["sections"]["todos"],
                         {"scheduled": 2, "completed": 1})

    def test_journal_present_and_absent(self):
        with patch("journal_assistant._current_journal",
                   return_value=(b"# hi\n", 0o644, ("dev", 9))):
            payload = rev.generate_review(
                self.conn, kind="evening", day="2026-09-18",
                settings=self.settings(), now_ms=ms(DAY, 19),
                evidence=FakeEvidence([]), graph=self.graph)
        self.assertEqual(payload["sections"]["journal"],
                         {"present": True})
        payload = rev.generate_review(
            self.conn, kind="evening", day="2026-09-18",
            settings=self.settings(), now_ms=ms(DAY, 19),
            evidence=FakeEvidence([]), graph=self.graph)
        self.assertEqual(payload["sections"]["journal"],
                         {"present": False})

    def test_markdown_bounded(self):
        start, _ = day_window()
        sessions = [make_session(i + 1, start + 3_600_000,
                                 start + 5_400_000, project=PID)
                    for i in range(50)]
        self.todos_rows = [self.todo_row(i + 1, "t" * 240)
                           for i in range(12)]
        payload = rev.generate_review(
            self.conn, kind="evening", day="2026-09-18",
            settings=self.settings(), now_ms=ms(DAY, 19),
            evidence=FakeEvidence(sessions), graph=self.graph)
        self.assertLessEqual(len(payload["markdown"].encode("utf-8")),
                             8 * 1024)
        encoded = json.dumps(payload).encode("utf-8")
        self.assertLessEqual(len(encoded), 64 * 1024)

    def test_failed_graph_sections_flagged(self):
        payload = rev.generate_review(
            self.conn, kind="evening", day="2026-09-18",
            settings=self.settings(), now_ms=ms(DAY, 19),
            evidence=FakeEvidence([]), graph=None)
        sections = payload["sections"]
        self.assertIn("todos", sections.get("unavailable", []))
        self.assertIn("journal", sections.get("unavailable", []))
        self.assertEqual(payload["top"], [])


class GetTests(ReviewCase):
    def _args(self, kind, date=None, refresh=False):
        return argparse.Namespace(kind=kind, date=date, refresh=refresh,
                                  db=self.db, graph=str(self.graph))

    def _fake_default_evidence(self, sessions):
        fake = FakeEvidence(sessions)
        entered = patch.object(tick, "DefaultEvidence",
                               return_value=fake)
        entered.start()
        self.addCleanup(entered.stop)
        return fake

    def test_stored_returned_without_regeneration(self):
        payload = {"kind": "morning", "day": "2026-09-18",
                   "sections": {"projects": []}, "top": [],
                   "markdown": "# stored"}
        annotations.save_review(self.conn, "2026-09-18", "morning",
                                payload, ms(DAY, 8))

        class ExplodingEvidence(FakeEvidence):
            def list_sessions(self, **kwargs):
                raise AssertionError("must not regenerate")

        with patch.object(tick, "DefaultEvidence",
                          return_value=ExplodingEvidence()):
            view = rev._do_get(self._args("morning", "2026-09-18"),
                               self.settings(), ms(DAY, 19))
        self.assertTrue(view["found"])
        self.assertEqual(view["review"]["markdown"], "# stored")
        self.assertFalse(view["review"]["polish_enabled"])

    def test_missing_evening_before_time_is_not_due(self):
        fresh_db = str(self.base / "fresh.db")
        args = argparse.Namespace(kind="evening", date="2026-09-18",
                                  refresh=False, db=fresh_db,
                                  graph=str(self.graph))
        view = rev._do_get(args, self.settings(), ms(DAY, 12))
        self.assertEqual(view, {"kind": "evening", "day": "2026-09-18",
                                "found": False, "reason": "not_due"})
        # The read-only path creates nothing.
        self.assertFalse(Path(fresh_db).exists())

    def test_morning_generates_on_demand(self):
        self._fake_default_evidence(self.sessions_today())
        view = rev._do_get(self._args("morning", "2026-09-18"),
                           self.settings(), ms(DAY, 6))
        self.assertTrue(view["found"])
        self.assertEqual(view["day"], "2026-09-18")
        self.assertIn("projects", view["review"]["sections"])
        row = annotations.get_review(self.conn, "2026-09-18", "morning")
        self.assertIsNotNone(row)

    def test_refresh_regenerates(self):
        self._fake_default_evidence(self.sessions_today())
        first = rev._do_get(self._args("morning", "2026-09-18"),
                            self.settings(), ms(DAY, 8))
        self._fake_default_evidence(self.sessions_today(count=3))
        second = rev._do_get(self._args("morning", "2026-09-18",
                                        refresh=True),
                             self.settings(), ms(DAY, 9))
        self.assertTrue(first["found"] and second["found"])
        first_total = sum(p["sessions"] for p in
                          first["review"]["sections"]["projects"])
        second_total = sum(p["sessions"] for p in
                           second["review"]["sections"]["projects"])
        self.assertEqual(first_total, 1)
        self.assertEqual(second_total, 3)

    def test_get_validation(self):
        with self.assertRaises(rev.ReviewError):
            rev._do_get(self._args("noon", "2026-09-18"),
                        self.settings(), ms(DAY, 8))
        with self.assertRaises(rev.ReviewError):
            rev._do_get(self._args("morning", "tomorrow"),
                        self.settings(), ms(DAY, 8))


class ExportTests(ReviewCase):
    def _stored(self):
        annotations.save_review(
            self.conn, "2026-09-18", "evening",
            {"kind": "evening", "day": "2026-09-18",
             "sections": {}, "top": [], "markdown": "# review text"},
            ms(DAY, 19))

    def test_prepare_apply_roundtrip(self):
        self._stored()
        prepped = rev.prepare_save(
            self.conn, "evening", "2026-09-18", graph=self.graph,
            now_ms=ms(DAY, 20))
        self.assertEqual(prepped["target"], "journal")
        self.assertEqual(prepped["kind"], "evening")
        self.assertEqual(prepped["day"], "2026-09-18")
        self.assertEqual(prepped["expires_in"], 600)
        preview = prepped["preview"]
        self.assertEqual(preview["target"], "journal")
        self.assertIn("quickshell-review::2026-09-18-evening",
                      preview["addition"])
        self.assertIn("# review text", preview["addition"])
        self.assertEqual(prepped["revision"], "rev0")
        applied = rev.apply_saved(self.conn, prepped["prepared"],
                                  graph=self.graph, now_ms=ms(DAY, 20) + 5)
        self.assertTrue(applied["applied"])
        self.assertEqual(applied["target"], "journal")
        self.assertEqual(applied["kind"], "evening")
        self.assertEqual(applied["revision"], "rev-applied")
        row = annotations.get_review(self.conn, "2026-09-18", "evening")
        assert row is not None
        self.assertEqual(row["saved_ms"], ms(DAY, 20) + 5)
        # A second apply of the same token is refused.
        with self.assertRaises(rev.ReviewError) as ctx:
            rev.apply_saved(self.conn, prepped["prepared"],
                            graph=self.graph, now_ms=ms(DAY, 20) + 6)
        self.assertIn("unknown or expired", str(ctx.exception))

    def test_marker_refusal(self):
        self._stored()
        self.journal_content = ("# journal\n\n"
                                "quickshell-review::2026-09-18-evening\n")
        with self.assertRaises(rev.ReviewError) as ctx:
            rev.prepare_save(self.conn, "evening", "2026-09-18",
                             graph=self.graph, now_ms=ms(DAY, 20))
        self.assertIn("already saved", str(ctx.exception))

    def test_stale_revision_burns_token(self):
        self._stored()
        prepped = rev.prepare_save(
            self.conn, "evening", "2026-09-18", graph=self.graph,
            now_ms=ms(DAY, 20))
        self.journal_rev = "rev-changed-elsewhere"
        with self.assertRaises(rev.ReviewError) as ctx:
            rev.apply_saved(self.conn, prepped["prepared"],
                            graph=self.graph, now_ms=ms(DAY, 20) + 1)
        self.assertIn("journal write failed", str(ctx.exception))
        with self.assertRaises(rev.ReviewError) as ctx2:
            rev.apply_saved(self.conn, prepped["prepared"],
                            graph=self.graph, now_ms=ms(DAY, 20) + 2)
        self.assertIn("unknown or expired", str(ctx2.exception))

    def test_prepare_unknown_review(self):
        with self.assertRaises(rev.ReviewError) as ctx:
            rev.prepare_save(self.conn, "morning", "2026-09-18",
                             graph=self.graph, now_ms=ms(DAY, 20))
        self.assertIn("unknown review", str(ctx.exception))


def run_cli(args, env_extra=None):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "daily_review.py"),
         *args],
        text=True, capture_output=True, timeout=30, check=False, env=env)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.db = str(self.base / "annotations.db")
        self.settings_file = str(self.base / "settings.json")
        Path(self.settings_file).write_text(json.dumps({"memory": {}}),
                                             encoding="utf-8")
        self.env = {"QS_ANNOTATIONS_DB": self.db,
                    "QUICKSHELL_SETTINGS": self.settings_file,
                    "OPENROUTER_API_KEY": ""}

    def test_cli_validation_errors_bounded(self):
        bad = run_cli(["--db", self.db, "get", "--kind", "noon"],
                      env_extra=self.env)
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("error:", bad.stderr)
        self.assertNotIn("Traceback", bad.stderr)
        self.assertEqual(bad.stdout, "")
        bad2 = run_cli(["--db", self.db, "get", "--kind", "evening",
                        "--date", "tomorrow"], env_extra=self.env)
        self.assertNotEqual(bad2.returncode, 0)
        self.assertIn("error:", bad2.stderr)
        self.assertNotIn("Traceback", bad2.stderr)
        bad3 = run_cli(["--db", self.db, "polish", "--kind", "evening"],
                       env_extra=self.env)
        # The polish command is deleted: the parser rejects it with
        # a bounded single-line error, no content echo.
        self.assertNotEqual(bad3.returncode, 0)
        self.assertIn("error:", bad3.stderr)
        self.assertNotIn("Traceback", bad3.stderr)
        self.assertEqual(bad3.stdout, "")
        bad4 = run_cli(["--db", self.db, "apply", "--prepared",
                        "rev_doesnotexist"], env_extra=self.env)
        self.assertNotEqual(bad4.returncode, 0)
        self.assertIn("error:", bad4.stderr)
        self.assertNotIn("Traceback", bad4.stderr)
        self.assertEqual(bad4.stdout, "")

    def test_cli_rejects_secret_arg_without_echo(self):
        secret = "SECRET-MARKER-9f8e7d6c5b4a"
        completed = run_cli(["--db", self.db, "get", "--kind", secret],
                            env_extra=self.env)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("error:", completed.stderr)
        self.assertNotIn(secret, completed.stdout + completed.stderr)
        self.assertEqual(completed.stdout, "")

    def test_cli_help_exits_zero(self):
        completed = run_cli(["--help"], env_extra=self.env)
        self.assertEqual(completed.returncode, 0)
        self.assertIn("usage", completed.stdout.lower())


if __name__ == "__main__":
    unittest.main()
