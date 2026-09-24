"""Tests for deterministic work-log drafts (scripts/work_log.py).

Offline, injected Evidence, no real binaries/network. Part 1 covers
resolution, draft sections/bounds/markdown/properties, cache behavior,
digest stability and the draft CLI.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import annotations
import work_log
from work_log import EvidenceUnavailable, WorkLogError

PID = "11111111-1111-4111-8111-111111111111"
PID2 = "22222222-2222-4222-8222-222222222222"
SID = "a" * 32
SID2 = "b" * 32
NOW = 5_000_000

SCRIPT = ROOT / "scripts" / "work_log.py"


def make_session(index=1, project=PID, status="closed",
                 apps=("app-a", "app-b"), events=10, start=1000, end=9000,
                 sid=SID):
    return {
        "session_id": sid if isinstance(sid, str) else "%032x" % index,
        "device_id": "d" * 32,
        "project": {"id": project} if project else None,
        "start_ms": start,
        "end_ms": end,
        "event_count": events,
        "status": status,
        "ended_reason": "inactivity" if status == "closed" else None,
        "active": status != "closed",
        "effective_status": "closed" if status == "closed" else "active",
        "applications": list(apps),
        "resources": [],
    }


class FakeEvidence:
    """Injectable evidence provider (no binaries, no network)."""

    def __init__(self, sessions=(), resources=(), todos=None, changes=None,
                 fail_with=None):
        self.sessions = {s["session_id"]: dict(s) for s in sessions}
        self.all_sessions = [dict(s) for s in sessions]
        self.by_project = {}
        for session in sessions:
            project = (session.get("project") or {}).get("id")
            if project:
                self.by_project.setdefault(project, []).append(session)
        self.resources = list(resources)
        self.todos = todos if todos is not None else {"todos": []}
        self.changes = changes
        self.fail_with = fail_with
        self.calls = []

    def _maybe_fail(self, name):
        self.calls.append(name)
        if self.fail_with is not None:
            raise self.fail_with("boom-detail")

    def last_session(self, project_id):
        self._maybe_fail("last_session")
        rows = self.by_project.get(str(project_id).lower(), [])
        rows = self.by_project.get(project_id, rows)
        if not rows:
            return {"session": None}
        return {"session": dict(rows[-1])}

    def get_session(self, session_id):
        self._maybe_fail("get_session")
        row = self.sessions.get(session_id.lower())
        return dict(row) if row is not None else None

    def session_resources(self, session_id, limit=20):
        self._maybe_fail("session_resources")
        return {"session_id": session_id, "resources": list(self.resources),
                "count": len(self.resources)}

    def project_todos(self, project_id):
        self._maybe_fail("project_todos")
        if isinstance(self.todos, dict):
            return dict(self.todos)
        return {"todos": []}

    def session_changes(self, project_id, session_id):
        self._maybe_fail("session_changes")
        if self.changes is not None:
            return dict(self.changes)
        return {"available": False, "reason": "no linked repository",
                "summary_key": "no-repo"}

    def work_sessions(self, project_id, limit=20):
        self._maybe_fail("work_sessions")
        rows = [dict(s) for s in self.all_sessions
                if ((s.get("project") or {}).get("id") == project_id)]
        return {"sessions": rows[:limit], "count": len(rows)}


def default_changes(**over):
    changes = {
        "available": True, "reason": "",
        "project_id": PID, "session_id": SID,
        "baseline_commit": "b" * 40, "latest_commit": "c" * 40,
        "has_baseline": True, "summary_key": "bbbbbbbbbbbb..cccccccccccc",
        "evidence": "M foo.py",
        "repo_path": "/home/user/code/demo", "observed_at_ms": 1,
        "updated_at_ms": 2,
        "limitations": "last observed state",
    }
    changes.update(over)
    return changes


class DraftCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.db = str(self.base / "annotations.db")
        self.conn = annotations.connect(self.db)

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        self.temp.cleanup()

    def settings(self, **over):
        settings = {}
        settings.update(over)
        return settings


class ResolutionTests(DraftCase):
    def test_stored_attribution_wins(self):
        session = make_session(project=PID)
        resolved = work_log.resolve_session_project(
            session, conn=self.conn, settings=self.settings(),
            evidence=None, now_ms=NOW)
        self.assertEqual(resolved, {"project_id": PID, "source": "attribution",
                                    "suggested": False})

    def test_explicit_field_wins_over_attribution(self):
        session = make_session(project=PID, sid=SID)
        session["project_id"] = PID2
        resolved = work_log.resolve_session_project(
            session, conn=self.conn, settings=self.settings(),
            evidence=None, now_ms=NOW)
        self.assertEqual(resolved, {"project_id": PID2, "source": "explicit",
                                    "suggested": False})

    def test_no_inference_no_suggestion(self):
        # Unattributed sessions resolve to None even when labels or
        # state carry suggestions: attribution or explicit only.
        session = make_session(project=None, sid=SID)
        self.assertIsNone(work_log.resolve_session_project(
            session, conn=self.conn, settings=self.settings(),
            evidence=None, now_ms=NOW))
        self.assertIsNone(work_log.resolve_session_project(
            session, conn=self.conn,
            settings=self.settings(associationSuggestions=True),
            evidence=None, now_ms=NOW))
        annotations.set_state(self.conn, f"dismissed_suggestion:{SID}", "1")
        self.assertIsNone(work_log.resolve_session_project(
            session, conn=self.conn, settings=self.settings(),
            evidence=None, now_ms=NOW))

class GenerateDraftTests(DraftCase):
    def test_draft_shape_sections_and_properties(self):
        session = make_session()
        evidence = FakeEvidence(
            sessions=[session],
            resources=[{"resource": {"adapter": "file",
                                     "file": "/home/user/code/demo/main.py"},
                        "occurrence_count": 5}],
            todos={"todos": [{"line": 12, "task": "Fix the thing",
                              "marker": "TODO", "done": False}],
                   "revision": "r" * 64, "has_logseq_linkage": True,
                   "path": "pages/Demo.md"},
            changes=default_changes())
        draft = work_log.generate_draft(
            PID, SID, conn=self.conn, now_ms=NOW, evidence=evidence,
            settings=self.settings())
        self.assertTrue(draft["draft_id"].startswith("dwl_"))
        self.assertEqual(draft["project"], PID)
        self.assertEqual(draft["session"], SID)
        self.assertEqual(draft["session_start_ms"], 1000)
        self.assertEqual(draft["session_end_ms"], 9000)
        self.assertEqual([s["id"] for s in draft["sections"]],
                         ["what_happened", "open_todos",
                          "decisions", "next"])
        titles = [s["title"] for s in draft["sections"]]
        self.assertEqual(titles, ["What happened",
                                  "Open TODOs", "Decisions", "Next"])
        self.assertEqual(draft["properties"],
                         {"quickshell-worklog": f"{PID}:{SID}"})
        self.assertIn("#12 Fix the thing", draft["markdown"])
        self.assertRegex(draft["evidence_digest"], r"^[0-9a-f]{64}$")
        self.assertFalse(draft["cached"])
        self.assertNotIn("polished", draft)
        self.assertNotIn("polished_markdown", draft)
        self.assertNotIn("polish_enabled", draft)
        self.assertEqual(draft["saved"],
                         {"journal_ms": None, "page_ms": None})
        self.assertLessEqual(len(draft["markdown"].encode("utf-8")),
                             work_log.MAX_MARKDOWN_CHARS + 64)

    def test_unavailable_evidence_still_drafts_with_reasons(self):
        session = make_session()
        evidence = FakeEvidence(sessions=[session])  # default: unavailable
        draft = work_log.generate_draft(
            PID, SID, conn=self.conn, now_ms=NOW, evidence=evidence,
            settings=self.settings())
        by_id = {s["id"]: s["items"] for s in draft["sections"]}
        self.assertEqual(sorted(by_id.keys()),
                         ["decisions", "next", "open_todos",
                          "what_happened"])
        # Default todos are available but empty: explicit empty text.
        self.assertTrue(any("No open TODOs" in item
                            for item in by_id["open_todos"]))
        self.assertTrue(any("No decisions" in item
                            for item in by_id["decisions"]))
        self.assertTrue(any("next step" in item.lower()
                            for item in by_id["next"]))
        # Unlinked projects carry the explicit reason item instead.
        unlinked = FakeEvidence(
            sessions=[session],
            todos={"todos": [], "has_logseq_linkage": False,
                   "reason": "project has no Logseq linkage"})
        draft2 = work_log.generate_draft(
            PID, SID, conn=self.conn, now_ms=NOW, evidence=unlinked,
            settings=self.settings(), refresh=True)
        by_id2 = {s["id"]: s["items"] for s in draft2["sections"]}
        self.assertTrue(any("unavailable" in item.lower()
                            for item in by_id2["open_todos"]))

    def test_bounds_items_and_chars(self):
        session = make_session(apps=[f"app-{i}" for i in range(40)],
                               events=500)
        resources = [{"resource": {"adapter": "file",
                                    "file": f"/x/file-{i}.py"},
                      "occurrence_count": i} for i in range(30)]
        todos = {"todos": [{"line": i + 1, "task": "T" * 500,
                            "marker": "TODO", "done": False}
                           for i in range(30)],
                 "has_logseq_linkage": True}
        evidence = FakeEvidence(sessions=[session], resources=resources,
                                todos=todos, changes=default_changes())
        draft = work_log.generate_draft(
            PID, SID, conn=self.conn, now_ms=NOW, evidence=evidence,
            settings=self.settings())
        for section in draft["sections"]:
            self.assertLessEqual(len(section["items"]),
                                 work_log.MAX_SECTION_ITEMS)
            for item in section["items"]:
                self.assertLessEqual(len(item), work_log.MAX_ITEM_CHARS + 8)

    def test_last_session_path_requires_closed(self):
        evidence = FakeEvidence(sessions=[])
        with self.assertRaises(WorkLogError) as ctx:
            work_log.generate_draft(PID, None, conn=self.conn, now_ms=NOW,
                                    evidence=evidence,
                                    settings=self.settings())
        self.assertIn("no closed session", str(ctx.exception))
        open_session = make_session(status="open")
        evidence = FakeEvidence(sessions=[open_session])
        with self.assertRaises(WorkLogError):
            work_log.generate_draft(PID, None, conn=self.conn, now_ms=NOW,
                                    evidence=evidence,
                                    settings=self.settings())

    def test_last_session_closed_drafts(self):
        session = make_session()
        evidence = FakeEvidence(sessions=[session])
        draft = work_log.generate_draft(PID, None, conn=self.conn,
                                        now_ms=NOW, evidence=evidence,
                                        settings=self.settings())
        self.assertEqual(draft["session"], SID)

    def test_sessionless_falls_back_to_newest_closed(self):
        old = make_session(index=1, start=1000, end=2000,
                           sid="c" * 32)
        new = make_session(index=2, status="open", start=3000, end=9000,
                           sid="d" * 32)
        evidence = FakeEvidence(sessions=[old, new],
                                changes=default_changes())
        draft = work_log.generate_draft(PID, None, conn=self.conn,
                                        now_ms=NOW, evidence=evidence,
                                        settings=self.settings())
        self.assertEqual(draft["session"], "c" * 32)
        self.assertIn("work_sessions", evidence.calls)

    def test_sessionless_none_closed_errors(self):
        only_open = make_session(status="open")
        evidence = FakeEvidence(sessions=[only_open])
        with self.assertRaises(WorkLogError) as ctx:
            work_log.generate_draft(PID, None, conn=self.conn, now_ms=NOW,
                                    evidence=evidence,
                                    settings=self.settings())
        self.assertIn("no closed session", str(ctx.exception))

    def test_evidence_failure_raises_unavailable(self):
        session = make_session()
        evidence = FakeEvidence(sessions=[session],
                                fail_with=RuntimeError)
        with self.assertRaises(EvidenceUnavailable):
            work_log.generate_draft(PID, SID, conn=self.conn, now_ms=NOW,
                                    evidence=evidence,
                                    settings=self.settings())

    def test_project_validation(self):
        with self.assertRaises(WorkLogError):
            work_log.generate_draft(None, None, conn=self.conn, now_ms=NOW,
                                    evidence=FakeEvidence(),
                                    settings=self.settings())
        with self.assertRaises(WorkLogError):
            work_log.generate_draft("nope", None, conn=self.conn,
                                    now_ms=NOW, evidence=FakeEvidence(),
                                    settings=self.settings())
        with self.assertRaises(WorkLogError):
            work_log.generate_draft(PID, "short", conn=self.conn,
                                    now_ms=NOW, evidence=FakeEvidence(),
                                    settings=self.settings())

    def test_digest_stable_and_session_scoped(self):
        session = make_session()
        evidence = FakeEvidence(sessions=[session],
                                changes=default_changes())
        first = work_log.generate_draft(
            PID, SID, conn=self.conn, now_ms=NOW, evidence=evidence,
            settings=self.settings())
        second = work_log.generate_draft(
            PID, SID, conn=self.conn, now_ms=NOW + 999,
            evidence=evidence, settings=self.settings(),
            refresh=True)
        self.assertEqual(first["evidence_digest"], second["evidence_digest"])
        self.assertEqual(first["draft_id"], second["draft_id"])
        # A changed event count changes the digest and the draft id.
        other = make_session(events=11)
        evidence2 = FakeEvidence(sessions=[other],
                                 changes=default_changes())
        third = work_log.generate_draft(
            PID, SID, conn=self.conn, now_ms=NOW, evidence=evidence2,
            settings=self.settings(), refresh=True)
        self.assertNotEqual(first["evidence_digest"], third["evidence_digest"])
        self.assertNotEqual(first["draft_id"], third["draft_id"])

    def test_digest_stable_across_resource_order(self):
        session = make_session()
        res_a = {"resource": {"adapter": "file", "file": "/x/a.py"},
                 "occurrence_count": 5}
        res_b = {"resource": {"adapter": "file", "file": "/x/b.py"},
                 "occurrence_count": 3}
        first = work_log.generate_draft(
            PID, SID, conn=self.conn, now_ms=NOW,
            evidence=FakeEvidence(sessions=[session],
                                  resources=[res_a, res_b],
                                  changes=default_changes()),
            settings=self.settings())
        self.assertFalse(first["cached"])
        second = work_log.generate_draft(
            PID, SID, conn=self.conn, now_ms=NOW + 1,
            evidence=FakeEvidence(sessions=[session],
                                  resources=[res_b, res_a],
                                  changes=default_changes()),
            settings=self.settings())
        self.assertEqual(first["evidence_digest"], second["evidence_digest"])
        self.assertEqual(first["draft_id"], second["draft_id"])
        self.assertTrue(second["cached"])

    def test_cache_hit_and_refresh(self):
        session = make_session()
        evidence = FakeEvidence(sessions=[session],
                                changes=default_changes())
        first = work_log.generate_draft(
            PID, SID, conn=self.conn, now_ms=NOW, evidence=evidence,
            settings=self.settings())
        self.assertFalse(first["cached"])
        # Same evidence later: digest matches, cache hit (no refresh).
        second = work_log.generate_draft(
            PID, SID, conn=self.conn, now_ms=NOW + 1000, evidence=evidence,
            settings=self.settings())
        self.assertTrue(second["cached"])
        self.assertEqual(second["draft_id"], first["draft_id"])
        refreshed = work_log.generate_draft(
            PID, SID, conn=self.conn, now_ms=NOW + 2000, evidence=evidence,
            settings=self.settings(), refresh=True)
        self.assertFalse(refreshed["cached"])

    def test_cache_hit_merges_saved_and_polished(self):
        session = make_session()
        evidence = FakeEvidence(sessions=[session],
                                changes=default_changes())
        first = work_log.generate_draft(
            PID, SID, conn=self.conn, now_ms=NOW, evidence=evidence,
            settings=self.settings())
        annotations.mark_draft_saved(self.conn, first["draft_id"], "journal",
                                     now_ms=1111)
        annotations.mark_draft_polished(self.conn, first["draft_id"],
                                        "# shiny", now_ms=2222)
        second = work_log.generate_draft(
            PID, SID, conn=self.conn, now_ms=NOW + 1000, evidence=evidence,
            settings=self.settings())
        self.assertTrue(second["cached"])
        self.assertEqual(second["saved"],
                         {"journal_ms": 1111, "page_ms": None})
        self.assertTrue(second["polished"])
        self.assertEqual(second["polished_markdown"], "# shiny")


class DraftForSessionTests(DraftCase):
    def test_ok_changed_and_cached_shapes(self):
        session = make_session()
        evidence = FakeEvidence(sessions=[session],
                                changes=default_changes())
        first = work_log.draft_for_session(
            session, conn=self.conn, settings=self.settings(), now_ms=NOW,
            evidence=evidence)
        self.assertEqual(first["status"], "ok")
        self.assertEqual(first["reason"], "")
        self.assertTrue(first["changed"])
        self.assertFalse(first["cached"])
        self.assertEqual(first["jev_calls"], 0)
        self.assertEqual(first["project"], PID)
        self.assertIn("draft_id", first)
        second = work_log.draft_for_session(
            session, conn=self.conn, settings=self.settings(),
            now_ms=NOW + 1000, evidence=evidence)
        self.assertEqual(second["status"], "ok")
        self.assertFalse(second["changed"])
        self.assertTrue(second["cached"])

    def test_no_project_skipped(self):
        session = make_session(project=None, sid=SID2)
        result = work_log.draft_for_session(
            session, conn=self.conn, settings=self.settings(), now_ms=NOW,
            evidence=FakeEvidence())
        self.assertEqual(result, {"status": "skipped", "reason": "no_project",
                                  "changed": False, "jev_calls": 0})

    def test_no_closed_session_deferred(self):
        session = make_session(project=PID)
        result = work_log.draft_for_session(
            session, conn=self.conn, settings=self.settings(), now_ms=NOW,
            evidence=FakeEvidence(sessions=[]))
        # Explicit session id with unknown evidence reads as unavailable.
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(result["reason"], "evidence_unavailable")

    def test_unexpected_internal_error_failed_no_traceback(self):
        class Exploding:
            def last_session(self, project_id):
                raise RuntimeError("boom")

            def get_session(self, session_id):
                raise RuntimeError("boom")

            def session_resources(self, session_id, limit=20):
                raise RuntimeError("boom")

            def project_todos(self, project_id):
                raise RuntimeError("boom")

            def session_changes(self, project_id, session_id):
                raise RuntimeError("boom")

        session = make_session()
        result = work_log.draft_for_session(
            session, conn=self.conn, settings=self.settings(), now_ms=NOW,
            evidence=Exploding())
        self.assertEqual(result["status"], "deferred")
        self.assertIn(result["reason"],
                      ("evidence_unavailable", "failed"))
        # Never raises for domain misses, even with a broken conn.
        broken = work_log.draft_for_session(
            make_session(project=None, sid=SID2), conn=None,
            settings=self.settings(), now_ms=NOW, evidence=FakeEvidence())
        self.assertEqual(broken["status"], "skipped")

    def test_explicit_project_id_row_supported(self):
        row = {"session_id": "", "project_id": PID, "status": "unknown",
               "effective_status": "unknown", "event_count": 0,
               "applications": []}
        session = make_session()
        evidence = FakeEvidence(sessions=[session])
        result = work_log.draft_for_session(
            row, conn=self.conn, settings=self.settings(), now_ms=NOW,
            evidence=evidence)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["project"], PID)

    def test_partial_evidence_never_spawns_production(self):
        import unittest.mock as mock

        class PartialEvidence:
            def get_session(self, session_id):
                return dict(make_session())

        session = make_session()
        with mock.patch.object(
                work_log, "DefaultEvidence",
                side_effect=AssertionError("must not instantiate")):
            result = work_log.draft_for_session(
                session, conn=self.conn, settings=self.settings(),
                now_ms=NOW, evidence=PartialEvidence())
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(result["reason"], "evidence_unavailable")
        with mock.patch.object(
                work_log, "DefaultEvidence",
                side_effect=AssertionError("must not instantiate")):
            with self.assertRaises(EvidenceUnavailable):
                work_log.generate_draft(
                    PID, SID, conn=self.conn, now_ms=NOW,
                    evidence=PartialEvidence(), settings=self.settings())


def run_cli(args, env_extra=None):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        text=True, capture_output=True, timeout=30, check=False, env=env)


class DraftCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.db = str(self.base / "annotations.db")
        self.settings_file = str(self.base / "settings.json")
        Path(self.settings_file).write_text(json.dumps({"memory": {}}),
                                             encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_draft_validation_errors_bounded(self):
        env = {"QS_ANNOTATIONS_DB": self.db,
               "QUICKSHELL_SETTINGS": self.settings_file}
        bad = run_cli(["--db", self.db, "draft", "--project", "nope"],
                      env_extra=env)
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("error:", bad.stderr)
        self.assertNotIn("Traceback", bad.stderr)
        self.assertEqual(bad.stdout, "")
        missing = run_cli(["--db", self.db, "draft"], env_extra=env)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("error:", missing.stderr)
        unknown = run_cli(["--db", self.db, "draft", "--draft-id",
                           "dwl_missing"], env_extra=env)
        self.assertNotEqual(unknown.returncode, 0)
        self.assertIn("error:", unknown.stderr)

    def test_draft_unknown_project_offline_no_leak(self):
        env = {"QS_ANNOTATIONS_DB": self.db,
               "QUICKSHELL_SETTINGS": self.settings_file,
               "QS_DESKTOP_CONTEXT_BIN": "/nonexistent-qs-bin-xyz",
               "QS_DESKTOP_DB": "/nonexistent-qs-db-xyz"}
        completed = run_cli(
            ["--db", self.db, "draft", "--project", PID], env_extra=env)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("error:", completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)
        self.assertNotIn("nonexistent", completed.stdout + completed.stderr)

    def test_draft_by_id_json_shape(self):
        conn = annotations.connect(self.db)
        try:
            session = make_session()
            evidence = FakeEvidence(sessions=[session],
                                    changes=default_changes())
            draft_id = work_log.generate_draft(
                PID, SID, conn=conn, now_ms=NOW, evidence=evidence,
                settings={})["draft_id"]
        finally:
            conn.close()
        env = {"QS_ANNOTATIONS_DB": self.db,
               "QUICKSHELL_SETTINGS": self.settings_file}
        completed = run_cli(["--db", self.db, "draft", "--draft-id",
                             draft_id], env_extra=env)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["draft_id"], draft_id)
        self.assertEqual(payload["project"], PID)
        self.assertEqual(payload["session"], SID)
        self.assertTrue(payload["cached"])
        self.assertEqual([s["id"] for s in payload["sections"]],
                         ["what_happened", "open_todos",
                          "decisions", "next"])
        self.assertIsInstance(payload["markdown"], str)
        self.assertEqual(payload["properties"],
                         {"quickshell-worklog": f"{PID}:{SID}"})
        self.assertIn("evidence_digest", payload)
        self.assertIn("summary_key", payload)
        self.assertNotIn("polished_markdown", payload)
        self.assertEqual(payload["saved"],
                         {"journal_ms": None, "page_ms": None})
        self.assertNotIn("polish_enabled", payload)


class ExportCase(DraftCase):
    def setUp(self):
        super().setUp()
        self.graph = self.base / "graph"
        (self.graph / "pages").mkdir(parents=True)
        (self.graph / "pages" / "Demo.md").write_text(
            "# Demo\n\n- TODO Write tests\n", encoding="utf-8")
        registry = self.base / "registry.toml"
        registry.write_text(
            'version = 1\n'
            '[[projects]]\n'
            f'id = "{PID}"\n'
            'name = "Demo"\n'
            'logseq_path = "pages/Demo.md"\n'
            'local_folder = ""\n'
            'github_url = ""\n',
            encoding="utf-8")
        import projects
        self._projects = projects
        self._old_registry = os.environ.get(projects.ENV_VAR)
        os.environ[projects.ENV_VAR] = str(registry)

    def tearDown(self):
        if self._old_registry is None:
            os.environ.pop(self._projects.ENV_VAR, None)
        else:
            os.environ[self._projects.ENV_VAR] = self._old_registry
        super().tearDown()

    def make_draft(self, **over):
        session = make_session()
        evidence = FakeEvidence(sessions=[session],
                                changes=default_changes())
        return work_log.generate_draft(
            PID, SID, conn=self.conn, now_ms=NOW, evidence=evidence,
            settings=self.settings(), **over)


class PrepareApplyTests(ExportCase):
    def test_page_roundtrip_shapes(self):
        draft = self.make_draft()
        prepped = work_log.prepare_export(
            self.conn, draft["draft_id"], "page", graph=self.graph,
            now_ms=NOW)
        self.assertEqual(prepped["target"], "page")
        preview = prepped["preview"]
        self.assertEqual(preview["path"], "pages/Demo.md")
        self.assertEqual(preview["page"], "Demo")
        self.assertIn("quickshell-worklog::", preview["block"])
        self.assertTrue(preview["block"].startswith("- ## Session logs\n\t- Session log "))
        self.assertIn("quickshell-worklog::", preview["content"])
        self.assertIn("- ## Session logs", preview["content"])
        applied = work_log.apply_export(
            self.conn, prepped["prepared"], graph=self.graph, now_ms=NOW + 5)
        self.assertTrue(applied["applied"])
        self.assertEqual(applied["target"], "page")
        content = (self.graph / "pages" / "Demo.md").read_text(
            encoding="utf-8")
        self.assertIn(f"quickshell-worklog:: {PID}:{SID}", content)
        self.assertIn("- TODO Write tests", content)

    def test_page_nests_under_existing_session_logs_heading(self):
        (self.graph / "pages" / "Demo.md").write_text(
            "# Demo\n- ## Session logs\n\t- Session log old00000\n"
            "\t  quickshell-worklog:: old:old\n- ## Later\n- TODO Keep\n",
            encoding="utf-8")
        draft = self.make_draft()
        prepped = work_log.prepare_export(
            self.conn, draft["draft_id"], "page", graph=self.graph,
            now_ms=NOW)
        preview = prepped["preview"]
        self.assertTrue(preview["block"].startswith("\t- Session log "))
        applied = work_log.apply_export(
            self.conn, prepped["prepared"], graph=self.graph, now_ms=NOW + 5)
        self.assertTrue(applied["applied"])
        content = (self.graph / "pages" / "Demo.md").read_text(
            encoding="utf-8")
        lines = content.splitlines()
        old_idx = next(
            i for i, line in enumerate(lines)
            if "quickshell-worklog:: old:old" in line)
        new_idx = next(
            i for i, line in enumerate(lines)
            if f"quickshell-worklog:: {PID}:{SID}" in line)
        later_idx = next(
            i for i, line in enumerate(lines) if line == "- ## Later")
        self.assertGreater(new_idx, old_idx)
        self.assertLess(new_idx, later_idx)
        self.assertIn("- ## Later", content)
        self.assertIn("- TODO Keep", content)
        self.assertEqual(content.count("- ## Session logs"), 1)

    def test_page_inner_session_log_line_is_not_a_heading(self):
        (self.graph / "pages" / "Demo.md").write_text(
            "# Demo\n- Session log deadbeef\n"
            "  quickshell-worklog:: other:other\n"
            "  # Session log\n  old body\n",
            encoding="utf-8")
        draft = self.make_draft()
        prepped = work_log.prepare_export(
            self.conn, draft["draft_id"], "page", graph=self.graph,
            now_ms=NOW)
        preview = prepped["preview"]
        self.assertEqual(preview["content"].count("- ## Session logs"), 1)
        content = preview["content"]
        self.assertGreater(content.index("- ## Session logs"),
                           content.index("old body"))
        self.assertTrue(preview["block"].startswith("- ## Session logs\n\t- Session log "))

    def test_double_save_refused_by_db_flag(self):
        draft = self.make_draft()
        prepped = work_log.prepare_export(
            self.conn, draft["draft_id"], "page", graph=self.graph,
            now_ms=NOW)
        work_log.apply_export(self.conn, prepped["prepared"],
                              graph=self.graph, now_ms=NOW + 1)
        with self.assertRaises(WorkLogError) as ctx:
            work_log.prepare_export(self.conn, draft["draft_id"], "page",
                                    graph=self.graph, now_ms=NOW + 2)
        self.assertIn("already saved", str(ctx.exception))

    def test_stale_revision_refuses_and_burns_token(self):
        draft = self.make_draft()
        prepped = work_log.prepare_export(
            self.conn, draft["draft_id"], "page", graph=self.graph,
            now_ms=NOW)
        # External write moves the page revision forward.
        import project_planner as _pp
        page = _pp.read_page(self.graph, "pages/Demo.md")
        _pp.update_page(self.graph, "pages/Demo.md", page["revision"],
                        page["content"] + "- external edit\n")
        with self.assertRaises(WorkLogError):
            work_log.apply_export(self.conn, prepped["prepared"],
                                   graph=self.graph, now_ms=NOW + 1)
        # Token burned: a retry reads as unknown, not as a fresh write.
        with self.assertRaises(WorkLogError) as ctx:
            work_log.apply_export(self.conn, prepped["prepared"],
                                   graph=self.graph, now_ms=NOW + 2)
        self.assertIn("unknown or expired", str(ctx.exception))

    def test_expiry_refused(self):
        draft = self.make_draft()
        prepped = work_log.prepare_export(
            self.conn, draft["draft_id"], "page", graph=self.graph,
            now_ms=NOW)
        with self.assertRaises(WorkLogError) as ctx:
            work_log.apply_export(
                self.conn, prepped["prepared"], graph=self.graph,
                now_ms=NOW + work_log.PREPARED_TTL_MS + 1)
        self.assertIn("unknown or expired", str(ctx.exception))

    def test_prepare_validation(self):
        draft = self.make_draft()
        with self.assertRaises(WorkLogError):
            work_log.prepare_export(self.conn, draft["draft_id"], "inbox",
                                    graph=self.graph, now_ms=NOW)
        with self.assertRaises(WorkLogError):
            work_log.prepare_export(self.conn, "dwl_missing", "page",
                                    graph=self.graph, now_ms=NOW)
        with self.assertRaises(WorkLogError):
            work_log.apply_export(self.conn, "tok_missing",
                                   graph=self.graph, now_ms=NOW)
        with self.assertRaises(WorkLogError):
            work_log.apply_export(self.conn, "not a token!!",
                                   graph=self.graph, now_ms=NOW)

    def test_page_marker_scan_refuses_prepare(self):
        draft = self.make_draft()
        page = self.graph / "pages" / "Demo.md"
        page.write_text(page.read_text(encoding="utf-8")
                        + f"\nquickshell-worklog:: {PID}:{SID}\n",
                        encoding="utf-8")
        with self.assertRaises(WorkLogError) as ctx:
            work_log.prepare_export(self.conn, draft["draft_id"], "page",
                                    graph=self.graph, now_ms=NOW)
        self.assertIn("already saved", str(ctx.exception))

    def test_truncate_utf8_never_exceeds_limit(self):
        big = "x" * (work_log.MAX_MARKDOWN_CHARS + 100)
        clipped = work_log._truncate_utf8(big, work_log.MAX_MARKDOWN_CHARS)
        self.assertLessEqual(len(clipped.encode("utf-8")),
                             work_log.MAX_MARKDOWN_CHARS)
        self.assertIn("[truncated]", clipped)
        small = work_log._truncate_utf8("small", work_log.MAX_MARKDOWN_CHARS)
        self.assertEqual(small, "small")

def _marker_text():
    return f"quickshell-worklog:: {PID}:{SID}"


class ExportCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.db = str(self.base / "annotations.db")
        self.graph = self.base / "graph"
        (self.graph / "pages").mkdir(parents=True)
        (self.graph / "pages" / "Demo.md").write_text(
            "# Demo\n", encoding="utf-8")
        registry = self.base / "registry.toml"
        registry.write_text(
            'version = 1\n'
            '[[projects]]\n'
            f'id = "{PID}"\n'
            'name = "Demo"\n'
            'logseq_path = "pages/Demo.md"\n'
            'local_folder = ""\n'
            'github_url = ""\n',
            encoding="utf-8")
        settings_file = self.base / "settings.json"
        settings_file.write_text(json.dumps({"memory": {}}),
                                 encoding="utf-8")
        import projects
        self.env = {"QS_ANNOTATIONS_DB": self.db,
                    "QUICKSHELL_SETTINGS": str(settings_file),
                    "QUICKSHELL_PROJECTS_FILE": str(registry),
                    "QS_DESKTOP_CONTEXT_BIN": "/nonexistent-qs-bin-xyz",
                    "QS_DESKTOP_DB": "/nonexistent-qs-db-xyz"}

    def tearDown(self):
        self.temp.cleanup()

    def _draft_id(self):
        # Seed a draft directly (evidence is faked in-process).
        conn = annotations.connect(self.db)
        try:
            session = make_session()
            evidence = FakeEvidence(sessions=[session],
                                    changes=default_changes())
            draft = work_log.generate_draft(
                PID, SID, conn=conn, now_ms=NOW, evidence=evidence,
                settings={})
            return draft["draft_id"]
        finally:
            conn.close()

    def test_cli_prepare_apply_page(self):
        draft_id = self._draft_id()
        prep = run_cli(["--db", self.db, "--graph", str(self.graph),
                        "prepare", "--draft-id", draft_id,
                        "--target", "page"], env_extra=self.env)
        self.assertEqual(prep.returncode, 0, prep.stderr)
        payload = json.loads(prep.stdout)
        self.assertEqual(payload["preview"]["target"], "page")
        applied = run_cli(["--db", self.db, "--graph", str(self.graph),
                           "apply", "--prepared",
                           payload["prepared"]], env_extra=self.env)
        self.assertEqual(applied.returncode, 0, applied.stderr)

    def test_cli_bad_target_no_echo(self):
        secret = "SECRET-TARGET-9f8e"
        completed = run_cli(["--db", self.db, "--graph", str(self.graph),
                             "prepare", "--draft-id", "dwl_x",
                             "--target", secret], env_extra=self.env)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("error:", completed.stderr)
        self.assertNotIn(secret, completed.stdout + completed.stderr)


class PreviewOpenFieldsTests(ExportCase):
    def test_page_preview_carries_graph_and_page(self):
        draft = self.make_draft()
        prepped = work_log.prepare_export(
            self.conn, draft["draft_id"], "page", graph=self.graph,
            now_ms=NOW)
        preview = prepped["preview"]
        self.assertEqual(preview["page"], "Demo")
        self.assertEqual(preview["graphName"], self.graph.name)
        stored = annotations.get_prepared(self.conn, prepped["prepared"])
        assert stored is not None
        self.assertNotIn("graphName", stored["payload"])
        self.assertNotIn("page", stored["payload"])

    def test_preview_text_bounds(self):
        self.assertEqual(work_log._preview_text("x" * 300), "x" * 200)
        self.assertEqual(work_log._preview_text("a\x00b\x01c\nd"),
                         "abcd")
        self.assertEqual(work_log._preview_text(""), "")
        self.assertEqual(work_log._preview_text("   "), "")
        self.assertEqual(work_log._preview_text(None), "")
        self.assertEqual(work_log._preview_text(123), "")


class SessionLogCardTests(DraftCase):
    """Phase-2c §5.6 card content: ≤4 what-happened bullets, one strip.

    Open TODOs/Decisions/Next are unchanged (the decisions placeholder
    stays until a feeder exists); Changes/Files prose is gone, replaced
    by the single collapsed context strip.
    """

    def _draft(self, **over):
        session = make_session()
        kwargs = {"sessions": [session], "changes": default_changes()}
        kwargs.update(over)
        evidence = FakeEvidence(**kwargs)
        return work_log.generate_draft(
            PID, SID, conn=self.conn, now_ms=NOW, evidence=evidence,
            settings=self.settings())

    def _by_id(self, draft):
        return {s["id"]: s["items"] for s in draft["sections"]}

    def test_what_happened_caps_at_four_bullets(self):
        resources = [{"resource": {"adapter": "file",
                                   "file": "/home/user/code/demo/f%d.py" % i},
                      "occurrence_count": i} for i in range(10)]
        draft = self._draft(resources=resources)
        items = self._by_id(draft)["what_happened"]
        self.assertLessEqual(len(items), 4)
        self.assertGreaterEqual(len(items), 2)
        # At most one duration/apps/events prose line, trailing.
        prose = [item for item in items if "Session lasted" in item]
        self.assertEqual(len(prose), 1)
        self.assertEqual(items[-1], prose[0])
        self.assertIn("events across", prose[0])
        # Resource bullets lead, with basenames only (no home paths).
        self.assertTrue(any("f0.py" in item for item in items))
        for item in items:
            self.assertNotIn("/home/user", item)

    def test_what_happened_without_resources_is_one_line(self):
        draft = self._draft(resources=[])
        items = self._by_id(draft)["what_happened"]
        self.assertEqual(len(items), 1)
        self.assertIn("Session lasted", items[0])
        self.assertIn("events", items[0])

    def test_context_strip_format_and_bounds(self):
        resources = [{"resource": {"adapter": "file",
                                   "file": "/x/main.py"},
                      "occurrence_count": 3}]
        draft = self._draft(resources=resources)
        context = draft["context"]
        self.assertRegex(context["summary"], r"^\d+ commits? · \d+ files?$")
        self.assertLessEqual(len(context["detail"]), 8)
        self.assertIn("- Context: %s" % context["summary"],
                      draft["markdown"])
        for entry in context["detail"]:
            self.assertIn(entry, draft["markdown"])

    def test_strip_counts_commits_and_files(self):
        evidence_text = ("abc123def456 range one\n"
                         "123abc456def range two\n"
                         "deadbeef1234 range three\n")
        changes = default_changes(evidence=evidence_text)
        session = make_session()
        resources = [{"resource": {"adapter": "file",
                                   "file": "/x/a.py"},
                      "occurrence_count": 1},
                     {"resource": {"adapter": "file",
                                   "file": "/x/b.py"},
                      "occurrence_count": 2}]
        evidence = FakeEvidence(sessions=[session], resources=resources,
                                todos={"todos": []}, changes=changes)
        draft = work_log.generate_draft(
            PID, SID, conn=self.conn, now_ms=NOW, evidence=evidence,
            settings=self.settings())
        self.assertEqual(draft["context"]["summary"], "3 commits · 2 files")

    def test_no_changes_or_files_prose(self):
        draft = self._draft()
        self.assertEqual([s["id"] for s in draft["sections"]],
                         ["what_happened", "open_todos",
                          "decisions", "next"])
        self.assertNotIn("## Changes", draft["markdown"])
        self.assertNotIn("## Files", draft["markdown"])
        self.assertNotIn("Committed during", draft["markdown"])
        self.assertNotIn("M foo.py", draft["markdown"])

    def test_other_sections_unchanged(self):
        draft = self._draft()
        by_id = self._by_id(draft)
        # Decisions placeholder stays until a feeder exists.
        self.assertEqual(by_id["decisions"],
                         ["No decisions recorded for this session."])
        self.assertTrue(any("No open TODOs" in item
                            for item in by_id["open_todos"]))
        self.assertTrue(any("next step" in item.lower()
                            for item in by_id["next"]))


if __name__ == "__main__":
    unittest.main()
