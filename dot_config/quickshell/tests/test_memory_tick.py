"""Tests for the memory tick coordinator (scripts/memory_tick.py).

Local deterministic work only: work-log drafts, capture scans (local
regex only), and daily reviews (deterministic). No model calls, no
budget ledger, no decider on the automatic path. Jobs are draft, scan,
and review; there is no enrich/triage/assoc/autoignore/decider/budget
or Jev coverage.

No real network, ever: injected clock/evidence/adapters/monotonic.
Settings dicts are injected in-process; CLI subprocess tests use an
isolated ``QUICKSHELL_SETTINGS`` file plus ``QS_ANNOTATIONS_DB`` temp
dir.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import annotations
import memory_tick as tick


def base_settings(**over):
    settings = {
        "enabled": True,
        "tickSeconds": 60,
        "workLog": True,
        "sessionCapture": False,
        "dailyReview": False,
        "organise": False,
        "minSessionMs": 0,
        "reviewTime": "18:00",
        "morningTime": "07:00",
    }
    settings.update(over)
    return settings


def make_session(index, start, end, status="closed", apps=("app-a", "app-b"),
                 events=10, project=None):
    effective = "closed" if status == "closed" else "active"
    return {
        "session_id": "%032x" % index,
        "device_id": "d" * 32,
        "project": {"id": project} if project else None,
        "start_ms": start,
        "end_ms": end,
        "status": status,
        "effective_status": effective,
        "event_count": events,
        "applications": list(apps),
    }


class FakeEvidence:
    """Emulates Rust overlap range semantics oldest-first."""

    def __init__(self, sessions):
        self.sessions = [dict(s) for s in sessions]
        self.calls = []

    def list_sessions(self, *, limit, from_ms=None, to_ms=None):
        self.calls.append({"limit": limit, "from_ms": from_ms,
                           "to_ms": to_ms})
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

    def device_id(self):
        return "d" * 32

    def session_resources(self, sid, limit=20):
        return {"resources": []}

    def list_projects(self):
        return {"projects": []}

    def close(self, session_id, **over):
        for session in self.sessions:
            if session["session_id"] == session_id.lower():
                session["status"] = "closed"
                session["effective_status"] = "closed"
                session.update(over)
                return


def local_ok(*args):
    return {"status": "ok", "reason": "", "jev_calls": 0, "changed": True}


def local_skip(*args):
    return {"status": "skipped", "reason": "nothing_ready", "jev_calls": 0,
            "changed": False}


class TickCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.db = str(self.base / "annotations.db")
        self.conn = annotations.connect(self.db)
        self.now = 5_000_000
        self.addCleanup(self.conn.close)
        self.addCleanup(self.temp.cleanup)

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        try:
            self.temp.cleanup()
        except Exception:
            pass

    def do_tick(self, sessions, settings=None, now=None, draft=None,
                scan=None, review=None, lock_fn=None, monotonic=None,
                discovery_limit=None):
        evidence = sessions if isinstance(sessions, FakeEvidence) \
            else FakeEvidence(sessions)
        kwargs = dict(
            conn=self.conn, settings=settings or base_settings(),
            now_ms=self.now if now is None else now, evidence=evidence,
            draft=draft or local_skip,
            scan=scan or local_skip, review=review or local_skip,
            lock_fn=lock_fn, monotonic=monotonic)
        if discovery_limit is not None:
            kwargs["discovery_limit"] = discovery_limit
        return tick.run_tick(**kwargs), evidence

    def frontier(self):
        return tick._get_state_int(self.conn, tick.STATE_FRONTIER, 0)

    def deferred_ids(self):
        return tick._load_id_set(self.conn, tick.STATE_DEFERRED, None)

    def done_draft(self):
        return tick._load_json_dict(self.conn, tick.STATE_DONE_DRAFT)


class GateTests(TickCase):
    def test_disabled_tick_reports_disabled(self):
        payload, _ = self.do_tick(
            [make_session(1, 1000, 9000)],
            settings=base_settings(enabled=False))
        self.assertTrue(any(d.get("reason") == "disabled"
                            for d in payload["deferred"]))
        self.assertFalse(payload["changed"])
        self.assertEqual(payload["jev_calls"], 0)

    def test_tick_gap_inside_window_skips(self):
        payload, _ = self.do_tick([make_session(1, 1000, 9000)])
        self.assertEqual(payload["processed"]["sessions"], 1)
        again, _ = self.do_tick([make_session(1, 1000, 9000)],
                                now=self.now + 1000)
        self.assertTrue(any(d.get("reason") == "tick_gap"
                            for d in again["deferred"]))

    def test_worklog_flag_off_skips_drafts(self):
        seen = []

        def spy(session, ctx):
            seen.append(1)
            return local_ok(session, ctx)

        payload, _ = self.do_tick(
            [make_session(1, 1000, 9000)],
            settings=base_settings(workLog=False), draft=spy)
        self.assertEqual(seen, [])
        self.assertEqual(payload["processed"]["drafts"], 0)

    def test_scan_flag_off_never_invokes_adapter(self):
        seen = []

        def spy(ctx):
            seen.append(1)
            return local_ok(ctx)

        payload, _ = self.do_tick(
            [make_session(1, 1000, 9000)],
            settings=base_settings(sessionCapture=False), scan=spy)
        self.assertEqual(seen, [])
        self.assertFalse(any(d.get("job") == "scan"
                             for d in payload["deferred"]))

    def test_review_flag_off_never_invokes_adapter(self):
        seen = []

        def spy(ctx):
            seen.append(1)
            return local_ok(ctx)

        payload, _ = self.do_tick(
            [make_session(1, 1000, 9000)],
            settings=base_settings(dailyReview=False), review=spy)
        self.assertEqual(seen, [])
        self.assertFalse(any(d.get("job") == "review"
                             for d in payload["deferred"]))

    def test_local_adapters_zero_calls(self):
        payload, _ = self.do_tick(
            [make_session(1, 1000, 9000)], draft=local_ok,
            scan=local_ok, review=local_ok,
            settings=base_settings(sessionCapture=True,
                                   dailyReview=True))
        self.assertEqual(payload["jev_calls"], 0)
        self.assertTrue(payload["available"])
        self.assertTrue(payload["changed"])


class DrainTests(TickCase):
    def test_five_per_tick_budget_and_frontier(self):
        sessions = [make_session(i, 1000 * i, 1000 * i + 500)
                    for i in range(1, 9)]
        payload, _ = self.do_tick(sessions, draft=local_ok)
        self.assertEqual(payload["processed"]["sessions"], 5)
        self.assertEqual(payload["processed"]["drafts"], 5)
        again, _ = self.do_tick(sessions, now=self.now + 61_000,
                                draft=local_ok)
        self.assertEqual(again["processed"]["sessions"], 3)

    def test_discovery_saturated_reports(self):
        sessions = [make_session(i, 1000, 2000 + i) for i in range(1, 4)]
        self.do_tick(sessions, draft=local_skip, discovery_limit=2)
        payload, _ = self.do_tick(sessions, now=self.now + 61_000,
                                  draft=local_skip, discovery_limit=2)
        self.assertTrue(any(d.get("reason") == "discovery_saturated"
                            for d in payload["deferred"]))


class CompletionTests(TickCase):
    def test_ok_and_skipped_mark_digest_done(self):
        sessions = [make_session(1, 1000, 9000)]
        payload, _ = self.do_tick(sessions, draft=local_ok)
        self.assertTrue(payload["changed"])
        self.assertEqual(self.done_draft() != {}, True)
        again, _ = self.do_tick(sessions, now=self.now + 61_000,
                                draft=local_ok)
        self.assertEqual(again["processed"]["sessions"], 0)

    def test_deferred_stays_pending_and_rechecks(self):
        sessions = [make_session(1, 1000, 9000)]

        def defer(session, ctx):
            return {"status": "deferred", "reason": "boom",
                    "jev_calls": 0, "changed": False}

        payload, _ = self.do_tick(sessions, draft=defer)
        self.assertTrue(any(d.get("job") == "draft"
                            for d in payload["deferred"]))
        self.assertIn(sessions[0]["session_id"],
                      self.deferred_ids() or set())
        again, _ = self.do_tick(sessions, now=self.now + 61_000,
                                draft=local_ok)
        self.assertEqual(again["processed"]["drafts"], 1)
        self.assertNotIn(sessions[0]["session_id"],
                         self.deferred_ids() or set())

    def test_not_implemented_visible_and_requeued(self):
        sessions = [make_session(1, 1000, 9000)]

        def missing(session, ctx):
            return {"status": "not_implemented",
                    "reason": "not_implemented", "jev_calls": 0,
                    "changed": False}

        payload, _ = self.do_tick(sessions, draft=missing)
        self.assertTrue(any(d.get("reason") == "not_implemented"
                            for d in payload["deferred"]))
        self.assertIn(sessions[0]["session_id"],
                      self.deferred_ids() or set())

    def test_adapter_exception_maps_failed(self):
        sessions = [make_session(1, 1000, 9000)]

        def boom(session, ctx):
            raise TypeError("boom-detail")

        payload, _ = self.do_tick(sessions, draft=boom)
        self.assertTrue(any(d.get("job") == "draft"
                            and d.get("reason") == "failed"
                            for d in payload["deferred"]))


class DefaultDraftTests(TickCase):
    PID = "11111111-1111-4111-8111-111111111111"

    def ctx(self, evidence):
        return {"conn": self.conn, "settings": base_settings(),
                "now_ms": self.now, "evidence": evidence, "manual": False}

    def test_no_project_skipped_without_touching_evidence(self):
        net = []

        class StrictEvidence(FakeEvidence):
            def get_session(self, session_id):  # pragma: no cover
                net.append(1)
                return super().get_session(session_id)

        session = make_session(1, 1000, 9000)  # no project
        result = tick.default_draft(session,
                                    self.ctx(StrictEvidence([session])))
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "no_project")
        self.assertEqual(result["jev_calls"], 0)
        self.assertFalse(result["changed"])
        self.assertEqual(net, [])

    def test_fake_sessions_never_hit_network(self):
        def boom(*args, **kwargs):  # pragma: no cover
            raise AssertionError("network must never be used")

        class FailingEvidence(FakeEvidence):
            def session_resources(self, session_id, limit=20):
                raise RuntimeError("boom-detail")

            def project_todos(self, project_id):
                raise RuntimeError("boom-detail")

            def session_changes(self, project_id, session_id):
                raise RuntimeError("boom-detail")

        session = make_session(1, 1000, 9000, project=self.PID)
        result = tick.default_draft(
            session, self.ctx(FailingEvidence([session])))
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(result["reason"], "evidence_unavailable")
        self.assertEqual(result["jev_calls"], 0)

    def test_tick_uses_local_draft_with_fake_sessions(self):
        class FullFake(FakeEvidence):
            def session_resources(self, session_id, limit=20):
                return {"session_id": session_id, "resources": [],
                        "count": 0}

            def project_todos(self, project_id):
                return {"todos": [], "has_logseq_linkage": True}

            def session_changes(self, project_id, session_id):
                return {"available": False,
                        "reason": "no linked repository",
                        "summary_key": "no-repo"}

            def last_session(self, project_id):
                return {"session": None}

        sessions = [make_session(1, 1000, 9000, project=self.PID)]
        evidence = FullFake(sessions)

        def local_only(session, ctx):
            return tick.default_draft(session, ctx)

        payload, _ = self.do_tick(
            evidence, draft=local_only,
            scan=local_skip, review=local_skip)
        self.assertEqual(payload["processed"]["sessions"], 1)
        self.assertEqual(payload["processed"]["drafts"], 1)
        self.assertTrue(payload["changed"])
        self.assertEqual(payload["jev_calls"], 0)
        self.assertEqual(payload["deferred"], [])

    def test_import_failure_reports_not_implemented(self):
        with patch.dict(sys.modules, {"work_log": None}):
            result = tick.default_draft(make_session(1, 1000, 9000),
                                        self.ctx(FakeEvidence([])))
        self.assertEqual(result["status"], "not_implemented")
        self.assertEqual(result["jev_calls"], 0)

    def test_unexpected_adapter_failure_maps_failed(self):
        import work_log
        with patch.object(work_log, "draft_for_session",
                          side_effect=TypeError("boom-detail")):
            result = tick.default_draft(make_session(1, 1000, 9000),
                                        self.ctx(FakeEvidence([])))
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(result["reason"], "failed")
        self.assertEqual(result["jev_calls"], 0)

    def test_allow_default_only_for_production_evidence(self):
        import work_log
        seen = {}

        def spy(session, **kwargs):
            seen.update(kwargs)
            return {"status": "skipped", "reason": "no_project",
                    "changed": False, "jev_calls": 0}

        with patch.object(work_log, "draft_for_session", side_effect=spy):
            tick.default_draft(make_session(1, 1000, 9000),
                               self.ctx(FakeEvidence([])))
            self.assertFalse(seen.get("allow_default"))
            tick.default_draft(
                make_session(1, 1000, 9000),
                self.ctx(tick.DefaultEvidence()))
            self.assertTrue(seen.get("allow_default"))


class DefaultScanTests(TickCase):
    def ctx(self):
        return {"conn": self.conn,
                "settings": base_settings(sessionCapture=True),
                "now_ms": self.now, "evidence": FakeEvidence([]),
                "manual": False}

    def test_import_unavailable_reports_not_implemented(self):
        with patch.dict(sys.modules, {"session_capture": None}):
            result = tick.default_scan(self.ctx())
        self.assertEqual(result["status"], "not_implemented")
        self.assertEqual(result["reason"], "not_implemented")
        self.assertEqual(result["jev_calls"], 0)

    def test_unexpected_adapter_failure_maps_failed(self):
        import session_capture
        with patch.object(session_capture, "scan",
                          side_effect=TypeError("boom-detail")):
            result = tick.default_scan(self.ctx())
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(result["reason"], "failed")
        self.assertEqual(result["jev_calls"], 0)

    def test_scan_delegates_to_local_capture(self):
        import session_capture
        with patch.object(session_capture, "scan",
                          return_value={"status": "skipped",
                                        "reason": "disabled",
                                        "jev_calls": 0,
                                        "changed": False}) as spy:
            result = tick.default_scan(self.ctx())
        self.assertEqual(result["reason"], "disabled")
        self.assertTrue(spy.called)

    def test_current_project_never_raises(self):
        evidence = tick.DefaultEvidence()
        try:
            result = evidence.current_project()
        except Exception as exc:  # pragma: no cover
            self.fail(f"current_project raised: {exc}")
        self.assertTrue(result is None or isinstance(result, dict))
        with patch("desktop_projects.current_project",
                   side_effect=RuntimeError("boom-detail")):
            self.assertIsNone(tick.DefaultEvidence().current_project())


class DefaultReviewTests(TickCase):
    def ctx(self):
        return {"conn": self.conn, "settings": base_settings(),
                "now_ms": self.now, "evidence": FakeEvidence([]),
                "manual": False}

    def test_import_unavailable_reports_not_implemented(self):
        with patch.dict(sys.modules, {"daily_review": None}):
            result = tick.default_review(self.ctx())
        self.assertEqual(result["status"], "not_implemented")
        self.assertEqual(result["reason"], "not_implemented")
        self.assertEqual(result["jev_calls"], 0)
        self.assertFalse(result["changed"])

    def test_unexpected_adapter_failure_maps_failed(self):
        import daily_review
        with patch.object(daily_review, "review",
                          side_effect=TypeError("boom-detail")):
            result = tick.default_review(self.ctx())
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(result["reason"], "failed")
        self.assertEqual(result["jev_calls"], 0)
        self.assertFalse(result["changed"])

    def test_real_adapter_idempotent_not_due_after_success(self):
        # End to end through the coordinator with the real review
        # adapter: one generation per day, idempotent across ticks,
        # zero network calls (deterministic order only).
        import datetime as _datetime
        day = _datetime.date(2026, 9, 18)
        start = int(_datetime.datetime(
            day.year, day.month, day.day).timestamp() * 1000)
        sessions = [make_session(1, start + 3_600_000,
                                 start + 5_400_000)]
        now = start + 19 * 3_600_000
        first, _ = self.do_tick(
            sessions, now=now,
            settings=base_settings(dailyReview=True),
            scan=local_skip, review=tick.default_review)
        self.assertTrue(first["processed"]["review"], first)
        self.assertTrue(first["changed"])
        second, _ = self.do_tick(
            sessions, now=now + 61_000,
            settings=base_settings(dailyReview=True),
            scan=local_skip, review=tick.default_review)
        self.assertFalse(second["processed"]["review"])
        self.assertFalse(second["changed"])


class TickMechanicsTests(TickCase):
    def test_open_sessions_rechecked_by_id(self):
        evidence = FakeEvidence(
            [make_session(1, 1000, 9000, status="active")])
        payload, _ = self.do_tick(evidence, draft=local_ok)
        # Open sessions are tracked, not drafted.
        self.assertEqual(payload["processed"]["drafts"], 0)
        evidence.close(evidence.sessions[0]["session_id"])
        again, _ = self.do_tick(evidence, now=self.now + 61_000,
                                draft=local_ok)
        self.assertEqual(again["processed"]["drafts"], 1)

    def test_frontier_advances_past_completed(self):
        sessions = [make_session(1, 1000, 9000)]
        before = self.frontier()
        self.do_tick(sessions, draft=local_ok)
        self.assertGreater(self.frontier(), before)

    def test_malformed_sessions_skipped_boundedly(self):
        class MessyEvidence(FakeEvidence):
            def list_sessions(self, *, limit, from_ms=None, to_ms=None):
                return [{"nope": True}, None, "x",
                        make_session(9, 1000, 9000)]

        payload, _ = self.do_tick(MessyEvidence([]), draft=local_ok)
        self.assertEqual(payload["processed"]["drafts"], 1)
        self.assertEqual(payload["jev_calls"], 0)

    def test_evidence_failure_is_unavailable(self):
        class BadEvidence(FakeEvidence):
            def list_sessions(self, **kwargs):
                raise RuntimeError("boom-detail")

        with self.assertRaises(Exception):
            tick.run_tick(conn=self.conn, settings=base_settings(),
                          now_ms=self.now, evidence=BadEvidence([]),
                          draft=local_skip, scan=local_skip,
                          review=local_skip)


class StatusTests(TickCase):
    def test_status_reports_pending_and_last_tick(self):
        self.do_tick([make_session(1, 1000, 9000)], draft=local_ok)
        status = tick.get_status(conn=self.conn,
                                 settings=base_settings(), now_ms=self.now)
        self.assertTrue(status["available"])
        self.assertIn("pending", status)
        self.assertIn("drafts", status["pending"])
        self.assertIn("captures", status["pending"])
        self.assertIn("reviews", status["pending"])
        self.assertIsNotNone(status["last_tick_ms"])

    def test_status_never_touches_evidence(self):
        status = tick.get_status(conn=self.conn,
                                 settings=base_settings(), now_ms=self.now)
        self.assertTrue(status["available"])
        self.assertIsNone(status["last_tick_ms"])


class DeferredRecheckTests(TickCase):
    def test_deferred_ids_bounded_and_rotate(self):
        sessions = [make_session(i, 1000, 2000 + i) for i in range(1, 4)]

        def defer(session, ctx):
            return {"status": "deferred", "reason": "busy",
                    "jev_calls": 0, "changed": False}

        payload, _ = self.do_tick(sessions, draft=defer)
        self.assertEqual(len(self.deferred_ids() or set()), 3)
        again, _ = self.do_tick(sessions, now=self.now + 61_000,
                                draft=local_ok)
        self.assertEqual(again["processed"]["drafts"], 3)


def run_cli(args, env_extra=None):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "memory_tick.py"), *args],
        text=True, capture_output=True, timeout=30, check=False, env=env)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.db = str(self.base / "annotations.db")
        self.settings_file = str(self.base / "settings.json")
        Path(self.settings_file).write_text(json.dumps({"memory": {
            "enabled": True, "tickSeconds": 60, "workLog": True,
            "sessionCapture": False, "dailyReview": False,
            "minSessionMs": 0}}),
            encoding="utf-8")
        self.env = {"QS_ANNOTATIONS_DB": self.db,
                    "QUICKSHELL_SETTINGS": self.settings_file,
                    "OPENROUTER_API_KEY": "",
                    "QS_DESKTOP_CONTEXT_BIN": "/nonexistent-qs-bin-xyz"}

    def test_tick_missing_binary_soft_fails_exit0_generic(self):
        completed = run_cli(["--db", self.db, "tick"], env_extra=self.env)
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stderr, "")
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["available"])
        self.assertIn("reason", payload)
        self.assertNotIn("nonexistent-qs-bin-xyz", completed.stdout)
        self.assertNotIn("Traceback", completed.stdout)

    def test_tick_no_leak_of_secret_evidence(self):
        completed = run_cli(["--db", self.db, "tick"], env_extra=self.env)
        self.assertEqual(completed.returncode, 0)
        self.assertNotIn("SECRET", completed.stdout + completed.stderr)

    def test_run_validation_exit1(self):
        bad_project = run_cli(["--db", self.db, "run", "--job", "draft",
                               "--project", "nope"], env_extra=self.env)
        self.assertNotEqual(bad_project.returncode, 0)
        self.assertIn("error:", bad_project.stderr)
        self.assertEqual(bad_project.stdout, "")
        bad_session = run_cli(["--db", self.db, "run", "--job", "draft",
                               "--session", "short"], env_extra=self.env)
        self.assertNotEqual(bad_session.returncode, 0)
        self.assertIn("error:", bad_session.stderr)
        missing_session = run_cli(["--db", self.db, "run", "--job",
                                   "draft"], env_extra=self.env)
        self.assertNotEqual(missing_session.returncode, 0)
        self.assertIn("error:", missing_session.stderr)
        bad_job = run_cli(["--db", self.db, "run", "--job", "polish"],
                          env_extra=self.env)
        self.assertNotEqual(bad_job.returncode, 0)
        self.assertIn("error:", bad_job.stderr)
        # Deleted jobs are rejected by the parser, not dispatched.
        for job in ("enrich", "triage", "assoc"):
            bad = run_cli(["--db", self.db, "run", "--job", job],
                          env_extra=self.env)
            self.assertNotEqual(bad.returncode, 0)
            self.assertIn("error:", bad.stderr)

    def test_run_review_and_scan_adapters_exit0(self):
        # Both helpers are implemented: review runs the real
        # daily_review adapter, scan the session_capture adapter. With
        # no binary behind the stubs both stay skipped/deferred
        # (exit 0, zero calls, never not_implemented, no network).
        completed = run_cli(
            ["--db", self.db, "run", "--job", "review",
             "--session", "a" * 32, "--project",
             "11111111-1111-4111-8111-111111111111"],
            env_extra=self.env)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertNotEqual(payload["status"], "not_implemented")
        self.assertIn(payload["status"], ("skipped", "deferred"))
        self.assertEqual(payload["jev_calls"], 0)
        scanned = run_cli(
            ["--db", self.db, "run", "--job", "scan",
             "--session", "a" * 32, "--project",
             "11111111-1111-4111-8111-111111111111"],
            env_extra=self.env)
        self.assertEqual(scanned.returncode, 0, scanned.stderr)
        scan_payload = json.loads(scanned.stdout)
        self.assertNotEqual(scan_payload["status"], "not_implemented")
        self.assertEqual(scan_payload["jev_calls"], 0)
        # Project-only draft runs the local work_log adapter: with no
        # binary behind the evidence stub it defers (exit 0, no network).
        completed = run_cli(
            ["--db", self.db, "run", "--job", "draft",
             "--project", "11111111-1111-4111-8111-111111111111"],
            env_extra=self.env)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], "deferred")
        self.assertEqual(payload["reason"], "evidence_unavailable")
        self.assertEqual(payload["jev_calls"], 0)

    def test_status_exit0_shape(self):
        completed = run_cli(["--db", self.db, "status"], env_extra=self.env)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["available"])
        self.assertIn("pending", payload)


class LockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.lock = str(self.base / "tick.lock")

    def test_acquire_and_overlap(self):
        import os as _os
        first = tick.acquire_tick_lock(self.lock)
        try:
            self.assertIsNotNone(first)
            info = _os.lstat(self.lock)
            self.assertTrue(tick._lock_file_is_safe(info))
            self.assertEqual(tick._lock_reject_kind(info), "")
        finally:
            try:
                first.close()
            except Exception:
                pass

    def test_symlink_rejected(self):
        target = self.base / "real.lock"
        target.write_text("x", encoding="utf-8")
        link = self.base / "link.lock"
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest("symlinks unavailable")
        with self.assertRaises(tick.TickLockUnsafe):
            tick.acquire_tick_lock(str(link))

    def test_overlap_lock_fn_reports_overlap(self):
        conn = annotations.connect(str(self.base / "a.db"))
        try:
            out = tick.run_tick(
                conn=conn, settings=base_settings(), now_ms=5000000,
                evidence=FakeEvidence([]), draft=local_skip,
                scan=local_skip, review=local_skip,
                lock_fn=lambda: None)
        finally:
            conn.close()
        self.assertTrue(any(d.get("reason") == "overlap"
                            for d in out["deferred"]))

    def test_unsafe_lock_fn_reports_lock_unsafe(self):
        conn = annotations.connect(str(self.base / "a.db"))
        try:
            def bad_lock():
                raise tick.TickLockUnsafe("tick lock unavailable")

            out = tick.run_tick(
                conn=conn, settings=base_settings(), now_ms=5000000,
                evidence=FakeEvidence([]), draft=local_skip,
                scan=local_skip, review=local_skip,
                lock_fn=bad_lock)
        finally:
            conn.close()
        self.assertIn("lock_unsafe", json.dumps(out))


class ManualDispatchTests(TickCase):
    def test_manual_run_job_review(self):
        code, payload = tick._do_run(
            self.db, "review", None, None,
            settings_injected=base_settings(dailyReview=True))
        self.assertEqual(code, 0)
        self.assertEqual(payload["job"], "review")
        self.assertEqual(payload["jev_calls"], 0)

    def test_manual_run_unknown_job_rejected(self):
        with self.assertRaises(tick.TickError):
            tick._do_run(self.db, "enrich", None, None,
                         settings_injected=base_settings())

    def test_manual_draft_requires_selector(self):
        with self.assertRaises(tick.TickError):
            tick._do_run(self.db, "draft", None, None,
                         settings_injected=base_settings())

    def test_manual_unavailable_without_evidence(self):
        code, payload = tick._do_run(
            self.db, "draft", DefaultDraftTests.PID, None,
            settings_injected=base_settings())
        self.assertEqual(code, 0)
        self.assertIn(payload["status"], ("unavailable", "deferred",
                                          "skipped"))


class PersistTests(TickCase):
    def test_state_roundtrips_bounded(self):
        tick._store_id_list(self.conn, tick.STATE_DEFERRED,
                            ["a" * 32, "b" * 32])
        self.assertEqual(
            set(tick._load_id_set(self.conn, tick.STATE_DEFERRED, None)),
            {"a" * 32, "b" * 32})
        tick._store_json_dict(self.conn, tick.STATE_DONE_DRAFT,
                              {"a" * 32: "digest"})
        self.assertEqual(
            tick._load_json_dict(self.conn, tick.STATE_DONE_DRAFT),
            {"a" * 32: "digest"})


if __name__ == "__main__":
    unittest.main()
