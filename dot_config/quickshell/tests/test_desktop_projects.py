import hashlib
import io
import json
import os
import selectors
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import desktop_projects
import projects

EXTENSION = (ROOT / ".pi" / "extensions" / "desktop-agent.ts").read_text(encoding="utf-8")


def run_cli(args, env_extra=None):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "desktop_projects.py"), *args],
        text=True, capture_output=True, timeout=15, check=False, env=env,
    )
    return completed


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.old_bin = os.environ.get(desktop_projects.BIN_ENV)
        self.old_db = os.environ.get(desktop_projects.DB_ENV_PRIMARY)
        self.old_db2 = os.environ.get(desktop_projects.DB_ENV_FALLBACK)
        for key in (desktop_projects.BIN_ENV, desktop_projects.DB_ENV_PRIMARY,
                    desktop_projects.DB_ENV_FALLBACK):
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in ((desktop_projects.BIN_ENV, self.old_bin),
                           (desktop_projects.DB_ENV_PRIMARY, self.old_db),
                           (desktop_projects.DB_ENV_FALLBACK, self.old_db2)):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_bin_default_explicit_env_precedence(self):
        self.assertEqual(desktop_projects.resolve_desktop_bin(None),
                         desktop_projects.DEFAULT_BIN)
        self.assertEqual(desktop_projects.resolve_desktop_bin(""),
                         desktop_projects.DEFAULT_BIN)
        os.environ[desktop_projects.BIN_ENV] = "/tmp/from-env"
        self.assertEqual(str(desktop_projects.resolve_desktop_bin(None)),
                         "/tmp/from-env")
        self.assertEqual(str(desktop_projects.resolve_desktop_bin("/tmp/explicit")),
                         "/tmp/explicit")

    def test_db_explicit_env_default(self):
        self.assertIsNone(desktop_projects.resolve_db_path(None))
        os.environ[desktop_projects.DB_ENV_PRIMARY] = "/tmp/a.db"
        self.assertEqual(desktop_projects.resolve_db_path(None), "/tmp/a.db")
        self.assertEqual(desktop_projects.resolve_db_path("/tmp/b.db"), "/tmp/b.db")
        os.environ.pop(desktop_projects.DB_ENV_PRIMARY)
        os.environ[desktop_projects.DB_ENV_FALLBACK] = "/tmp/c.db"
        self.assertEqual(desktop_projects.resolve_db_path(None), "/tmp/c.db")

    def test_uuid_and_limit_validation(self):
        with self.assertRaises(desktop_projects.DesktopError):
            desktop_projects._validate_project_id("not-a-uuid")
        with self.assertRaises(desktop_projects.DesktopError):
            desktop_projects._validate_project_id("")
        canonical = desktop_projects._validate_project_id(
            "AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE")
        self.assertEqual(canonical, "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
        self.assertEqual(desktop_projects._validate_limit(None), 20)
        self.assertEqual(desktop_projects._validate_limit("5"), 5)
        for bad in (0, 1001, "x", True, 1.5):
            with self.assertRaises(desktop_projects.DesktopError):
                desktop_projects._validate_limit(bad)


class RegistryOverrideEnvTests(unittest.TestCase):
    """Explicit --projects-file must reach the Rust detector child-only.

    Detection (`current`) and lookup (`list_projects`) must share one
    stable registry file: the explicit override is resolved once via
    `projects.resolve_registry_file` and forwarded as child-only env
    (`QUICKSHELL_PROJECTS_FILE`), never mutating `os.environ`. Without an
    explicit override the child inherits the parent environment unchanged.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.custom = str(self.base / "custom.toml")
        self.inherited = str(self.base / "inherited.toml")
        Path(self.inherited).write_text("version = 1\n", encoding="utf-8")
        created = projects.create_project({"name": "Custom"}, self.custom)
        self.entry = created["project"]
        self.old_env = os.environ.get(projects.ENV_VAR)
        os.environ[projects.ENV_VAR] = self.inherited

    def tearDown(self):
        if self.old_env is None:
            os.environ.pop(projects.ENV_VAR, None)
        else:
            os.environ[projects.ENV_VAR] = self.old_env
        self.temp.cleanup()

    def _canned_proc(self, payload):
        proc = unittest.mock.MagicMock()
        proc.stdout.fileno.return_value = 101
        proc.stderr.fileno.return_value = 102
        proc.poll.return_value = 0
        proc.returncode = 0
        raw = json.dumps(payload).encode("utf-8")
        proc.stdout.read.side_effect = [raw, b""]
        proc.stderr.read.side_effect = [b""]
        return proc

    def _canned_current(self, project):
        return {"available": True, "source": "hyprland",
                "observed_at_ms": 1, "focused_window": None,
                "workspace": None, "resource": None, "project": project}

    def test_explicit_override_forwarded_child_only(self):
        report = {"id": self.entry["id"], "name": "StaleName",
                  "matched_by": "cwd"}
        captured = {}
        canned = self._canned_current(report)

        def fake_popen(argv, **kwargs):
            captured["argv"] = argv
            captured["env"] = kwargs.get("env")
            return self._canned_proc(canned)

        with patch("subprocess.Popen", side_effect=fake_popen):
            with patch("os.set_blocking"):
                with patch("selectors.DefaultSelector") as sel_cls:
                    sel = unittest.mock.MagicMock()
                    sel.select.return_value = []
                    sel_cls.return_value = sel
                    out = desktop_projects.current_project(
                        self.custom, "/bin/x", None)
        expected = str(projects.resolve_registry_file(self.custom))
        self.assertIsNotNone(captured.get("env"))
        self.assertEqual(
            captured["env"].get(projects.ENV_VAR), expected)
        # Inherited env value must not leak into the child override.
        self.assertNotEqual(captured["env"].get(projects.ENV_VAR),
                            self.inherited)
        # No global mutation: parent env still points at inherited file.
        self.assertEqual(os.environ.get(projects.ENV_VAR), self.inherited)
        # Lookup used the same registry file (stable contract).
        self.assertEqual(
            projects.list_projects(self.custom)["file"], expected)
        self.assertEqual(out["registry"]["id"], self.entry["id"])
        self.assertEqual(out["project"]["name"], "Custom")
        self.assertEqual(out["logseq_path"], "")

    def test_no_override_inherits_env_unchanged(self):
        captured = {}
        canned = self._canned_current(None)

        def fake_popen(argv, **kwargs):
            captured["argv"] = argv
            captured["env"] = kwargs.get("env")
            return self._canned_proc(canned)

        with patch("subprocess.Popen", side_effect=fake_popen):
            with patch("os.set_blocking"):
                with patch("selectors.DefaultSelector") as sel_cls:
                    sel = unittest.mock.MagicMock()
                    sel.select.return_value = []
                    sel_cls.return_value = sel
                    out = desktop_projects.current_project(
                        None, "/bin/x", None)
        # Child-only override absent: inherit parent env (Popen env=None).
        self.assertIsNone(captured.get("env"))
        self.assertEqual(os.environ.get(projects.ENV_VAR), self.inherited)
        self.assertEqual(out["status"], "unassociated")


class CurrentIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.reg = str(self.base / "projects.toml")
        self.old_env = os.environ.get(projects.ENV_VAR)
        os.environ.pop(projects.ENV_VAR, None)
        self.graph = self.base / "graph"
        (self.graph / "pages").mkdir(parents=True)
        (self.graph / "journals").mkdir()

    def tearDown(self):
        if self.old_env is None:
            os.environ.pop(projects.ENV_VAR, None)
        else:
            os.environ[projects.ENV_VAR] = self.old_env
        self.temp.cleanup()

    def make_entry(self, name="Demo", logseq_path="pages/Demo.md"):
        created = projects.create_project(
            {"name": name, "logseq_path": logseq_path} if logseq_path else
            {"name": name}, self.reg)
        return created["project"]

    def fake_current(self, project):
        return {"available": True, "source": "hyprland",
                "observed_at_ms": 1, "focused_window": None,
                "workspace": None, "resource": None, "project": project}

    def test_associated_uses_current_registry_mapping_not_history(self):
        entry = self.make_entry(name="Old", logseq_path="pages/Old.md")
        pid = entry["id"]
        # Registry renames after the compositor snapshot: new name/path wins.
        listed = projects.list_projects(self.reg)
        projects.update_project({"id": pid, "revision": listed["revision"],
                                 "name": "New",
                                 "logseq_path": "pages/New.md"}, self.reg)
        stale_report = {"id": pid, "name": "Old", "matched_by": "cwd"}
        with patch.object(desktop_projects, "_fetch_current",
                          return_value=self.fake_current(stale_report)):
            out = desktop_projects.current_project(self.reg, "/bin/x", None)
        self.assertEqual(out["status"], "associated")
        self.assertEqual(out["registry"]["name"], "New")
        self.assertEqual(out["logseq_path"], "pages/New.md")
        self.assertEqual(out["project"]["name"], "New")
        self.assertEqual(out["project"]["matched_by"], "cwd")
        self.assertTrue(out["has_logseq_linkage"])

    def test_unassociated_needs_no_graph(self):
        with patch.object(desktop_projects, "_fetch_current",
                          return_value=self.fake_current(None)):
            with patch("logseq_common.resolve_graph") as resolve:
                resolve.side_effect = AssertionError("graph must not be needed")
                out = desktop_projects.project_todos(
                    None, None, self.reg, "/bin/x", None)
                todos_out = out
                ctx_out = desktop_projects.project_logseq_context(
                    None, None, self.reg, "/bin/x", None)
        self.assertIsNone(todos_out["project"])
        self.assertEqual(todos_out["todos"], [])
        self.assertEqual(todos_out["status"], "unassociated")
        self.assertFalse(todos_out["has_logseq_linkage"])
        self.assertEqual(ctx_out["content"], "")

    def test_stale_removed_maps_to_unknown(self):
        entry = self.make_entry(name="Gone", logseq_path="pages/Gone.md")
        pid = entry["id"]
        listed = projects.list_projects(self.reg)
        projects.remove_project({"id": pid, "revision": listed["revision"]},
                                self.reg)
        stale = {"id": pid, "name": "Gone", "matched_by": "file"}
        with patch.object(desktop_projects, "_fetch_current",
                          return_value=self.fake_current(stale)):
            cur = desktop_projects.current_project(self.reg, "/bin/x", None)
            self.assertEqual(cur["status"], "stale-removed")
            self.assertIsNone(cur["project"])
            self.assertIsNone(cur["registry"] if cur["registry"] is None else None) \
                if False else self.assertIsNone(cur["registry"])
            with patch("logseq_common.resolve_graph") as resolve:
                resolve.side_effect = AssertionError("no graph for stale")
                todos = desktop_projects.project_todos(
                    None, None, self.reg, "/bin/x", None)
        self.assertEqual(todos["status"], "stale-removed")
        self.assertEqual(todos["todos"], [])

    def test_name_only_returns_explicit_no_linkage(self):
        entry = self.make_entry(name="Solo", logseq_path="")
        pid = entry["id"]
        report = {"id": pid, "name": "Solo", "matched_by": "git_remote"}
        with patch.object(desktop_projects, "_fetch_current",
                          return_value=self.fake_current(report)):
            with patch("logseq_common.resolve_graph") as resolve:
                resolve.side_effect = AssertionError("no graph for name-only")
                with patch("project_planner.read_page") as rp:
                    rp.side_effect = AssertionError("no read for name-only")
                    todos = desktop_projects.project_todos(
                        None, None, self.reg, "/bin/x", None)
                    ctx = desktop_projects.project_logseq_context(
                        None, None, self.reg, "/bin/x", None)
        self.assertEqual(todos["status"], "name-only-no-linkage")
        self.assertFalse(todos["has_logseq_linkage"])
        self.assertIn("not guessing", todos["reason"])
        self.assertEqual(ctx["content"], "")

    def test_todos_reuses_read_page(self):
        (self.graph / "pages" / "Demo.md").write_text(
            "- TODO ship\n- DONE old\n", encoding="utf-8")
        entry = self.make_entry(name="Demo", logseq_path="pages/Demo.md")
        report = {"id": entry["id"], "name": "Demo", "matched_by": "cwd"}
        with patch.object(desktop_projects, "_fetch_current",
                          return_value=self.fake_current(report)):
            out = desktop_projects.project_todos(
                str(self.graph), None, self.reg, "/bin/x", None)
        self.assertEqual(out["path"], "pages/Demo.md")
        self.assertEqual(out["page"], "Demo")
        self.assertTrue(any(t["task"] == "ship" for t in out["todos"]))
        # No duplicate task parser in the new backend.
        source = (ROOT / "scripts" / "desktop_projects.py").read_text(
            encoding="utf-8")
        self.assertNotIn("TODO|NOW|LATER", source)
        self.assertIn("project_planner.read_page", source)
        self.assertIn("read_page(graph", source)

    def test_explicit_project_skips_current(self):
        entry = self.make_entry(name="Exp", logseq_path="pages/Demo.md")
        (self.graph / "pages" / "Demo.md").write_text("- TODO x\n",
                                                     encoding="utf-8")
        with patch.object(desktop_projects, "_fetch_current") as cur:
            cur.side_effect = AssertionError("explicit must not fetch current")
            out = desktop_projects.project_todos(
                str(self.graph), entry["id"], self.reg, "/bin/x", None)
        self.assertEqual(out["requested_project_id"], entry["id"])
        self.assertEqual(out["path"], "pages/Demo.md")

    def test_explicit_unknown_still_queries_history(self):
        ghost = "00000000-0000-4000-8000-000000000000"
        rows = [{"id": 1, "observed_at_ms": 5, "kind": "focus",
                 "source": "hyprland", "snapshot": {}}]
        with patch.object(desktop_projects, "_fetch_history",
                          return_value=rows) as hist:
            out = desktop_projects.recent_activity(
                ghost, 5, self.reg, "/bin/x", None)
        hist.assert_called_once()
        self.assertEqual(out["requested_project_id"], ghost)
        self.assertEqual(out["history"], rows)
        self.assertIsNone(out["registry"])
        # Logseq view for unknown explicit needs no graph.
        with patch("logseq_common.resolve_graph") as resolve:
            resolve.side_effect = AssertionError("no graph for unknown")
            todos = desktop_projects.project_todos(
                None, ghost, self.reg, "/bin/x", None)
        self.assertEqual(todos["todos"], [])

    def test_history_defaults_to_current(self):
        entry = self.make_entry(name="H", logseq_path="pages/H.md")
        report = {"id": entry["id"], "name": "H", "matched_by": "file"}
        rows = [{"id": 2, "observed_at_ms": 9, "kind": "focus",
                 "source": "hyprland", "snapshot": {}}]
        with patch.object(desktop_projects, "_fetch_current",
                          return_value=self.fake_current(report)):
            with patch.object(desktop_projects, "_fetch_history",
                              return_value=rows) as hist:
                out = desktop_projects.recent_activity(
                    None, None, self.reg, "/bin/x", None)
        hist.assert_called_once()
        args = hist.call_args[0]
        self.assertEqual(args[2], entry["id"])
        self.assertEqual(args[3], 20)
        self.assertEqual(out["count"], 1)

    def test_history_no_current_needs_no_db_query(self):
        with patch.object(desktop_projects, "_fetch_current",
                          return_value=self.fake_current(None)):
            with patch.object(desktop_projects, "_fetch_history") as hist:
                hist.side_effect = AssertionError("no DB query when unassociated")
                out = desktop_projects.recent_activity(
                    None, None, self.reg, "/bin/x", None)
                last = desktop_projects.last_activity(
                    None, self.reg, "/bin/x", None)
                res = desktop_projects.project_resources(
                    None, None, self.reg, "/bin/x", None)
        self.assertEqual(out["history"], [])
        self.assertIsNone(last["activity"])
        self.assertEqual(res["resources"], [])

    def test_name_only_default_history_still_queries(self):
        # Name-only projects have identity but no Logseq page: history,
        # last-activity and resources must still query by id.
        entry = self.make_entry(name="Solo", logseq_path="")
        report = {"id": entry["id"], "name": "Solo", "matched_by": "cwd"}
        rows = [{"id": 3, "observed_at_ms": 7, "kind": "focus",
                 "source": "hyprland", "snapshot": {}}]
        row = {"id": 3, "observed_at_ms": 7, "kind": "focus",
               "source": "hyprland", "snapshot": {}}
        items = [{"resource": {"adapter": "kitty"}, "observed_at_ms": 7,
                  "activity_id": 3}]
        with patch.object(desktop_projects, "_fetch_current",
                          return_value=self.fake_current(report)):
            with patch.object(desktop_projects, "_fetch_history",
                              return_value=rows) as hist:
                out = desktop_projects.recent_activity(
                    None, None, self.reg, "/bin/x", None)
            self.assertEqual(out["count"], 1)
            self.assertEqual(out["history"], rows)
            self.assertEqual(out["project"]["id"], entry["id"])
            self.assertEqual(out["project"]["name"], "Solo")
            hist.assert_called_once()
            with patch.object(desktop_projects, "_fetch_last_activity",
                              return_value=row):
                last = desktop_projects.last_activity(
                    None, self.reg, "/bin/x", None)
            self.assertEqual(last["activity"], row)
            self.assertEqual(last["project"]["id"], entry["id"])
            with patch.object(desktop_projects, "_fetch_resources",
                              return_value=items):
                res = desktop_projects.project_resources(
                    None, 5, self.reg, "/bin/x", None)
            self.assertEqual(res["resources"], items)
            self.assertEqual(res["project"]["id"], entry["id"])
        # Logseq views still need no graph and stay empty for name-only.
        with patch.object(desktop_projects, "_fetch_current",
                          return_value=self.fake_current(report)):
            with patch("logseq_common.resolve_graph") as resolve:
                resolve.side_effect = AssertionError("no graph for name-only")
                todos = desktop_projects.project_todos(
                    None, None, self.reg, "/bin/x", None)
        self.assertEqual(todos["todos"], [])
        self.assertEqual(todos["status"], "name-only-no-linkage")

    def test_last_and_resources_shapes(self):
        entry = self.make_entry(name="R", logseq_path="pages/R.md")
        report = {"id": entry["id"], "name": "R", "matched_by": "cwd"}
        row = {"id": 7, "observed_at_ms": 11, "kind": "focus",
               "source": "hyprland", "snapshot": {"available": True}}
        items = [{"resource": {"adapter": "kitty", "file": "/tmp/x",
                               "cwd": None, "git_root": None,
                               "git_branch": None, "git_remote": None,
                               "url": None, "page": None, "title": "t"},
                  "observed_at_ms": 11, "activity_id": 7}]
        with patch.object(desktop_projects, "_fetch_current",
                          return_value=self.fake_current(report)):
            with patch.object(desktop_projects, "_fetch_last_activity",
                              return_value=row):
                last = desktop_projects.last_activity(
                    None, self.reg, "/bin/x", None)
            with patch.object(desktop_projects, "_fetch_resources",
                              return_value=items):
                res = desktop_projects.project_resources(
                    None, 5, self.reg, "/bin/x", None)
        self.assertEqual(last["activity"], row)
        self.assertEqual(res["resources"], items)
        self.assertEqual(res["count"], 1)


class SessionOperationsTests(unittest.TestCase):
    SID = "a" * 32
    SID_UPPER = "A" * 32
    GHOST = "00000000-0000-4000-8000-000000000000"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.reg = str(self.base / "projects.toml")
        old = os.environ.get(projects.ENV_VAR)
        self._old_reg_env = old
        os.environ.pop(projects.ENV_VAR, None)

    def tearDown(self):
        if self._old_reg_env is None:
            os.environ.pop(projects.ENV_VAR, None)
        else:
            os.environ[projects.ENV_VAR] = self._old_reg_env
        self.temp.cleanup()

    def test_session_id_validation_canonical(self):
        self.assertEqual(
            desktop_projects._validate_session_id(self.SID_UPPER), self.SID)
        for bad in ("", "xyz", "a" * 31, "a" * 33, "g" * 32,
                    "a" * 31 + " ", None, 123, True):
            with self.assertRaises(desktop_projects.DesktopError):
                desktop_projects._validate_session_id(bad)

    def test_session_range_pairing_and_bounds(self):
        self.assertEqual(
            desktop_projects._validate_session_range(None, None), (None, None))
        # Only omitted (None) means absent: explicit empty/whitespace values
        # reject, fail closed, and never become an unrestricted search.
        for frm, to in (("", ""), (" ", "  "), ("", None), (None, ""),
                        (" ", None), (None, "  "), ("5", ""), ("", "5")):
            with self.assertRaises(desktop_projects.DesktopError):
                desktop_projects._validate_session_range(frm, to)
        self.assertEqual(
            desktop_projects._validate_session_range(0, 10), (0, 10))
        self.assertEqual(
            desktop_projects._validate_session_range("5", "5"), (5, 5))
        self.assertEqual(
            desktop_projects._validate_session_range(
                0, desktop_projects.I64_MAX),
            (0, desktop_projects.I64_MAX))
        for frm, to in ((5, None), (None, 5), ("5", None), (None, "5")):
            with self.assertRaises(desktop_projects.DesktopError) as ctx:
                desktop_projects._validate_session_range(frm, to)
            self.assertIn("together", str(ctx.exception))
        for frm, to in ((-1, 5), (5, -1), (10, 5), ("x", 5), (5, "y"),
                        (True, 5), (5, 1.5),
                        (desktop_projects.I64_MAX + 1, desktop_projects.I64_MAX + 1),
                        (0, desktop_projects.I64_MAX + 1),
                        (str(desktop_projects.I64_MAX + 1),
                         str(desktop_projects.I64_MAX + 1))):
            with self.assertRaises(desktop_projects.DesktopError):
                desktop_projects._validate_session_range(frm, to)

    def test_current_session_no_compositor_or_registry(self):
        row = {"session_id": self.SID, "start_ms": 1}
        with patch.object(desktop_projects, "_fetch_current") as cur, \
                patch.object(desktop_projects, "_load_registry") as reg, \
                patch.object(desktop_projects, "_resolve_current_identity") as ident, \
                patch.object(desktop_projects, "_run_desktop_cli",
                             return_value=json.dumps(row).encode()) as run:
            cur.side_effect = AssertionError("no compositor call")
            reg.side_effect = AssertionError("no registry call")
            ident.side_effect = AssertionError("no identity call")
            out = desktop_projects.current_session("/bin/x", None)
        self.assertEqual(out["session"], row)
        self.assertEqual(out["reason"], "")
        argv = run.call_args[0][2]
        self.assertEqual(argv, ["current-session"])
        with patch.object(desktop_projects, "_run_desktop_cli",
                          return_value=b"null"):
            out = desktop_projects.current_session("/bin/x", None)
        self.assertIsNone(out["session"])
        self.assertIn("no current session", out["reason"])
        with patch.object(desktop_projects, "_run_desktop_cli",
                          return_value=b"[]"):
            with self.assertRaises(desktop_projects.DesktopError):
                desktop_projects.current_session("/bin/x", None)

    def test_sessions_all_no_compositor_or_registry(self):
        rows = [{"session_id": self.SID}]
        with patch.object(desktop_projects, "_fetch_current") as cur, \
                patch.object(desktop_projects, "_load_registry") as reg, \
                patch.object(desktop_projects, "_resolve_current_identity") as ident, \
                patch.object(desktop_projects, "_resolve_explicit_identity") as expl, \
                patch.object(desktop_projects, "_run_desktop_cli",
                             return_value=json.dumps(rows).encode()) as run:
            cur.side_effect = AssertionError("no compositor call")
            reg.side_effect = AssertionError("no registry call")
            ident.side_effect = AssertionError("no identity call")
            expl.side_effect = AssertionError("no explicit identity call")
            out = desktop_projects.work_sessions(
                None, 5, None, None, self.reg, "/bin/x", None)
        self.assertEqual(out["sessions"], rows)
        self.assertEqual(out["count"], 1)
        self.assertIsNone(out["project"])
        self.assertIsNone(out["registry"])
        self.assertIsNone(out["requested_project_id"])
        argv = run.call_args[0][2]
        self.assertEqual(argv, ["sessions", "--limit", "5"])

    def test_sessions_range_passes_to_rust(self):
        with patch.object(desktop_projects, "_run_desktop_cli",
                          return_value=b"[]") as run:
            out = desktop_projects.work_sessions(
                None, 7, 100, 200, self.reg, "/bin/x", None)
        argv = run.call_args[0][2]
        self.assertEqual(
            argv, ["sessions", "--limit", "7", "--from", "100", "--to", "200"])
        self.assertEqual(out["count"], 0)

    def test_sessions_explicit_unknown_still_queries(self):
        rows = [{"session_id": self.SID}]
        with patch.object(desktop_projects, "_fetch_current") as cur, \
                patch.object(desktop_projects, "_fetch_sessions",
                             return_value=rows) as fetch:
            cur.side_effect = AssertionError("explicit must not fetch current")
            out = desktop_projects.work_sessions(
                self.GHOST, 5, None, None, self.reg, "/bin/x", None)
        fetch.assert_called_once()
        args = fetch.call_args[0]
        self.assertEqual(args[2], self.GHOST)
        self.assertEqual(args[3], 5)
        self.assertEqual(out["requested_project_id"], self.GHOST)
        self.assertIsNone(out["registry"])
        self.assertEqual(out["sessions"], rows)
        self.assertIn("unknown or deleted", out["reason"])

    def test_sessions_explicit_known_resolves_display(self):
        created = projects.create_project({"name": "W"}, self.reg)
        pid = created["project"]["id"]
        rows = [{"session_id": self.SID}]
        with patch.object(desktop_projects, "_fetch_sessions",
                          return_value=rows):
            out = desktop_projects.work_sessions(
                pid, 5, None, None, self.reg, "/bin/x", None)
        self.assertEqual(out["project"]["id"], pid)
        self.assertEqual(out["project"]["name"], "W")
        self.assertEqual(out["registry"]["id"], pid)
        self.assertEqual(out["reason"], "")

    def test_last_session_requires_project_and_queries_unknown(self):
        with self.assertRaises(desktop_projects.DesktopError) as ctx:
            desktop_projects.last_session(None, self.reg, "/bin/x", None)
        self.assertIn("explicit", str(ctx.exception).lower())
        with self.assertRaises(desktop_projects.DesktopError):
            desktop_projects.last_session(" ", self.reg, "/bin/x", None)
        row = {"session_id": self.SID}
        with patch.object(desktop_projects, "_fetch_current") as cur, \
                patch.object(desktop_projects, "_fetch_last_session",
                             return_value=row) as fetch:
            cur.side_effect = AssertionError("no compositor call")
            out = desktop_projects.last_session(
                self.GHOST, self.reg, "/bin/x", None)
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args[0][2], self.GHOST)
        self.assertEqual(out["session"], row)
        self.assertEqual(out["requested_project_id"], self.GHOST)
        self.assertIsNone(out["registry"])
        with patch.object(desktop_projects, "_fetch_last_session",
                          return_value=None):
            out = desktop_projects.last_session(
                self.GHOST, self.reg, "/bin/x", None)
            self.assertIsNone(out["session"])

    def test_session_resources_and_events_argv_shape(self):
        items = [{"session_id": self.SID}]
        rows = [{"id": 1}]
        with patch.object(desktop_projects, "_fetch_current") as cur, \
                patch.object(desktop_projects, "_load_registry") as reg, \
                patch.object(desktop_projects, "_run_desktop_cli",
                             return_value=json.dumps(items).encode()) as run:
            cur.side_effect = AssertionError("no compositor call")
            reg.side_effect = AssertionError("no registry call")
            out = desktop_projects.session_resources(
                self.SID_UPPER, 5, "/bin/x", None)
        self.assertEqual(out["session_id"], self.SID)
        self.assertEqual(out["resources"], items)
        self.assertEqual(out["count"], 1)
        self.assertEqual(run.call_args[0][2],
                         ["session-resources", "--session", self.SID,
                          "--limit", "5"])
        with patch.object(desktop_projects, "_fetch_current") as cur, \
                patch.object(desktop_projects, "_load_registry") as reg, \
                patch.object(desktop_projects, "_run_desktop_cli",
                             return_value=json.dumps(rows).encode()) as run:
            cur.side_effect = AssertionError("no compositor call")
            reg.side_effect = AssertionError("no registry call")
            out = desktop_projects.session_events(
                self.SID, None, "/bin/x", None)
        self.assertEqual(out["session_id"], self.SID)
        self.assertEqual(out["events"], rows)
        self.assertEqual(run.call_args[0][2],
                         ["session-events", "--session", self.SID,
                          "--limit", "20"])
        with self.assertRaises(desktop_projects.DesktopError):
            desktop_projects.session_resources("bad", 5, "/bin/x", None)
        with self.assertRaises(desktop_projects.DesktopError):
            desktop_projects.session_events(self.SID, 0, "/bin/x", None)

    def test_cli_errors_for_session_commands(self):
        missing_project = run_cli(
            ["--desktop-bin", "/nonexistent-qs-bin-xyz", "last-session"])
        self.assertNotEqual(missing_project.returncode, 0)
        self.assertIn("explicit", missing_project.stderr.lower())
        missing_session = run_cli(
            ["--desktop-bin", "/nonexistent-qs-bin-xyz", "session-resources"])
        self.assertNotEqual(missing_session.returncode, 0)
        self.assertIn("--session", missing_session.stderr)
        bad_session = run_cli(
            ["--desktop-bin", "/nonexistent-qs-bin-xyz",
             "session-events", "--session", "bad"])
        self.assertNotEqual(bad_session.returncode, 0)
        self.assertIn("32 hex", bad_session.stderr)
        unpaired = run_cli(
            ["--desktop-bin", "/nonexistent-qs-bin-xyz",
             "sessions", "--from", "5"])
        self.assertNotEqual(unpaired.returncode, 0)
        self.assertIn("together", unpaired.stderr)
        bad_range = run_cli(
            ["--desktop-bin", "/nonexistent-qs-bin-xyz",
             "sessions", "--from", "9", "--to", "3"])
        self.assertNotEqual(bad_range.returncode, 0)
        self.assertIn("range", bad_range.stderr.lower())
        bad_limit = run_cli(
            ["--desktop-bin", "/nonexistent-qs-bin-xyz",
             "sessions", "--limit", "0"])
        self.assertNotEqual(bad_limit.returncode, 0)
        self.assertIn("limit", bad_limit.stderr.lower())
        rejects = run_cli(
            ["current-session", "--limit", "5"])
        self.assertNotEqual(rejects.returncode, 0)
        self.assertIn("does not accept", rejects.stderr)
        raw_rejects = run_cli(
            ["--desktop-bin", "/nonexistent-qs-bin-xyz",
             "recent-activity", "--session", self.SID])
        self.assertNotEqual(raw_rejects.returncode, 0)
        self.assertIn("does not accept", raw_rejects.stderr)
        for completed in (missing_project, missing_session, bad_session,
                          unpaired, bad_range, bad_limit, rejects):
            self.assertNotIn("Traceback", completed.stderr)
            self.assertEqual(completed.stdout, "")

    def test_cli_errors_surface_rust_stderr(self):
        # Rust failures surface as single-line errors with no traceback.
        with patch("subprocess.Popen") as popen:
            proc = unittest.mock.MagicMock()
            proc.stdout.fileno.return_value = 91
            proc.stderr.fileno.return_value = 92
            proc.poll.return_value = 0
            proc.returncode = 1
            proc.stdout.read.side_effect = [b"", b""]
            proc.stderr.read.side_effect = [b"sessions query failed: boom", b""]
            popen.return_value = proc
            with patch("os.set_blocking"):
                with patch("selectors.DefaultSelector") as sel_cls:
                    sel = unittest.mock.MagicMock()
                    sel.select.return_value = []
                    sel_cls.return_value = sel
                    with self.assertRaises(desktop_projects.DesktopError) as ctx:
                        desktop_projects.current_session("/bin/x", None)
            self.assertIn("boom", str(ctx.exception))


class SubprocessSafetyTests(unittest.TestCase):
    def test_missing_binary_is_clear_no_build(self):
        with self.assertRaises(desktop_projects.DesktopError) as ctx:
            desktop_projects._run_desktop_cli(
                Path("/nonexistent-qs-desktop-12345"), None, ["current"])
        self.assertIn("not found", str(ctx.exception))
        self.assertIn("no automatic build", str(ctx.exception).lower()
                      if "no automatic" in str(ctx.exception).lower()
                      else str(ctx.exception))

    def test_timeout_output_and_json_errors(self):
        with patch("subprocess.Popen") as popen:
            popen.side_effect = FileNotFoundError("nope")
            with self.assertRaises(desktop_projects.DesktopError) as ctx:
                desktop_projects._run_desktop_cli(Path("/bin/x"), None,
                                                  ["current"])
            self.assertIn("not found", str(ctx.exception))
        with self.assertRaises(desktop_projects.DesktopError):
            desktop_projects._parse_json_output(b"not json", "current")
        with self.assertRaises(desktop_projects.DesktopError) as ctx:
            desktop_projects._validate_limit(0)
        self.assertIn("limit", str(ctx.exception).lower())

    def test_run_nonzero_surfaces_stderr(self):
        with patch("subprocess.Popen") as popen:
            proc = unittest.mock.MagicMock()
            proc.stdout.fileno.return_value = 99
            proc.stderr.fileno.return_value = 100
            proc.poll.return_value = 0
            proc.returncode = 1
            proc.stdout.read.side_effect = [b"", b""]
            proc.stderr.read.side_effect = [b"boom detail", b""]
            popen.return_value = proc
            with patch("os.set_blocking"):
                with patch("selectors.DefaultSelector") as sel_cls:
                    sel = unittest.mock.MagicMock()
                    sel.select.return_value = []
                    sel_cls.return_value = sel
                    with self.assertRaises(desktop_projects.DesktopError) as ctx:
                        desktop_projects._run_desktop_cli(
                            Path("/bin/x"), None, ["current"], timeout=2.0)
            self.assertIn("boom", str(ctx.exception))

    def test_run_streaming_timeout_kills_and_reaps_real(self):
        start = __import__("time").monotonic()
        with self.assertRaises(desktop_projects.DesktopError) as ctx:
            desktop_projects._run_desktop_cli(
                Path(sys.executable), None,
                ["-c", "import time; time.sleep(30)"], timeout=0.5)
        elapsed = __import__("time").monotonic() - start
        self.assertIn("timed out", str(ctx.exception))
        self.assertLess(elapsed, 10.0)
        # Pipes reaped: a follow-up call still works.
        with self.assertRaises(desktop_projects.DesktopError):
            desktop_projects._parse_json_output(b"not json", "current")

    def test_run_streaming_huge_output_bounded_real(self):
        # Real 3MiB stdout must trip the 2MiB cap while streaming, without
        # ever buffering unbounded output before the limit.
        script = ("import sys; sys.stdout.buffer.write(b'x' * %d)"
                  % (desktop_projects.MAX_OUTPUT + 1024 * 1024))
        start = __import__("time").monotonic()
        with self.assertRaises(desktop_projects.DesktopError) as ctx:
            desktop_projects._run_desktop_cli(
                Path(sys.executable), None, ["-c", script], timeout=10.0)
        elapsed = __import__("time").monotonic() - start
        self.assertIn("exceeded", str(ctx.exception))
        self.assertLess(elapsed, 15.0)

    def test_run_slow_child_reaped_no_zombie_real(self):
        # Slow direct child (descendant of the test process) must be killed
        # and reaped on timeout: bounded duration, no lingering sleep child.
        import time as _time
        marker = "qs-desktop-timeout-probe-30"
        start = _time.monotonic()
        with self.assertRaises(desktop_projects.DesktopError):
            desktop_projects._run_desktop_cli(
                Path(sys.executable), None,
                ["-c", "import time; time.sleep(30)  # %s" % marker],
                timeout=0.5)
        elapsed = _time.monotonic() - start
        self.assertLess(elapsed, 10.0)
        _time.sleep(1.0)  # let the kill/reap settle
        self.assertFalse(self._probe_sleep_alive(marker))

    def _probe_sleep_alive(self, marker):
        import subprocess as _sp
        try:
            out = _sp.run(["ps", "-o", "args="],
                          capture_output=True, text=True, timeout=5)
        except Exception:
            return False
        return marker in (out.stdout or "")

    def test_no_shell_used(self):
        source = (ROOT / "scripts" / "desktop_projects.py").read_text(
            encoding="utf-8")
        self.assertIn("shell=False", source)
        self.assertNotIn("shell=True", source)

    def test_direct_cli_sigterm_kills_and_reaps_child_real(self):
        # Direct `kill -TERM <desktop_projects.py>` must kill/reap its Rust
        # child instead of orphaning it (extension group-kill covers the
        # nested case; this covers direct CLI cancellation).
        import signal as _signal
        import time as _time
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "fake-bin.sh"
            fake.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
            fake.chmod(0o755)
            reg = str(Path(tmp) / "r.toml")
            Path(reg).write_text("version = 1\n", encoding="utf-8")
            proc = subprocess.Popen(
                [sys.executable, str(ROOT / "scripts" / "desktop_projects.py"),
                 "--desktop-bin", str(fake), "--projects-file", reg,
                 "current-project"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                start_new_session=False,
            )
            try:
                _time.sleep(1.0)  # let the CLI spawn sleep 30
                self.assertIsNone(proc.poll(), "CLI exited before TERM")
                proc.send_signal(_signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.fail("CLI did not exit promptly after SIGTERM")
            finally:
                try:
                    proc.kill()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=3)
                except Exception:
                    pass
                try:
                    if proc.stdout:
                        proc.stdout.close()
                except Exception:
                    pass
                try:
                    if proc.stderr:
                        proc.stderr.close()
                except Exception:
                    pass
            _time.sleep(1.0)
            # No orphaned `sleep 30` from the fake bin may remain.
            try:
                out = subprocess.run(
                    ["ps", "-o", "args="], capture_output=True, text=True,
                    timeout=5)
                self.assertNotIn(str(fake), out.stdout or "")
            except FileNotFoundError:
                pass

    def test_cli_error_contract_no_traceback(self):
        missing = run_cli(["--desktop-bin", "/nonexistent-qs-bin-xyz",
                           "current-project"])
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("error:", missing.stderr)
        self.assertNotIn("Traceback", missing.stderr)
        self.assertEqual(missing.stdout, "")
        bad_uuid = run_cli(["--desktop-bin", "/nonexistent-qs-bin-xyz",
                            "todos", "--project", "nope"])
        self.assertNotEqual(bad_uuid.returncode, 0)
        self.assertIn("UUID", bad_uuid.stderr)
        bad_limit = run_cli(["--desktop-bin", "/nonexistent-qs-bin-xyz",
                             "recent-activity", "--limit", "0"])
        self.assertNotEqual(bad_limit.returncode, 0)
        self.assertIn("limit", bad_limit.stderr.lower())
        rejects = run_cli(["current-project", "--project",
                           "00000000-0000-4000-8000-000000000000"])
        self.assertNotEqual(rejects.returncode, 0)
        self.assertIn("does not accept", rejects.stderr)


class RealBinaryBoundaryTests(unittest.TestCase):
    """Real `qs-desktop-context` boundaries: no mocks, temp private dir.

    Requires the release binary built via
    ``cargo build --locked --release --manifest-path
    services/agent-orchestrator/Cargo.toml``. All queries use a temp empty
    registry plus a missing-DB path and must never create files or start a
    collector (read-only `current`/`history`/`last-activity`/`resources`/
    `current-session`/`sessions`/`last-session`/`session-resources`/
    `session-events`).
    """

    BIN = ROOT / "services" / "agent-orchestrator" / "target" / "release" / "qs-desktop-context"
    GHOST = "00000000-0000-4000-8000-000000000000"

    def setUp(self):
        if not (self.BIN.is_file() and os.access(self.BIN, os.X_OK)):
            self.skipTest("real qs-desktop-context binary not built")
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.reg = str(self.base / "empty.toml")
        Path(self.reg).write_text("version = 1\n", encoding="utf-8")
        self.missing_db = str(self.base / "missing.db")
        old = os.environ.get(projects.ENV_VAR)
        self._old_reg_env = old
        os.environ.pop(projects.ENV_VAR, None)

    def tearDown(self):
        if self._old_reg_env is None:
            os.environ.pop(projects.ENV_VAR, None)
        else:
            os.environ[projects.ENV_VAR] = self._old_reg_env
        self.temp.cleanup()

    def test_real_current_empty_registry_needs_no_graph_no_create(self):
        out = desktop_projects.current_project(
            self.reg, str(self.BIN), self.missing_db)
        # Empty registry forces unknown regardless of live compositor.
        self.assertIn(out["status"], ("unassociated", "stale-removed"))
        self.assertIsNone(out["project"])
        self.assertIsNone(out["registry"])
        self.assertFalse(out["has_logseq_linkage"])
        self.assertIsInstance(out["context"], dict)
        self.assertIn("available", out["context"])
        # Read-only: missing DB must still be missing (no autostart/create).
        self.assertFalse(Path(self.missing_db).exists())
        # Default todos also needs no graph and stays empty.
        todos = desktop_projects.project_todos(
            None, None, self.reg, str(self.BIN), self.missing_db)
        self.assertEqual(todos["todos"], [])
        self.assertFalse(todos["has_logseq_linkage"])
        self.assertFalse(Path(self.missing_db).exists())

    def test_real_explicit_uuid_missing_db_empty_no_create(self):
        hist = desktop_projects.recent_activity(
            self.GHOST, 5, self.reg, str(self.BIN), self.missing_db)
        self.assertEqual(hist["requested_project_id"], self.GHOST)
        self.assertEqual(hist["history"], [])
        self.assertEqual(hist["count"], 0)
        self.assertIsNone(hist["registry"])
        last = desktop_projects.last_activity(
            self.GHOST, self.reg, str(self.BIN), self.missing_db)
        self.assertIsNone(last["activity"])
        res = desktop_projects.project_resources(
            self.GHOST, 5, self.reg, str(self.BIN), self.missing_db)
        self.assertEqual(res["resources"], [])
        self.assertEqual(res["count"], 0)
        self.assertFalse(Path(self.missing_db).exists())

    def test_real_cli_boundaries_no_traceback(self):
        bad_uuid = run_cli(["--projects-file", self.reg, "--db", self.missing_db,
                            "recent-activity", "--project", "not-a-uuid"])
        self.assertNotEqual(bad_uuid.returncode, 0)
        self.assertIn("error:", bad_uuid.stderr)
        self.assertNotIn("Traceback", bad_uuid.stderr)
        bad_limit = run_cli(["--projects-file", self.reg, "--db", self.missing_db,
                             "resources", "--project", self.GHOST, "--limit", "0"])
        self.assertNotEqual(bad_limit.returncode, 0)
        self.assertIn("limit", bad_limit.stderr.lower())
        self.assertFalse(Path(self.missing_db).exists())

    def test_real_session_commands_missing_db_empty_no_create(self):
        # Phase 4 binary required: the checked-in release binary may predate
        # current-session/sessions; skip instead of failing in that case.
        try:
            probe = subprocess.run(
                [str(self.BIN), "--help"], text=True, capture_output=True,
                timeout=10)
        except OSError:
            self.skipTest("real qs-desktop-context binary not runnable")
        combined = (probe.stdout or "") + (probe.stderr or "")
        if "current-session" not in combined:
            self.skipTest("real qs-desktop-context binary predates Phase 4 sessions")
        cur = desktop_projects.current_session(str(self.BIN), self.missing_db)
        self.assertIsNone(cur["session"])
        self.assertFalse(Path(self.missing_db).exists())
        all_sessions = desktop_projects.work_sessions(
            None, 5, None, None, self.reg, str(self.BIN), self.missing_db)
        self.assertEqual(all_sessions["sessions"], [])
        self.assertEqual(all_sessions["count"], 0)
        self.assertIsNone(all_sessions["project"])
        self.assertFalse(Path(self.missing_db).exists())
        scoped = desktop_projects.work_sessions(
            self.GHOST, 5, None, None, self.reg, str(self.BIN), self.missing_db)
        self.assertEqual(scoped["requested_project_id"], self.GHOST)
        self.assertEqual(scoped["sessions"], [])
        self.assertIsNone(scoped["registry"])
        ranged = desktop_projects.work_sessions(
            None, 5, 0, 10, self.reg, str(self.BIN), self.missing_db)
        self.assertEqual(ranged["sessions"], [])
        last = desktop_projects.last_session(
            self.GHOST, self.reg, str(self.BIN), self.missing_db)
        self.assertIsNone(last["session"])
        fake_sid = "b" * 32
        res = desktop_projects.session_resources(
            fake_sid, 5, str(self.BIN), self.missing_db)
        self.assertEqual(res["session_id"], fake_sid)
        self.assertEqual(res["resources"], [])
        events = desktop_projects.session_events(
            fake_sid, 5, str(self.BIN), self.missing_db)
        self.assertEqual(events["session_id"], fake_sid)
        self.assertEqual(events["events"], [])
        self.assertFalse(Path(self.missing_db).exists())

    def test_real_search_leading_hyphen_value_is_data(self):
        # Rust consumes the token following a filter flag explicitly, so a
        # leading-hyphen value is data (Python->Rust keeps separate argv).
        try:
            probe = subprocess.run(
                [str(self.BIN), "--help"], text=True, capture_output=True,
                timeout=10)
        except OSError:
            self.skipTest("real qs-desktop-context binary not runnable")
        combined = (probe.stdout or "") + (probe.stderr or "")
        if "search [--project" not in combined:
            self.skipTest("real qs-desktop-context binary predates Phase 5 search")
        completed = subprocess.run(
            [str(self.BIN), "--db", self.missing_db,
             "search", "--query", "--help", "--limit", "2"],
            text=True, capture_output=True, timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        value = json.loads(completed.stdout)
        self.assertEqual(value["query"]["query"], "--help")
        self.assertEqual(value["sessions"], [])
        self.assertEqual(value["count"], 0)
        self.assertFalse(Path(self.missing_db).exists())


class SearchOperationsTests(unittest.TestCase):
    SID = "c" * 32
    DEV = "d" * 32
    GHOST = "00000000-0000-4000-8000-000000000000"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.reg = str(self.base / "projects.toml")
        old = os.environ.get(projects.ENV_VAR)
        self._old_reg_env = old
        os.environ.pop(projects.ENV_VAR, None)

    def tearDown(self):
        if self._old_reg_env is None:
            os.environ.pop(projects.ENV_VAR, None)
        else:
            os.environ[projects.ENV_VAR] = self._old_reg_env
        self.temp.cleanup()

    def _search_ok(self, **kw):
        return {
            "query": {
                "project": None, "application": None, "resource": None,
                "device": None, "query": None, "from_ms": None,
                "to_ms": None, "range_semantics": None, "limit": 20,
            },
            "sessions": [], "count": 0,
        }

    def test_search_text_device_validation(self):
        self.assertEqual(
            desktop_projects._validate_search_text("query", "  hi  "), "hi")
        for bad in ("", "   ", None, 123, True):
            with self.assertRaises(desktop_projects.DesktopError):
                desktop_projects._validate_search_text("query", bad)
        with self.assertRaises(desktop_projects.DesktopError):
            desktop_projects._validate_search_text("query", "x" * 257)
        self.assertEqual(
            desktop_projects._validate_device_id(self.DEV.upper()), self.DEV)
        for bad in ("", "xyz", "d" * 31, "g" * 32, None, 123, True):
            with self.assertRaises(desktop_projects.DesktopError):
                desktop_projects._validate_device_id(bad)

    def test_search_activity_all_filters_argv(self):
        payload = self._search_ok()
        with patch.object(desktop_projects, "_fetch_current") as cur, \
                patch.object(desktop_projects, "_load_registry") as reg, \
                patch.object(desktop_projects, "_run_desktop_cli",
                             return_value=json.dumps(payload).encode()) as run:
            cur.side_effect = AssertionError("no compositor call")
            reg.side_effect = AssertionError("no registry call")
            out = desktop_projects.search_activity(
                self.GHOST, "kitty", "/tmp/x", self.DEV.upper(),
                "hello", 100, 200, 5, "/bin/x", None)
        self.assertEqual(out["count"], 0)
        argv = run.call_args[0][2]
        self.assertEqual(
            argv,
            ["search", "--project", self.GHOST, "--application", "kitty",
             "--resource", "/tmp/x", "--device", self.DEV,
             "--query", "hello", "--from", "100", "--to", "200",
             "--limit", "5"])

    def test_search_activity_empty_filters_recent(self):
        payload = self._search_ok()
        with patch.object(desktop_projects, "_run_desktop_cli",
                          return_value=json.dumps(payload).encode()) as run:
            out = desktop_projects.search_activity(
                None, None, None, None, None, None, None, None,
                "/bin/x", None)
        self.assertEqual(out["sessions"], [])
        self.assertEqual(out["count"], 0)
        argv = run.call_args[0][2]
        self.assertEqual(argv, ["search", "--limit", "20"])

    def test_search_activity_range_limits_boundaries(self):
        with patch.object(desktop_projects, "_run_desktop_cli",
                          return_value=json.dumps(self._search_ok()).encode()):
            out = desktop_projects.search_activity(
                None, None, None, None, None, 0, 0, 1, "/bin/x", None)
            self.assertEqual(out["count"], 0)
            full = desktop_projects.search_activity(
                None, None, None, None, None,
                0, desktop_projects.I64_MAX, 5, "/bin/x", None)
            self.assertEqual(full["count"], 0)
        for frm, to in ((5, None), (None, 5), ("", None), (None, ""),
                        ("", ""), ("  ", "  "), ("5", ""), ("", "5")):
            with self.assertRaises(desktop_projects.DesktopError):
                desktop_projects.search_activity(
                    None, None, None, None, None, frm, to, 5,
                    "/bin/x", None)
        with self.assertRaises(desktop_projects.DesktopError):
            desktop_projects.search_activity(
                None, None, None, None, None, 9, 3, 5, "/bin/x", None)
        for frm, to in ((desktop_projects.I64_MAX + 1,
                         desktop_projects.I64_MAX + 1),
                        (0, desktop_projects.I64_MAX + 1)):
            with self.assertRaises(desktop_projects.DesktopError):
                desktop_projects.search_activity(
                    None, None, None, None, None, frm, to, 5,
                    "/bin/x", None)
        for bad_limit in (0, 1001, "x", True):
            with self.assertRaises(desktop_projects.DesktopError):
                desktop_projects.search_activity(
                    None, None, None, None, None, None, None,
                    bad_limit, "/bin/x", None)
        for bad_text in ("", "   ", "x" * 257):
            with self.assertRaises(desktop_projects.DesktopError):
                desktop_projects.search_activity(
                    None, bad_text, None, None, None, None, None, 5,
                    "/bin/x", None)
        with self.assertRaises(desktop_projects.DesktopError):
            desktop_projects.search_activity(
                None, None, None, "bad", None, None, None, 5,
                "/bin/x", None)
        with self.assertRaises(desktop_projects.DesktopError):
            desktop_projects.search_activity(
                "not-a-uuid", None, None, None, None, None, None, 5,
                "/bin/x", None)

    def test_search_validates_object_shape(self):
        with patch.object(desktop_projects, "_run_desktop_cli",
                          return_value=b"[]"):
            with self.assertRaises(desktop_projects.DesktopError):
                desktop_projects.search_activity(
                    None, None, None, None, None, None, None, 5,
                    "/bin/x", None)
        bad = {"query": {}, "sessions": [], "count": 1}
        with patch.object(desktop_projects, "_run_desktop_cli",
                          return_value=json.dumps(bad).encode()):
            with self.assertRaises(desktop_projects.DesktopError):
                desktop_projects.search_activity(
                    None, None, None, None, None, None, None, 5,
                    "/bin/x", None)

    def test_search_compact_no_snapshots_or_events(self):
        hit = {
            "session_id": self.SID, "device_id": self.DEV,
            "project": {"id": self.GHOST}, "start_ms": 1, "end_ms": 2,
            "event_count": 1, "status": "closed",
            "matched_at_ms": 2,
            "applications": ["kitty"],
            "resources": [{"resource_key": "file:/tmp/x"}],
        }
        payload = {
            "query": {"project": None, "limit": 5},
            "sessions": [hit], "count": 1,
        }
        with patch.object(desktop_projects, "_run_desktop_cli",
                          return_value=json.dumps(payload).encode()):
            out = desktop_projects.search_activity(
                None, None, None, None, None, None, None, 5,
                "/bin/x", None)
        text = json.dumps(out)
        self.assertNotIn("snapshot", text)
        self.assertNotIn("events", text)
        self.assertEqual(out["sessions"][0]["session_id"], self.SID)
        # Compact newest-matching-observation time passes through.
        self.assertEqual(out["sessions"][0]["matched_at_ms"], 2)
        # A no-filter hit carries matched_at_ms None (session-recency order).
        recent = dict(hit, matched_at_ms=None)
        payload = {
            "query": {"project": None, "limit": 5},
            "sessions": [recent], "count": 1,
        }
        with patch.object(desktop_projects, "_run_desktop_cli",
                          return_value=json.dumps(payload).encode()):
            out = desktop_projects.search_activity(
                None, None, None, None, None, None, None, 5,
                "/bin/x", None)
        self.assertIsNone(out["sessions"][0]["matched_at_ms"])
        self.assertNotIn("snapshot", json.dumps(out))

    def test_get_session_no_events_default(self):
        payload = {"session": None, "resources": [],
                   "events_included": False}
        with patch.object(desktop_projects, "_fetch_current") as cur, \
                patch.object(desktop_projects, "_load_registry") as reg, \
                patch.object(desktop_projects, "_run_desktop_cli",
                             return_value=json.dumps(payload).encode()) as run:
            cur.side_effect = AssertionError("no compositor call")
            reg.side_effect = AssertionError("no registry call")
            out = desktop_projects.get_session(
                self.SID.upper(), None, False, None, "/bin/x", None)
        self.assertFalse(out["events_included"])
        self.assertNotIn("events", out)
        argv = run.call_args[0][2]
        self.assertEqual(
            argv, ["session-detail", "--session", self.SID,
                   "--resource-limit", "20"])

    def test_get_session_explicit_events(self):
        payload = {"session": {"session_id": self.SID}, "resources": [],
                   "events_included": True, "events": [{"id": 1}]}
        with patch.object(desktop_projects, "_run_desktop_cli",
                          return_value=json.dumps(payload).encode()) as run:
            out = desktop_projects.get_session(
                self.SID, 3, True, 4, "/bin/x", None)
        self.assertTrue(out["events_included"])
        self.assertEqual(out["events"], [{"id": 1}])
        argv = run.call_args[0][2]
        self.assertEqual(
            argv, ["session-detail", "--session", self.SID,
                   "--resource-limit", "3", "--include-events",
                   "--event-limit", "4"])

    def test_get_session_event_limit_requires_include(self):
        with self.assertRaises(desktop_projects.DesktopError) as ctx:
            desktop_projects.get_session(
                self.SID, None, False, 5, "/bin/x", None)
        self.assertIn("include-events", str(ctx.exception))
        with self.assertRaises(desktop_projects.DesktopError):
            desktop_projects.get_session("bad", None, False, None,
                                         "/bin/x", None)
        with self.assertRaises(desktop_projects.DesktopError):
            desktop_projects.get_session(
                self.SID, 0, False, None, "/bin/x", None)
        with self.assertRaises(desktop_projects.DesktopError):
            desktop_projects.get_session(
                self.SID, None, True, 0, "/bin/x", None)
        with self.assertRaises(desktop_projects.DesktopError):
            desktop_projects.get_session(
                self.SID, None, "yes", None, "/bin/x", None)
        # events_included mismatch fails closed.
        mismatch = {"session": None, "resources": [],
                    "events_included": True, "events": []}
        with patch.object(desktop_projects, "_run_desktop_cli",
                          return_value=json.dumps(mismatch).encode()):
            with self.assertRaises(desktop_projects.DesktopError):
                desktop_projects.get_session(
                    self.SID, None, False, None, "/bin/x", None)
        leak = {"session": None, "resources": [],
                "events_included": False, "events": []}
        with patch.object(desktop_projects, "_run_desktop_cli",
                          return_value=json.dumps(leak).encode()):
            with self.assertRaises(desktop_projects.DesktopError):
                desktop_projects.get_session(
                    self.SID, None, False, None, "/bin/x", None)

    def test_current_context_aliases_current_project(self):
        entry = projects.create_project({"name": "Ctx"}, self.reg)["project"]
        report = {"id": entry["id"], "name": "Ctx", "matched_by": "cwd"}
        canned = {"available": True, "source": "hyprland",
                  "observed_at_ms": 1, "focused_window": None,
                  "workspace": None, "resource": None, "project": report}
        with patch.object(desktop_projects, "_fetch_current",
                          return_value=canned):
            a = desktop_projects.current_project(self.reg, "/bin/x", None)
            b = desktop_projects.current_context(self.reg, "/bin/x", None)
        self.assertEqual(a, b)
        self.assertIn("context", b)

    def test_project_activity_explicit_unknown_still_queries(self):
        payload = {"query": {"project": self.GHOST, "limit": 5},
                   "sessions": [{"session_id": self.SID}], "count": 1}
        with patch.object(desktop_projects, "_fetch_current") as cur, \
                patch.object(desktop_projects, "_fetch_search",
                             return_value=payload) as fetch:
            cur.side_effect = AssertionError("explicit must not fetch current")
            out = desktop_projects.project_activity(
                self.GHOST, "kitty", None, None, None, None, None, 5,
                self.reg, "/bin/x", None)
        fetch.assert_called_once()
        args = fetch.call_args[0]
        self.assertEqual(args[2], self.GHOST)
        self.assertEqual(args[3], "kitty")
        self.assertEqual(out["requested_project_id"], self.GHOST)
        self.assertIsNone(out["registry"])
        self.assertEqual(out["sessions"], [{"session_id": self.SID}])
        self.assertIn("unknown or deleted", out["reason"])

    def test_project_activity_defaults_to_current(self):
        created = projects.create_project({"name": "P"}, self.reg)
        pid = created["project"]["id"]
        report = {"id": pid, "name": "P", "matched_by": "cwd"}
        canned = {"available": True, "source": "hyprland",
                  "observed_at_ms": 1, "focused_window": None,
                  "workspace": None, "resource": None, "project": report}
        payload = {"query": {"project": pid, "limit": 7},
                   "sessions": [], "count": 0}
        with patch.object(desktop_projects, "_fetch_current",
                          return_value=canned):
            with patch.object(desktop_projects, "_fetch_search",
                              return_value=payload) as fetch:
                out = desktop_projects.project_activity(
                    None, None, None, None, "hello", 10, 20, 7,
                    self.reg, "/bin/x", None)
        fetch.assert_called_once()
        args = fetch.call_args[0]
        self.assertEqual(args[2], pid)
        self.assertEqual(args[6], "hello")
        self.assertEqual(args[7], 10)
        self.assertEqual(args[8], 20)
        self.assertEqual(args[9], 7)
        self.assertEqual(out["project"]["id"], pid)
        self.assertIsNone(out["requested_project_id"])
        self.assertEqual(out["query"]["project"], pid)

    def test_project_activity_no_current_needs_no_db_query(self):
        canned = {"available": True, "source": "hyprland",
                  "observed_at_ms": 1, "focused_window": None,
                  "workspace": None, "resource": None, "project": None}
        with patch.object(desktop_projects, "_fetch_current",
                          return_value=canned):
            with patch.object(desktop_projects, "_fetch_search") as fetch:
                fetch.side_effect = AssertionError("no DB query")
                out = desktop_projects.project_activity(
                    None, None, None, None, None, None, None, 5,
                    self.reg, "/bin/x", None)
        self.assertEqual(out["sessions"], [])
        self.assertEqual(out["count"], 0)
        self.assertIn("no current", out["reason"].lower())

    def test_cli_routing_and_rejection_gates(self):
        ok_search = run_cli(
            ["--desktop-bin", "/nonexistent-qs-bin-xyz",
             "search-activity", "--application", "kitty",
             "--resource", "/tmp/x", "--device", self.DEV,
             "--query", "hi", "--from", "1", "--to", "2",
             "--limit", "5"])
        self.assertNotEqual(ok_search.returncode, 0)
        self.assertIn("not found", ok_search.stderr)
        self.assertNotIn("Traceback", ok_search.stderr)
        bad_text = run_cli(
            ["--desktop-bin", "/nonexistent-qs-bin-xyz",
             "search-activity", "--application", ""])
        self.assertNotEqual(bad_text.returncode, 0)
        self.assertIn("nonempty", bad_text.stderr)
        bad_device = run_cli(
            ["--desktop-bin", "/nonexistent-qs-bin-xyz",
             "search-activity", "--device", "bad"])
        self.assertNotEqual(bad_device.returncode, 0)
        self.assertIn("32 hex", bad_device.stderr)
        unpaired = run_cli(
            ["--desktop-bin", "/nonexistent-qs-bin-xyz",
             "search-activity", "--from", "5"])
        self.assertNotEqual(unpaired.returncode, 0)
        self.assertIn("together", unpaired.stderr)
        missing_session = run_cli(
            ["--desktop-bin", "/nonexistent-qs-bin-xyz", "get-session"])
        self.assertNotEqual(missing_session.returncode, 0)
        self.assertIn("--session", missing_session.stderr)
        event_without_include = run_cli(
            ["--desktop-bin", "/nonexistent-qs-bin-xyz",
             "get-session", "--session", self.SID, "--event-limit", "5"])
        self.assertNotEqual(event_without_include.returncode, 0)
        self.assertIn("include-events", event_without_include.stderr)
        legacy_rejects = run_cli(
            ["--desktop-bin", "/nonexistent-qs-bin-xyz",
             "recent-activity", "--application", "x"])
        self.assertNotEqual(legacy_rejects.returncode, 0)
        self.assertIn("does not accept", legacy_rejects.stderr)
        context_rejects = run_cli(
            ["current-context", "--limit", "5"])
        self.assertNotEqual(context_rejects.returncode, 0)
        self.assertIn("does not accept", context_rejects.stderr)
        for completed in (bad_text, bad_device, unpaired, missing_session,
                          event_without_include, legacy_rejects,
                          context_rejects):
            self.assertNotIn("Traceback", completed.stderr)
            self.assertEqual(completed.stdout, "")

    def test_cli_equals_form_preserves_leading_hyphen_text(self):
        # Pi->Python sends arbitrary text as one `--opt=<value>` item so a
        # leading-hyphen value is data, never a flag. Python->Rust keeps
        # separate items: the Rust parser consumes the token following a
        # filter flag explicitly, so it also arrives as data.
        payload = self._search_ok()
        with patch.object(desktop_projects, "_run_desktop_cli",
                          return_value=json.dumps(payload).encode()) as run:
            buf = io.StringIO()
            with unittest.mock.patch("sys.stdout", buf):
                rc = desktop_projects.main(
                    ["--desktop-bin", "/bin/x", "search-activity",
                     "--application=-draft", "--query=--help"])
        self.assertEqual(rc, 0)
        argv = run.call_args[0][2]
        self.assertEqual(
            argv, ["search", "--application", "-draft",
                   "--query", "--help", "--limit", "20"])
        out = json.loads(buf.getvalue())
        self.assertEqual(out["count"], 0)

    def test_cli_explicit_empty_from_to_rejects(self):
        # main() reports validation failures as returncode 1 (single-line
        # stderr, no traceback); the Rust child must never spawn.
        for argv in (["search-activity", "--from", "", "--to", "5"],
                     ["search-activity", "--from", "5", "--to", ""],
                     ["search-activity", "--from=", "--to="],
                     ["project-activity", "--from", "  ", "--to", "5"]):
            with patch.object(desktop_projects, "_run_desktop_cli") as run:
                run.side_effect = AssertionError("must reject before spawn")
                rc = desktop_projects.main(
                    ["--desktop-bin", "/bin/x", *argv])
            self.assertEqual(rc, 1, argv)
            run.assert_not_called()


class PolicyCoherenceTests(unittest.TestCase):
    """Policy text and the tool_call gate agree on the desktop exception.

    Registration tests only prove tools exist; these prove every scope
    allowlist (palette/journal/project) explicitly permits the nine
    read-only desktop tools without weakening mutation restrictions.
    """

    DESKTOP_NINE = ("desktop_current_context", "desktop_project_todos",
                     "desktop_project_logseq_context", "desktop_project_activity",
                     "desktop_current_session", "desktop_search_activity",
                     "desktop_get_session", "desktop_resume_plan",
                     "session_search")

    def test_system_scope_bullets_permit_desktop_exception(self):
        system = (ROOT / ".pi" / "SYSTEM.md").read_text(encoding="utf-8")
        for name in self.DESKTOP_NINE:
            self.assertIn(name, system)
        # Palette and project bullets name the exception explicitly.
        self.assertGreaterEqual(
            system.count("nine read-only desktop tools"), 2)
        # Journal bullet carries the same exception.
        self.assertIn("nine-tool desktop read-only exception", system)
        # The exception never permits writes or skips approvals.
        self.assertIn("desktop exception never permits writes", system)
        # Mutation restrictions are unchanged.
        self.assertIn("requires UI confirmation", system)
        self.assertIn("Use only `logseq_project_read`", system)
        self.assertIn("stale revision is an error", system)

    def test_skill_inventories_permit_desktop_exception(self):
        skill = (ROOT / ".pi" / "skills" / "logseq-graph" / "SKILL.md"
                 ).read_text(encoding="utf-8")
        for name in self.DESKTOP_NINE:
            self.assertIn(name, skill)
        self.assertIn("exception in every scope", skill)
        self.assertIn("same list as above", skill)
        self.assertIn("read-only and changes no", skill)

    def test_gate_returns_early_for_exactly_nine(self):
        allowlist = EXTENSION.split("const DESKTOP_READ_TOOLS")[1].split("]")[0]
        for name in self.DESKTOP_NINE:
            self.assertIn(f'"{name}"', allowlist)
        self.assertNotIn("desktop_current_project", allowlist)
        self.assertIn(
            "if ((DESKTOP_READ_TOOLS as string[]).includes(event.toolName)) return;",
            EXTENSION)
        # Scoped gates still deny everything else.
        self.assertIn("Journal mode permits only the constrained journal tools",
                      EXTENSION)
        self.assertIn("Project mode permits only the constrained project tools",
                      EXTENSION)

    def test_system_qualifies_edit_claims(self):
        system = (ROOT / ".pi" / "SYSTEM.md").read_text(encoding="utf-8")
        self.assertIn("not file edits", system)
        self.assertIn("matched_at_ms", system)
        self.assertIn("edits are not recorded", system)

    @unittest.skipUnless(__import__("shutil").which("bun"), "bun required")
    def test_policy_gate_desktop_exception_every_scope(self):
        """Executable gate check: 9 desktop tools pass in palette/project/
        journal scopes while scoped denials still hold (policy text matches
        runtime behavior, not just registration)."""
        import shutil as _shutil
        import tempfile as _tmp
        bun = _shutil.which("bun")
        assert bun is not None
        source = "\n".join(
            line for line in EXTENSION.splitlines()
            if not line.startswith("import "))
        source = source.replace(
            "export default function desktopAgent", "function desktopAgent")
        script = r'''
import { EventEmitter } from "node:events";
import { dirname as pathDirname, join, isAbsolute, resolve, normalize, relative } from "node:path";
const dirname = pathDirname;
const fileURLToPath = () => "/repo/.pi/extensions/desktop-agent.ts";
const existsSync = () => true;
const lstatSync = () => { throw new Error("unused"); };
const readFileSync = () => { throw new Error("unused"); };
const realpathSync: any = { native: (value: string) => value };
const SessionManager: any = {};
const Type: any = { Object: (v: any) => v, String: () => ({}), Optional: (v: any) => v, Integer: () => ({}), Boolean: () => ({}), Any: () => ({}) };
const tools: any = {}, hooks: any = {};
function spawn(command: string, args: string[]) {
  const child: any = new EventEmitter();
  child.stdout = new EventEmitter(); child.stderr = new EventEmitter();
  child.stdin = new EventEmitter(); child.stdin.write=()=>{}; child.stdin.end=()=>{}; child.kill=()=>{};
  queueMicrotask(() => {
    (child as any).emit("spawn");
    const b = Buffer.from(JSON.stringify({ ok: true }));
    child.stdout.emit("data", b); child.emit("close", 0, null);
  });
  return child;
}
''' + source + r'''
const pi: any = { on(n: string, cb: any) { hooks[n] = cb; },
  registerTool(t: any) { tools[t.name] = t; }, registerCommand() {} };
const assert = (v: any, m: string) => { if (!v) throw new Error(m); };
const ctx: any = { cwd: "/work", hasUI: false, ui: {} };
// Palette scope: all nine pass.
process.env.QS_PROJECT_PATH = ""; process.env.QS_JOURNAL_MODE = "";
desktopAgent(pi);
const gate = hooks.tool_call;
const SEVEN = ["desktop_current_context", "desktop_project_todos", "desktop_project_logseq_context", "desktop_project_activity", "desktop_current_session", "desktop_search_activity", "desktop_get_session", "desktop_resume_plan", "session_search"];
for (const name of SEVEN) {
  const r = await gate({ toolName: name, input: {} }, ctx);
  assert(!r?.block, name + " blocked in palette scope");
}
// Project scope: all nine pass, generic search still denied.
process.env.QS_PROJECT_PATH = "pages/Work.md"; process.env.QS_JOURNAL_MODE = "";
for (const name of SEVEN) {
  const r = await gate({ toolName: name, input: {} }, ctx);
  assert(!r?.block, name + " blocked in project scope");
}
const projDenied = await gate({ toolName: "logseq_search", input: { query: "x" } }, ctx);
assert(projDenied?.block === true, "project scope no longer denies generic tools");
// Journal scope: all nine pass, generic search still denied.
process.env.QS_PROJECT_PATH = ""; process.env.QS_JOURNAL_MODE = "1";
for (const name of SEVEN) {
  const r = await gate({ toolName: name, input: {} }, ctx);
  assert(!r?.block, name + " blocked in journal scope");
}
const jrDenied = await gate({ toolName: "logseq_search", input: { query: "x" } }, ctx);
assert(jrDenied?.block === true, "journal scope no longer denies generic tools");
console.log(JSON.stringify({ ok: true }));
'''
        with _tmp.NamedTemporaryFile("w", suffix=".ts", delete=False,
                                     encoding="utf-8") as handle:
            handle.write(script)
            name = handle.name
        completed = subprocess.run([bun, name], text=True, capture_output=True,
                                   timeout=15)
        self.assertEqual(completed.returncode, 0,
                         completed.stderr + completed.stdout)
        self.assertEqual(
            json.loads(completed.stdout.splitlines()[-1]), {"ok": True})


class ExtensionContractTests(unittest.TestCase):
    def test_registers_nine_coherent_desktop_tools(self):
        for name in ("desktop_current_context", "desktop_project_todos",
                     "desktop_project_logseq_context", "desktop_project_activity",
                     "desktop_current_session",
                     "desktop_search_activity",
                     "desktop_get_session", "desktop_resume_plan",
                     "session_search"):
            self.assertIn(f'name: "{name}"', EXTENSION)
        # Overlapping low-level history tools are removed from Pi.
        for name in ("desktop_current_project", "desktop_project_resources",
                     "desktop_work_sessions", "desktop_session_resources",
                     "desktop_session_events"):
            self.assertNotIn(f'name: "{name}"', EXTENSION)
        # Read allowlist covers exactly the nine coherent tools.
        allowlist = EXTENSION.split("const DESKTOP_READ_TOOLS")[1].split("]")[0]
        for name in ("desktop_current_context", "desktop_project_todos",
                     "desktop_project_logseq_context", "desktop_project_activity",
                     "desktop_current_session", "desktop_search_activity",
                     "desktop_get_session", "desktop_resume_plan",
                     "session_search"):
            self.assertIn(name, allowlist)
        for name in ("desktop_current_project", "desktop_project_resources",
                     "desktop_work_sessions", "desktop_session_resources",
                     "desktop_session_events"):
            self.assertNotIn(name, allowlist)

    def test_helper_safety_style(self):
        self.assertIn("const DESKTOP_PROJECTS_HELPER", EXTENSION)
        self.assertIn("desktop_projects.py", EXTENSION)
        self.assertIn("DESKTOP_HELPER_TIMEOUT", EXTENSION)
        self.assertIn("DESKTOP_MAX_OUTPUT", EXTENSION)
        self.assertIn("desktop helper timed out", EXTENSION)
        self.assertIn("desktop helper output exceeded", EXTENSION)
        self.assertIn("desktop helper returned invalid JSON", EXTENSION)
        self.assertIn("shell: false", EXTENSION)
        self.assertIn("operation aborted", EXTENSION)
        # Process-group cancellation: group leader + TERM-then-KILL.
        self.assertIn("killDesktopGroup", EXTENSION)
        self.assertIn("detached: process.platform", EXTENSION)
        self.assertIn('killDesktopGroup(child, "SIGKILL")', EXTENSION)
        self.assertIn("process.kill(-pid", EXTENSION)
        # Escalation is independent of promise settlement: a TERM-resistant
        # grandchild must not survive just because the direct child exited.
        self.assertIn("beginTermination", EXTENSION)
        self.assertIn("terminationBegun", EXTENSION)
        # Python keeps Rust in its group (no new session) for nested cleanup.
        source = (ROOT / "scripts" / "desktop_projects.py").read_text(
            encoding="utf-8")
        self.assertIn("start_new_session=False", source)
        self.assertIn("selectors.DefaultSelector", source)
        self.assertIn("REQUEST_TIMEOUT", source)

    def test_fresh_tools_not_blocked_and_scoped_helpers_untouched(self):
        # Fresh desktop read tools are explicitly retained in scoped modes.
        self.assertIn("desktop_current_context", EXTENSION)
        self.assertIn("DESKTOP_READ_TOOLS", EXTENSION)
        # Existing scoped helpers stay pinned to QS_PROJECT_PATH.
        self.assertIn("return { path: process.env.QS_PROJECT_PATH, ...fields }",
                      EXTENSION)
        self.assertIn('projectHelper(ctx, "page"', EXTENSION)
        # Desktop helper never touches the scoped project path (the
        # resume pinned-page resolver is a separate function and may).
        start = EXTENSION.index("function desktopProjectsHelper")
        end = EXTENSION.index("\n}\n\nfunction desktopResumeHelper", start)
        desktop_fn = EXTENSION[start:end]
        self.assertNotIn("QS_PROJECT_PATH", desktop_fn)
        # Mutation/session identity wording unchanged.
        self.assertIn("Project mode permits only the constrained project tools",
                      EXTENSION)
        self.assertIn("project session scope was not established",
                      EXTENSION)

    def test_desktop_schemas_use_optional_project_limit(self):
        self.assertIn("desktopProjectTodosSchema", EXTENSION)
        self.assertIn("desktopProjectActivitySchema", EXTENSION)
        self.assertIn("desktopSearchActivitySchema", EXTENSION)
        self.assertIn("desktopGetSessionSchema", EXTENSION)
        self.assertIn("desktopCurrentSessionSchema", EXTENSION)
        self.assertIn("desktopCurrentContextSchema", EXTENSION)
        # Project override is an optional UUID string; limit is bounded 1..1000.
        self.assertIn("project must be a UUID", EXTENSION)
        self.assertIn("limit must be 1..1000", EXTENSION)
        # Session identity is a strict 32-hex string; range is paired.
        self.assertIn("session must be 32 hex", EXTENSION)
        self.assertIn("fromMs and toMs must be given together", EXTENSION)
        self.assertIn("device must be 32 hex", EXTENSION)
        self.assertIn("eventLimit requires includeEvents", EXTENSION)
        self.assertIn("desktopSearchActivitySchema", EXTENSION)
        # Helper union covers the coherent read-only commands.
        for command in ("current-context", "search-activity",
                        "get-session", "project-activity",
                        "current-session", "todos", "logseq-context"):
            self.assertIn(f'"{command}"', EXTENSION)
        # Legacy low-level history commands are gone from Pi (Python keeps them).
        for command in ("recent-activity", "last-activity",
                        '"resources"', '"sessions"', '"last-session"',
                        '"session-resources"', '"session-events"',
                        '"current-project"'):
            self.assertNotIn(command, EXTENSION)
        # Coherent tools are explicit that Pi agent sessions differ.
        self.assertIn("Unrelated to Pi agent sessions", EXTENSION)
        # Timezone is dynamic via Intl; descriptions carry the local zone and
        # start-inclusive/end-exclusive semantics without natural-language ranges.
        self.assertIn("Intl.DateTimeFormat().resolvedOptions().timeZone", EXTENSION)
        self.assertIn("DESKTOP_LOCAL_TZ", EXTENSION)
        self.assertIn("start-inclusive/end-exclusive", EXTENSION)
        self.assertIn("never pass natural-language ranges", EXTENSION)
        # Contract docstring distinguishes work sessions from Pi sessions.
        source = (ROOT / "scripts" / "desktop_projects.py").read_text(
            encoding="utf-8")
        self.assertIn("current-session", source)
        self.assertIn("session-resources", source)
        self.assertIn("search-activity", source)
        self.assertIn("session-detail", source)
        self.assertIn("Deterministic work sessions vs Pi agent sessions", source)

    def test_protected_paths_cover_new_helper(self):
        self.assertIn("desktop_projects.py", EXTENSION)
        # Trusted registry import must be gated like the other helpers.
        # Each helper is asserted individually: the protected list is one
        # long union, so no adjacent-order substring is assumed.
        for name in ('"desktop_projects.py"', '"desktop_resume.py"',
                     '"projects.py"', '"project_folder.py"'):
            self.assertIn(name, EXTENSION)
        for name in ("desktop_projects", "desktop_resume", "projects",
                     "project_folder"):
            self.assertIn(name, EXTENSION)

    @unittest.skipUnless(__import__("shutil").which("bun"), "bun required")
    def test_coherent_tools_validate_and_route(self):
        """Bun harness: coherent schemas validate before spawn and route.

        Covers desktop_current_context (no params), desktop_current_session
        (no params), desktop_search_activity / desktop_project_activity with
        all structured filters and paired-range gates, and
        desktop_get_session 32-hex/resourceLimit/includeEvents/eventLimit
        gates. Spawn is stubbed to capture the Python argv; validation
        failures must throw before any spawn, and all nine read tools stay
        unblocked in scoped modes. Dynamic timezone wording is embedded in
        search descriptions.
        """
        import shutil as _shutil
        import tempfile as _tmp
        bun = _shutil.which("bun")
        assert bun is not None
        source = "\n".join(
            line for line in EXTENSION.splitlines()
            if not line.startswith("import "))
        source = source.replace(
            "export default function desktopAgent", "function desktopAgent")
        script = r'''
import { EventEmitter } from "node:events";
import { dirname as pathDirname, join, isAbsolute, resolve, normalize, relative } from "node:path";
const dirname = pathDirname;
const fileURLToPath = () => "/repo/.pi/extensions/desktop-agent.ts";
const existsSync = () => true;
const lstatSync = () => { throw new Error("unused"); };
const readFileSync = () => { throw new Error("unused"); };
const realpathSync: any = { native: (value: string) => value };
const SessionManager: any = {};
const Type: any = { Object: (v: any) => v, String: () => ({}), Optional: (v: any) => v, Integer: () => ({}), Boolean: () => ({}), Any: () => ({}) };
const tools: any = {}, hooks: any = {};
const calls: any[] = [];
function spawn(command: string, args: string[]) {
  const child: any = new EventEmitter();
  child.stdout = new EventEmitter(); child.stderr = new EventEmitter();
  child.stdin = new EventEmitter(); child.stdin.write=()=>{}; child.stdin.end=()=>{}; child.kill=()=>{};
  calls.push({ command, args });
  queueMicrotask(() => {
    (child as any).emit("spawn");
    const b = Buffer.from(JSON.stringify({ ok: true }));
    child.stdout.emit("data", b); child.emit("close", 0, null);
  });
  return child;
}
''' + source + r'''
const pi: any = { on(n: string, cb: any) { hooks[n] = cb; },
  registerTool(t: any) { tools[t.name] = t; }, registerCommand() {} };
process.env.QS_PROJECT_PATH = ""; process.env.QS_JOURNAL_MODE = "";
desktopAgent(pi);
const assert = (v: any, m: string) => { if (!v) throw new Error(m); };
const ctx: any = { cwd: "/work", hasUI: false, ui: {} };
const names = Object.keys(tools).join(",");
assert(names === "logseq_search,logseq_todos,logseq_append_journal,create_project,create_logseq_page,logseq_agenda_list,logseq_agenda_add,zotero_search,zotero_item,zotero_read_pdf,zotero_collections,zotero_prepare,zotero_apply,desktop_current_context,desktop_project_todos,desktop_project_logseq_context,desktop_project_activity,desktop_current_session,desktop_search_activity,desktop_get_session,desktop_resume_plan,session_search", "nine tools wrong: " + names);
// All nine desktop tools stay unblocked in project + journal scopes.
process.env.QS_PROJECT_PATH = "pages/Work.md"; process.env.QS_JOURNAL_MODE = "";
for (const name of ["desktop_current_context", "desktop_project_todos", "desktop_project_logseq_context", "desktop_project_activity", "desktop_current_session", "desktop_search_activity", "desktop_get_session", "desktop_resume_plan", "session_search"]) {
  const r = await hooks.tool_call({ toolName: name, input: {} }, ctx);
  assert(!r?.block, name + " blocked in project scope");
}
process.env.QS_PROJECT_PATH = ""; process.env.QS_JOURNAL_MODE = "1";
for (const name of ["desktop_current_context", "desktop_search_activity", "desktop_get_session", "desktop_resume_plan", "session_search"]) {
  const r = await hooks.tool_call({ toolName: name, input: {} }, ctx);
  assert(!r?.block, name + " blocked in journal mode");
}
process.env.QS_PROJECT_PATH = ""; process.env.QS_JOURNAL_MODE = "";
const SID = "a".repeat(32);
const DEV = "b".repeat(32);
const UUID = "00000000-0000-4000-8000-000000000000";
// current-context and current-session take no params and route verbatim.
calls.length = 0;
await tools.desktop_current_context.execute("id", {}, undefined, undefined, ctx);
assert(calls.at(-1).args[1] === "current-context" && calls.at(-1).args.length === 2, "current-context argv wrong: " + JSON.stringify(calls.at(-1).args));
calls.length = 0;
await tools.desktop_current_session.execute("id", {}, undefined, undefined, ctx);
assert(calls.at(-1).args[1] === "current-session" && calls.at(-1).args.length === 2, "current-session argv wrong: " + JSON.stringify(calls.at(-1).args));
// search with all filters routes every flag. Free text travels as one
// `--opt=<value>` item so leading-hyphen values stay data for argparse.
calls.length = 0;
await tools.desktop_search_activity.execute("id", { project: UUID, application: "kitty", resource: "/tmp/x", device: DEV.toUpperCase(), query: "hello", fromMs: 100, toMs: 200, limit: 5 }, undefined, undefined, ctx);
assert(JSON.stringify(calls.at(-1).args.slice(1)) === JSON.stringify(["search-activity", "--project", UUID, "--application=kitty", "--resource=/tmp/x", "--device", DEV, "--query=hello", "--from", "100", "--to", "200", "--limit", "5"]), "search argv wrong: " + JSON.stringify(calls.at(-1).args));
// leading-hyphen text is one argv item, never a premature flag.
calls.length = 0;
await tools.desktop_search_activity.execute("id", { application: "-draft", query: "--help" }, undefined, undefined, ctx);
assert(JSON.stringify(calls.at(-1).args.slice(1)) === JSON.stringify(["search-activity", "--application=-draft", "--query=--help"]), "hyphen argv wrong: " + JSON.stringify(calls.at(-1).args));
// bare search lists recent sessions.
calls.length = 0;
await tools.desktop_search_activity.execute("id", {}, undefined, undefined, ctx);
assert(JSON.stringify(calls.at(-1).args.slice(1)) === JSON.stringify(["search-activity"]), "bare search argv wrong: " + JSON.stringify(calls.at(-1).args));
// project-activity threads the same filters through project-activity.
calls.length = 0;
await tools.desktop_project_activity.execute("id", { application: "kitty", limit: 7 }, undefined, undefined, ctx);
assert(JSON.stringify(calls.at(-1).args.slice(1)) === JSON.stringify(["project-activity", "--application=kitty", "--limit", "7"]), "project-activity argv wrong: " + JSON.stringify(calls.at(-1).args));
// search validation before spawn: unpaired range / bad range / bad limit /
// bad UUID / empty text / bad device / explicit empty timestamps /
// unsafe integers never spawn.
for (const bad of [{ fromMs: 5 }, { toMs: 5 }, { fromMs: 9, toMs: 3 }, { fromMs: -1, toMs: 2 }, { limit: 0 }, { limit: 1001 }, { project: "nope" }, { application: "" }, { application: "  " }, { query: "x".repeat(257) }, { device: "xyz" }, { device: "" }, { fromMs: "", toMs: 5 }, { fromMs: 5, toMs: "" }, { fromMs: "", toMs: "" }, { fromMs: "  ", toMs: 5 }, { fromMs: Number.MAX_SAFE_INTEGER + 1, toMs: Number.MAX_SAFE_INTEGER + 2 }, { fromMs: 9007199254740993, toMs: 9007199254740993 }, { fromMs: 1.5, toMs: 2 }]) {
  for (const tool of ["desktop_search_activity", "desktop_project_activity"]) {
    const before = calls.length;
    try { await (tools as any)[tool].execute("id", bad as any, undefined, undefined, ctx); throw new Error("bad search succeeded: " + tool + JSON.stringify(bad)); }
    catch (e) { assert(!String(e).includes("bad search succeeded"), String(e)); }
    assert(calls.length === before, "invalid search spawned: " + tool + JSON.stringify(bad));
  }
}
// get-session routes resourceLimit/includeEvents/eventLimit; events default off.
calls.length = 0;
await tools.desktop_get_session.execute("id", { session: SID.toUpperCase() }, undefined, undefined, ctx);
assert(JSON.stringify(calls.at(-1).args.slice(1)) === JSON.stringify(["get-session", "--session", SID]), "get-session argv wrong: " + JSON.stringify(calls.at(-1).args));
calls.length = 0;
await tools.desktop_get_session.execute("id", { session: SID, resourceLimit: 3, includeEvents: true, eventLimit: 4 }, undefined, undefined, ctx);
assert(JSON.stringify(calls.at(-1).args.slice(1)) === JSON.stringify(["get-session", "--session", SID, "--resource-limit", "3", "--include-events", "--event-limit", "4"]), "get-session events argv wrong: " + JSON.stringify(calls.at(-1).args));
for (const bad of [{}, { session: "bad" }, { session: SID, resourceLimit: 0 }, { session: SID, eventLimit: 5 }, { session: SID, includeEvents: "yes" as any }, { session: SID, includeEvents: true, eventLimit: 0 }]) {
  const before = calls.length;
  try { await tools.desktop_get_session.execute("id", bad as any, undefined, undefined, ctx); throw new Error("bad get-session succeeded: " + JSON.stringify(bad)); }
  catch (e) { assert(!String(e).includes("bad get-session succeeded"), String(e)); }
  assert(calls.length === before, "invalid get-session spawned: " + JSON.stringify(bad));
}
console.log(JSON.stringify({ ok: true }));
'''
        with _tmp.NamedTemporaryFile("w", suffix=".ts", delete=False,
                                     encoding="utf-8") as handle:
            handle.write(script)
            name = handle.name
        completed = subprocess.run([bun, name], text=True, capture_output=True,
                                   timeout=15)
        self.assertEqual(completed.returncode, 0,
                         completed.stderr + completed.stdout)
        self.assertEqual(
            json.loads(completed.stdout.splitlines()[-1]), {"ok": True})

    @unittest.skipUnless(__import__("shutil").which("bun"), "bun required")
    def test_projects_py_gating_preserves_scopes(self):
        import shutil as _shutil
        import tempfile as _tmp
        bun = _shutil.which("bun")
        assert bun is not None
        source = "\n".join(
            line for line in EXTENSION.splitlines()
            if not line.startswith("import "))
        source = source.replace(
            "export default function desktopAgent", "function desktopAgent")
        script = r'''
import { EventEmitter } from "node:events";
import { dirname as pathDirname, join, isAbsolute, resolve, normalize, relative } from "node:path";
const dirname = pathDirname;
const fileURLToPath = () => "/repo/.pi/extensions/desktop-agent.ts";
const existsSync = () => true;
const lstatSync = () => { throw new Error("unused"); };
const readFileSync = () => { throw new Error("unused"); };
const realpathSync: any = { native: (value: string) => value };
const SessionManager: any = {};
const Type: any = { Object: (v: any) => v, String: () => ({}), Optional: (v: any) => v, Integer: () => ({}), Boolean: () => ({}), Any: () => ({}) };
const tools: any = {}, hooks: any = {};
const calls: any[] = [];
function spawn(command: string, args: string[]) {
  const child: any = new EventEmitter();
  child.stdout = new EventEmitter(); child.stderr = new EventEmitter();
  child.stdin = new EventEmitter(); child.stdin.write=()=>{}; child.stdin.end=()=>{}; child.kill=()=>{};
  queueMicrotask(() => {
    (child as any).emit("spawn");
    const b = Buffer.from(JSON.stringify({ ok: true }));
    child.stdout.emit("data", b); child.emit("close", 0, null);
  });
  return child;
}
''' + source + r'''
const pi: any = { on(n: string, cb: any) { hooks[n] = cb; },
  registerTool(t: any) { tools[t.name] = t; }, registerCommand() {} };
process.env.QS_PROJECT_PATH = ""; process.env.QS_JOURNAL_MODE = "";
desktopAgent(pi);
const assert = (v: any, m: string) => { if (!v) throw new Error(m); };
const ctx: any = { cwd: "/work", hasUI: false, ui: {} };
const gate = hooks.tool_call;
// Direct helper paths are protected for read/write/edit traversal.
for (const tool of ["read", "write", "edit"]) {
  for (const target of ["scripts/projects.py", "scripts/desktop_projects.py"]) {
    const r = await gate({ toolName: tool, input: { path: target } }, ctx);
    assert(r?.block === true, tool + " " + target + " was not blocked");
  }
}
// Shell references to either helper are blocked.
for (const target of ["scripts/projects.py", "scripts/desktop_projects.py"]) {
  const r = await gate({ toolName: "bash", input: { command: "cat " + target } }, ctx);
  assert(r?.block === true, "shell " + target + " was not blocked");
}
// Fresh desktop tools stay unblocked in every scope; existing scopes intact.
for (const scoped of ["pages/Work.md", ""]) {
  process.env.QS_PROJECT_PATH = scoped;
  process.env.QS_JOURNAL_MODE = "";
  const r = await gate({ toolName: "desktop_current_context", input: {} }, ctx);
  assert(!r?.block, "desktop tool blocked in project scope " + JSON.stringify(scoped));
}
process.env.QS_PROJECT_PATH = ""; process.env.QS_JOURNAL_MODE = "1";
const jr = await gate({ toolName: "desktop_project_todos", input: {} }, ctx);
assert(!jr?.block, "desktop tool blocked in journal mode");
process.env.QS_PROJECT_PATH = "pages/Work.md"; process.env.QS_JOURNAL_MODE = "";
const shellBlocked = await gate({ toolName: "bash", input: { command: "echo hi" } }, ctx);
assert(shellBlocked?.block === true, "scoped shell was not blocked");
console.log(JSON.stringify({ ok: true }));
'''
        with _tmp.NamedTemporaryFile("w", suffix=".ts", delete=False,
                                     encoding="utf-8") as handle:
            handle.write(script)
            name = handle.name
        completed = subprocess.run([bun, name], text=True, capture_output=True,
                                   timeout=15)
        self.assertEqual(completed.returncode, 0,
                         completed.stderr + completed.stdout)
        self.assertEqual(
            json.loads(completed.stdout.splitlines()[-1]), {"ok": True})

    @unittest.skipUnless(__import__("shutil").which("bun"), "bun required")
    def test_group_kill_escalation_cleans_term_resistant_grandchild(self):
        """Group TERM then SIGKILL must clean a TERM-ignoring grandchild.

        Fixture mirrors the helper topology: a detached group-leader parent
        spawns a grandchild in the same group (no new session) with ignored
        stdio that ignores SIGTERM. The parent exits on the first group
        TERM (settling the promise/close early) while the grandchild
        survives it; the unconditional escalation SIGKILL must still reap
        it. Leftover processes are explicitly cleaned up on failure.
        """
        import shutil as _shutil
        import tempfile as _tmp
        bun = _shutil.which("bun")
        assert bun is not None
        start = EXTENSION.index("function killDesktopGroup")
        end = EXTENSION.index("\n}\n", start) + len("\n}\n")
        kill_fn = EXTENSION[start:end]
        with _tmp.TemporaryDirectory() as tmp:
            grandchild_js = Path(tmp) / "grandchild.js"
            grandchild_js.write_text(
                'import { writeFileSync } from "node:fs";\n'
                'process.on("SIGTERM", () => {});\n'
                'writeFileSync(process.argv[3], String(process.pid));\n'
                'setInterval(() => {}, 1000);\n',
                encoding="utf-8")
            parent_js = Path(tmp) / "parent.js"
            parent_js.write_text(
                'import { spawn } from "node:child_process";\n'
                'const kid = spawn(process.execPath, [process.argv[2], "x", process.argv[3]], {\n'
                '  stdio: "ignore", detached: false,\n'
                '});\n'
                'kid.unref?.();\n'
                'setInterval(() => {}, 1000);\n',
                encoding="utf-8")
            pidfile = str(Path(tmp) / "grandchild.pid")
            script = (
                'import { spawn } from "node:child_process";\n'
                'import { readFileSync, existsSync } from "node:fs";\n'
                + kill_fn
                + '\nconst TMP = ' + json.dumps(tmp) + ';\n'
                + r'''
const assert = (v: any, m: string) => { if (!v) throw new Error(m); };
const alive = (pid: number) => {
  try { process.kill(pid, 0); return true; } catch { return false; }
};
// Group-leader parent, like the extension's detached Python child.
const parent: any = spawn(process.execPath,
  [TMP + "/parent.js", TMP + "/grandchild.js", TMP + "/grandchild.pid"], {
  cwd: "/tmp", shell: false, env: process.env,
  detached: process.platform !== "win32", stdio: "ignore",
});
let parentClosed = false;
parent.on("close", () => { parentClosed = true; });
const pidFile = TMP + "/grandchild.pid";
const t0 = Date.now();
while (!existsSync(pidFile) && Date.now() - t0 < 10000)
  await new Promise((r) => setTimeout(r, 50));
assert(existsSync(pidFile), "grandchild pid file never appeared");
const gpid = Number(readFileSync(pidFile, "utf8").trim());
assert(Number.isInteger(gpid) && gpid > 0, "bad grandchild pid");
assert(alive(gpid), "grandchild not alive before TERM");
const pgid = (parent as { pid?: number }).pid as number;
try {
  // Mirror beginTermination: group TERM now, unconditional group SIGKILL
  // escalation after 1500ms even though the parent exits (settles) first.
  try { killDesktopGroup(parent, "SIGTERM"); } catch {}
  setTimeout(() => { try { killDesktopGroup(parent, "SIGKILL"); } catch {} }, 1500).unref?.();
  const t1 = Date.now();
  while (!parentClosed && Date.now() - t1 < 5000)
    await new Promise((r) => setTimeout(r, 50));
  assert(parentClosed, "parent did not exit after group TERM");
  assert(alive(gpid), "grandchild died on TERM; fixture must survive first TERM");
  const t2 = Date.now();
  while (alive(gpid) && Date.now() - t2 < 10000)
    await new Promise((r) => setTimeout(r, 100));
  assert(!alive(gpid), "grandchild survived group SIGKILL escalation");
  console.log(JSON.stringify({ ok: true }));
} finally {
  // Explicit cleanup: never leak the probe processes on failure.
  try { killDesktopGroup(parent, "SIGKILL"); } catch {}
  try { if (alive(gpid)) process.kill(gpid, "SIGKILL"); } catch {}
  try { process.kill(-pgid, "SIGKILL"); } catch {}
}
''')
            harness = Path(tmp) / "harness.ts"
            harness.write_text(script, encoding="utf-8")
            completed = subprocess.run(
                [bun, str(harness)], text=True, capture_output=True,
                timeout=30)
            self.assertEqual(completed.returncode, 0,
                             completed.stderr + completed.stdout)
            self.assertEqual(
                json.loads(completed.stdout.splitlines()[-1]), {"ok": True})


class SeenMapperTests(unittest.TestCase):
    SID1 = "a" * 32
    SID2 = "b" * 32
    GHOST = "00000000-0000-4000-8000-000000000000"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.reg = str(self.base / "projects.toml")
        self.old_env = os.environ.get(projects.ENV_VAR)
        os.environ.pop(projects.ENV_VAR, None)

    def tearDown(self):
        if self.old_env is None:
            os.environ.pop(projects.ENV_VAR, None)
        else:
            os.environ[projects.ENV_VAR] = self.old_env
        self.temp.cleanup()

    def make_entry(self, name="Demo", logseq_path="pages/Demo.md"):
        created = projects.create_project(
            {"name": name, "logseq_path": logseq_path} if logseq_path else
            {"name": name}, self.reg)
        return created["project"]

    def fake_current(self, project):
        return {"available": True, "source": "hyprland",
                "observed_at_ms": 1, "focused_window": None,
                "workspace": None, "resource": None, "project": project}

    def _payload(self, sessions, query="q"):
        return {"query": {"project": None, "query": query, "limit": 20},
                "sessions": sessions, "count": len(sessions)}

    def _session(self, sid, matched, project, resources):
        return {"session_id": sid, "device_id": "c" * 32,
                "project": project, "start_ms": 1,
                "end_ms": matched if isinstance(matched, int) else 1,
                "event_count": 1, "status": "closed",
                "matched_at_ms": matched, "applications": ["kitty"],
                "resources": resources}

    def _record(self, resource, last_seen, occurrence=1,
                identity="portable:file:x"):
        return {"session_id": self.SID1, "resource_key": "k",
                "kind": "portable", "portable_identity": identity,
                "local_identity": None, "resource": resource,
                "occurrence_count": occurrence, "first_seen_ms": 1,
                "last_seen_ms": last_seen, "first_activity_id": 1,
                "last_activity_id": 2}

    def _file_ctx(self, path="/repo/a/src/main.rs", root="/repo/a"):
        return {"adapter": "nvim", "file": path, "cwd": root,
                "git_root": root, "git_branch": None, "git_remote": None,
                "url": None, "page": None, "title": None, "zotero": None}

    def test_query_row_shape_and_kind_enum(self):
        zotero = {"server_id": "srv12345", "library_type": "user",
                  "library_id": "100", "item_key": "ABCD1234",
                  "attachment_key": None, "collections": [],
                  "ancestor_collections": [], "version": 3,
                  "uri": "zotero://select/library/items/ABCD1234"}
        resources = [
            self._record(self._file_ctx(), 10),
            self._record({"adapter": "zen",
                          "url": "https://example.com/docs",
                          "title": "Docs Page", "file": None, "cwd": None,
                          "git_root": None, "page": None, "zotero": None},
                         9, identity="portable:url:https://example.com/docs"),
            self._record({"adapter": "zotero", "title": "Some Paper",
                          "file": None, "cwd": None, "git_root": None,
                          "url": None, "page": None, "zotero": zotero},
                         8, identity="portable:zotero:x"),
            self._record({"adapter": "kitty", "page": "nvim ~/notes",
                          "file": None, "cwd": None, "git_root": None,
                          "url": None, "title": None, "zotero": None},
                         7, identity="portable:page:nvim ~/notes"),
            self._record({"adapter": "kitty", "cwd": "/tmp/work",
                          "file": None, "git_root": None, "url": None,
                          "page": None, "title": None, "zotero": None},
                         6, identity="local:cwd:/tmp/work"),
            # Adapter-only: no location, skipped from the rows.
            self._record({"adapter": "kitty", "file": None, "cwd": None,
                          "git_root": None, "url": None, "page": None,
                          "title": None, "zotero": None}, 5),
        ]
        project = {"id": self.GHOST, "name": "G"}
        payload = self._payload(
            [self._session(self.SID1, 10, project, resources)])
        with patch.object(desktop_projects, "_fetch_search",
                          return_value=payload):
            out = desktop_projects.seen(
                "q", None, None, self.reg, "/bin/x", None)
        self.assertEqual(len(out["rows"]), 5)
        self.assertEqual(out["total"], 5)
        self.assertFalse(out["truncated"])
        for row in out["rows"]:
            self.assertEqual(
                set(row), {"kind", "label", "identity", "project_id",
                           "project_name", "session_id", "last_seen_ms",
                           "occurrence_count"})
            self.assertIn(row["kind"], ("file", "url", "zotero", "page"))
            self.assertLessEqual(len(row["label"]), 160)
            self.assertEqual(row["project_id"], self.GHOST)
            self.assertEqual(row["project_name"], "G")
            self.assertEqual(row["session_id"], self.SID1)
        by_kind = {}
        for row in out["rows"]:
            by_kind.setdefault(row["kind"], []).append(row)
        self.assertEqual(by_kind["file"][0]["label"], "src/main.rs")
        self.assertEqual(by_kind["file"][1]["label"], "/tmp/work")
        self.assertIn("Docs Page", by_kind["url"][0]["label"])
        self.assertIn("example.com", by_kind["url"][0]["label"])
        self.assertIn("Some Paper", by_kind["zotero"][0]["label"])
        self.assertIn("zotero://", by_kind["zotero"][0]["label"])
        self.assertEqual(by_kind["page"][0]["label"], "nvim ~/notes")
        self.assertEqual(by_kind["file"][0]["identity"], "portable:file:x")
        # Newest resource first within the session.
        seen_ms = [row["last_seen_ms"] for row in out["rows"]]
        self.assertEqual(seen_ms, sorted(seen_ms, reverse=True))

    def test_query_orders_sessions_by_match(self):
        old = self._session(self.SID1, 50, None,
                            [self._record(self._file_ctx(), 50)])
        new = self._session(self.SID2, 200, None,
                            [self._record(self._file_ctx("/repo/a/b.py"), 60)])
        payload = self._payload([old, new])
        with patch.object(desktop_projects, "_fetch_search",
                          return_value=payload):
            out = desktop_projects.seen(
                "q", None, None, self.reg, "/bin/x", None)
        self.assertEqual(
            [row["session_id"] for row in out["rows"]],
            [self.SID2, self.SID1])
        self.assertIsNone(out["rows"][0]["project_id"])
        self.assertEqual(out["rows"][0]["project_name"], "")

    def test_query_bounded_twenty_rows_and_labels(self):
        resources = [self._record(self._file_ctx(f"/repo/a/f{i}.py"), i)
                     for i in range(25)]
        long_title = "t" * 200
        resources.append(self._record(
            {"adapter": "zen", "url": "https://example.com/x",
             "title": long_title, "file": None, "cwd": None,
             "git_root": None, "page": None, "zotero": None}, 1000))
        payload = self._payload(
            [self._session(self.SID1, 1000, None, resources)])
        with patch.object(desktop_projects, "_fetch_search",
                          return_value=payload):
            out = desktop_projects.seen(
                "q", None, 100, self.reg, "/bin/x", None)
        self.assertEqual(len(out["rows"]), 20)
        self.assertEqual(out["total"], 26)
        self.assertTrue(out["truncated"])
        for row in out["rows"]:
            self.assertLessEqual(len(row["label"]), 160)

    def test_empty_query_rejects_before_spawn(self):
        for bad in ("", "   "):
            with patch.object(desktop_projects, "_fetch_search") as fetch:
                fetch.side_effect = AssertionError("must reject before spawn")
                with self.assertRaises(desktop_projects.DesktopError):
                    desktop_projects.seen(
                        bad, None, None, self.reg, "/bin/x", None)
            fetch.assert_not_called()

    def test_query_scopes_to_explicit_project(self):
        payload = self._payload([])
        with patch.object(desktop_projects, "_fetch_search",
                          return_value=payload) as fetch:
            desktop_projects.seen(
                "q", self.GHOST, 5, self.reg, "/bin/x", None)
        args = fetch.call_args[0]
        self.assertEqual(args[2], self.GHOST)
        self.assertEqual(args[6], "q")

    def test_bare_defaults_to_current_project(self):
        entry = self.make_entry(name="H", logseq_path="pages/H.md")
        report = {"id": entry["id"], "name": "H", "matched_by": "file"}
        items = [{"resource": self._file_ctx(), "observed_at_ms": 42,
                  "activity_id": 9}]
        with patch.object(desktop_projects, "_fetch_current",
                          return_value=self.fake_current(report)):
            with patch.object(desktop_projects, "_fetch_resources",
                              return_value=items) as res:
                with patch.object(desktop_projects, "_fetch_search") as fetch:
                    fetch.side_effect = AssertionError("bare uses resources")
                    out = desktop_projects.seen(
                        None, None, None, self.reg, "/bin/x", None)
        args = res.call_args[0]
        self.assertEqual(args[2], entry["id"])
        # One-row truncation probe behind the 20-row cap.
        self.assertEqual(args[3], 21)
        self.assertEqual(len(out["rows"]), 1)
        row = out["rows"][0]
        self.assertEqual(row["kind"], "file")
        self.assertEqual(row["label"], "src/main.rs")
        self.assertEqual(row["identity"], "file:src/main.rs")
        self.assertEqual(row["project_id"], entry["id"])
        self.assertEqual(row["project_name"], "H")
        self.assertIsNone(row["session_id"])
        self.assertEqual(row["last_seen_ms"], 42)
        self.assertEqual(row["occurrence_count"], 1)
        self.assertFalse(out["truncated"])
        self.assertEqual(out["total"], 1)

    def test_bare_explicit_project_skips_current(self):
        items = [{"resource": self._file_ctx(), "observed_at_ms": 7,
                  "activity_id": 1}]
        with patch.object(desktop_projects, "_fetch_current") as cur:
            cur.side_effect = AssertionError("explicit must not fetch current")
            with patch.object(desktop_projects, "_fetch_resources",
                              return_value=items):
                out = desktop_projects.seen(
                    None, self.GHOST, None, self.reg, "/bin/x", None)
        self.assertEqual(out["rows"][0]["project_id"], self.GHOST)

    def test_bare_no_current_needs_no_db_query(self):
        with patch.object(desktop_projects, "_fetch_current",
                          return_value=self.fake_current(None)):
            with patch.object(desktop_projects, "_fetch_resources") as res:
                res.side_effect = AssertionError("no DB query")
                out = desktop_projects.seen(
                    None, None, None, self.reg, "/bin/x", None)
        self.assertEqual(out["rows"], [])
        self.assertEqual(out["total"], 0)
        self.assertFalse(out["truncated"])

    def test_bare_truncation_probe(self):
        entry = self.make_entry(name="H", logseq_path="pages/H.md")
        report = {"id": entry["id"], "name": "H", "matched_by": "file"}
        items = [{"resource": self._file_ctx(f"/repo/a/f{i}.py"), "observed_at_ms": i,
                  "activity_id": i} for i in range(21)]
        with patch.object(desktop_projects, "_fetch_current",
                          return_value=self.fake_current(report)):
            with patch.object(desktop_projects, "_fetch_resources",
                              return_value=items):
                out = desktop_projects.seen(
                    None, None, None, self.reg, "/bin/x", None)
        self.assertEqual(len(out["rows"]), 20)
        self.assertTrue(out["truncated"])
        self.assertEqual(out["total"], 20)

    def test_seen_cli_gates(self):
        with patch.object(desktop_projects, "_run_desktop_cli") as run:
            run.side_effect = AssertionError("must reject before spawn")
            self.assertEqual(
                desktop_projects.main(
                    ["--desktop-bin", "/bin/x", "seen", "--session",
                     self.SID1]), 1)
            self.assertEqual(
                desktop_projects.main(
                    ["--desktop-bin", "/bin/x", "seen", "--query", ""]), 1)
            self.assertEqual(
                desktop_projects.main(
                    ["--desktop-bin", "/bin/x", "seen", "--days", "7"]), 1)

    def test_seen_cli_query_happy_path(self):
        payload = self._payload(
            [self._session(self.SID1, 10, None,
                           [self._record(self._file_ctx(), 10)])])
        with patch.object(desktop_projects, "_run_desktop_cli",
                          return_value=json.dumps(payload).encode()):
            buf = io.StringIO()
            with unittest.mock.patch("sys.stdout", buf):
                rc = desktop_projects.main(
                    ["--desktop-bin", "/bin/x", "seen", "--query", "main"])
        self.assertEqual(rc, 0)
        out = json.loads(buf.getvalue())
        self.assertEqual(out["total"], 1)
        self.assertEqual(out["rows"][0]["kind"], "file")


class UnmappedFoldersTests(unittest.TestCase):
    SID1 = "a" * 32
    SID2 = "b" * 32
    RECENT = 9999999999999

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.reg = str(self.base / "projects.toml")
        self.old_env = os.environ.get(projects.ENV_VAR)
        os.environ.pop(projects.ENV_VAR, None)

    def tearDown(self):
        if self.old_env is None:
            os.environ.pop(projects.ENV_VAR, None)
        else:
            os.environ[projects.ENV_VAR] = self.old_env
        self.temp.cleanup()

    def _payload(self, sessions):
        return {"query": {"project": None, "limit": 200},
                "sessions": sessions, "count": len(sessions)}

    def _session(self, sid, end_ms, resources):
        return {"session_id": sid, "device_id": "c" * 32,
                "project": None, "start_ms": 1, "end_ms": end_ms,
                "event_count": 1, "status": "closed",
                "matched_at_ms": None, "applications": ["kitty"],
                "resources": resources}

    def _record(self, resource, last_seen, occurrence=1):
        return {"session_id": self.SID1, "resource_key": "k",
                "kind": "portable", "portable_identity": "portable:x",
                "local_identity": None, "resource": resource,
                "occurrence_count": occurrence, "first_seen_ms": 1,
                "last_seen_ms": last_seen, "first_activity_id": 1,
                "last_activity_id": 2}

    def _folder_ctx(self, folder, cwd=None):
        return {"adapter": "kitty", "file": None,
                "cwd": cwd if cwd is not None else folder,
                "git_root": folder, "git_branch": None, "git_remote": None,
                "url": None, "page": None, "title": None, "zotero": None}

    def test_ranking_prefers_count_then_recency(self):
        sessions = [self._session(self.SID1, self.RECENT, [
            self._record(self._folder_ctx("/repo/side"), 100, 5),
            self._record({"adapter": "kitty", "file": None,
                          "cwd": "/tmp/work", "git_root": None,
                          "git_branch": None, "git_remote": None,
                          "url": None, "page": None, "title": None,
                          "zotero": None}, 50, 2),
            self._record(self._folder_ctx("/repo/other", "/repo/other/sub"),
                         150, 4),
        ]), self._session(self.SID2, self.RECENT, [
            self._record(self._folder_ctx("/repo/side"), 200, 1),
        ])]
        payload = self._payload(sessions)
        with patch.object(desktop_projects, "_fetch_search",
                          return_value=payload):
            out = desktop_projects.unmapped_folders(
                None, None, self.reg, "/bin/x", None)
        self.assertEqual(
            [(row["git_root_or_cwd"], row["observation_count"],
              row["last_seen_ms"]) for row in out["rows"]],
            [("/repo/side", 6, 200), ("/repo/other", 4, 150),
             ("/tmp/work", 2, 50)])
        self.assertFalse(out["truncated"])
        # git_root wins over cwd when both are present.
        self.assertNotIn("/repo/other/sub",
                         [row["git_root_or_cwd"] for row in out["rows"]])

    def test_mapped_folders_and_stale_sessions_excluded(self):
        projects.create_project(
            {"name": "Claimed", "local_folder": "/repo/claimed"}, self.reg)
        sessions = [self._session(self.SID1, self.RECENT, [
            self._record(self._folder_ctx("/repo/claimed"), 300, 9),
            self._record(self._folder_ctx("/repo/free"), 10, 1),
        ]), self._session(self.SID2, 1, [
            self._record(self._folder_ctx("/repo/ancient"), 2, 99),
        ])]
        payload = self._payload(sessions)
        with patch.object(desktop_projects, "_fetch_search",
                          return_value=payload):
            out = desktop_projects.unmapped_folders(
                None, None, self.reg, "/bin/x", None)
        folders = [row["git_root_or_cwd"] for row in out["rows"]]
        self.assertEqual(folders, ["/repo/free"])
        self.assertFalse(out["truncated"])

    def test_home_relative_registry_match(self):
        home = os.path.expanduser("~")
        projects.create_project(
            {"name": "Home", "local_folder": "~/work/app"}, self.reg)
        sessions = [self._session(self.SID1, self.RECENT, [
            self._record(self._folder_ctx(f"{home}/work/app"), 10, 3),
            self._record(self._folder_ctx(f"{home}/work/other"), 11, 1),
        ])]
        payload = self._payload(sessions)
        with patch.object(desktop_projects, "_fetch_search",
                          return_value=payload):
            out = desktop_projects.unmapped_folders(
                None, None, self.reg, "/bin/x", None)
        self.assertEqual(
            [row["git_root_or_cwd"] for row in out["rows"]],
            [f"{home}/work/other"])

    def test_capped_at_ten_rows(self):
        resources = [self._record(self._folder_ctx(f"/repo/r{i}"), i, 1)
                     for i in range(12)]
        payload = self._payload([self._session(self.SID1, self.RECENT,
                                               resources)])
        with patch.object(desktop_projects, "_fetch_search",
                          return_value=payload):
            out = desktop_projects.unmapped_folders(
                None, None, self.reg, "/bin/x", None)
            capped = desktop_projects.unmapped_folders(
                None, 3, self.reg, "/bin/x", None)
        self.assertEqual(len(out["rows"]), 10)
        self.assertTrue(out["truncated"])
        self.assertEqual(len(capped["rows"]), 3)
        self.assertTrue(capped["truncated"])

    def test_days_validation_and_window(self):
        for bad in (0, 366, "x", True):
            with self.assertRaises(desktop_projects.DesktopError):
                desktop_projects.unmapped_folders(
                    bad, None, self.reg, "/bin/x", None)
        self.assertEqual(
            desktop_projects._validate_days(None), 30)
        self.assertEqual(
            desktop_projects._validate_days("7"), 7)

    def test_unmapped_cli_gates_and_happy_path(self):
        with patch.object(desktop_projects, "_run_desktop_cli") as run:
            run.side_effect = AssertionError("must reject before spawn")
            self.assertEqual(
                desktop_projects.main(
                    ["--desktop-bin", "/bin/x", "unmapped-folders",
                     "--project", SeenMapperTests.GHOST]), 1)
            self.assertEqual(
                desktop_projects.main(
                    ["--desktop-bin", "/bin/x", "unmapped-folders",
                     "--query", "x"]), 1)
            self.assertEqual(
                desktop_projects.main(
                    ["--desktop-bin", "/bin/x", "unmapped-folders",
                     "--days", "0"]), 1)
        payload = self._payload([self._session(
            self.SID1, self.RECENT,
            [self._record(self._folder_ctx("/repo/free"), 10, 2)])])
        with patch.object(desktop_projects, "_run_desktop_cli",
                          return_value=json.dumps(payload).encode()):
            buf = io.StringIO()
            with unittest.mock.patch("sys.stdout", buf):
                rc = desktop_projects.main(
                    ["--desktop-bin", "/bin/x",
                     "--projects-file", self.reg,
                     "unmapped-folders", "--days", "7"])
        self.assertEqual(rc, 0)
        out = json.loads(buf.getvalue())
        self.assertEqual(out["rows"][0]["git_root_or_cwd"], "/repo/free")
        self.assertFalse(out["truncated"])


def _watch_child_pids(pid):
    """Pids of live ``qs-desktop-context watch`` children of ``pid``."""
    try:
        out = subprocess.run(
            ["ps", "-o", "pid=,args=", "--ppid", str(pid)],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    pids = []
    for line in (out.stdout or "").splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        try:
            child = int(parts[0])
        except ValueError:
            continue
        if "qs-desktop-context" in parts[1] and "watch" in parts[1]:
            pids.append(child)
    return pids


def _pid_cmd(pid):
    """Full command line for ``pid`` ("" when gone/unreadable)."""
    try:
        out = subprocess.run(
            ["ps", "-o", "args=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (out.stdout or "").strip()


def _pid_dead(pid):
    """True when ``pid`` is gone — or a zombie awaiting init reap.

    A SIGKILLed bridge reparents its Rust child to init (PPID 1); if that
    init never reaps, the child lingers as a zombie, which still proves the
    cascade killed it.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    try:
        out = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    stat = (out.stdout or "").strip().split()
    if not stat:
        return True  # ps knows no such pid
    return stat[0].startswith("Z")


def _reap_bridge_and_child(proc, rust_pids):
    """addCleanup backstop: never leave a bridge or Rust child behind.

    Terminates + reaps the bridge, closes its pipes, then SIGKILLs any
    tracked Rust child that survived — but only when its command line still
    matches (never touch a reused pid).
    """
    try:
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
    finally:
        for stream in (getattr(proc, "stdout", None),
                       getattr(proc, "stderr", None),
                       getattr(proc, "stdin", None)):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass
        for pid in list(rust_pids):
            if _pid_dead(pid):
                continue
            cmd = _pid_cmd(pid)
            if "qs-desktop-context" not in cmd or "watch" not in cmd:
                continue  # pid reused by something else: leave it alone
            try:
                os.kill(pid, 9)
            except OSError:
                pass


class WatchBridgeTests(unittest.TestCase):
    """Resident ``watch`` bridge: one raw-source child, in-process mapping.

    The bridge resolves the binary exactly like the one-shot path
    (``--desktop-bin`` > ``QS_DESKTOP_CONTEXT_BIN`` > repo default, with the
    child-only registry override), spawns ``qs-desktop-context watch`` ONCE
    per bridge lifetime, maps each raw line through the shared single-owner
    mapping (no fork per line), and prints the tagged ``current-project``
    shape change-only. Child exit/stream end => non-zero exit (DesktopError
    in-process) so the supervisor restarts the pair.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.reg = str(self.base / "projects.toml")
        Path(self.reg).write_text("version = 1\n", encoding="utf-8")
        self.missing_db = str(self.base / "missing.db")
        self.old_bin = os.environ.get(desktop_projects.BIN_ENV)
        self.old_reg = os.environ.get(projects.ENV_VAR)
        os.environ.pop(desktop_projects.BIN_ENV, None)
        os.environ.pop(projects.ENV_VAR, None)

    def tearDown(self):
        for key, value in ((desktop_projects.BIN_ENV, self.old_bin),
                           (projects.ENV_VAR, self.old_reg)):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def _raw(self, project, ts=1):
        return {"available": True, "source": "hyprland",
                "observed_at_ms": ts, "focused_window": None,
                "workspace": None, "resource": None, "project": project}

    def _watch_proc(self, lines):
        proc = unittest.mock.MagicMock()
        proc.stdout.readline.side_effect = list(lines)
        proc.poll.return_value = 0
        proc.stderr = None
        return proc

    def _run_watch_capture(self, proc, registry_file=None):
        captured = {}

        def fake_popen(argv, **kwargs):
            captured["argv"] = argv
            captured["env"] = kwargs.get("env")
            return proc

        buf = io.BytesIO()
        fake_stdout = unittest.mock.MagicMock()
        fake_stdout.buffer = buf
        with patch("subprocess.Popen", side_effect=fake_popen):
            with patch.object(sys, "stdout", fake_stdout):
                with self.assertRaises(desktop_projects.DesktopError) as ctx:
                    desktop_projects.watch_current_project(
                        registry_file, "/bin/x", None)
        self.assertIn("watch source ended", str(ctx.exception))
        return captured, buf.getvalue()

    def test_watch_uses_shared_mapping_startup_and_change_only(self):
        entry = projects.create_project(
            {"name": "Demo", "logseq_path": "pages/Demo.md"},
            self.reg)["project"]
        null_a = self._raw(None, ts=1)
        null_b = self._raw(None, ts=2)  # timestamp-only churn: no re-emit
        assoc = self._raw({"id": entry["id"], "name": "Stale",
                           "matched_by": "cwd"}, ts=3)
        assoc_dup = self._raw({"id": entry["id"], "name": "Stale",
                               "matched_by": "cwd"}, ts=4)
        lines = [(json.dumps(v) + "\n").encode() for v in
                 (null_a, null_b, assoc, assoc_dup)] + [b""]
        proc = self._watch_proc(lines)
        captured, raw_out = self._run_watch_capture(proc, self.reg)
        # One child for the whole stream (no fork per line); watch subcommand.
        self.assertEqual(captured["argv"], ["/bin/x", "watch"])
        self.assertEqual(proc.stdout.readline.call_count, len(lines))
        # Startup line + one line per state change only.
        out_lines = raw_out.decode("utf-8").splitlines()
        self.assertEqual(len(out_lines), 2)
        first = json.loads(out_lines[0])
        second = json.loads(out_lines[1])
        self.assertEqual(first["type"], "current-project")
        self.assertEqual(first["status"], "unassociated")
        self.assertIsNone(first["project"])
        self.assertEqual(second["type"], "current-project")
        self.assertEqual(second["status"], "associated")
        self.assertEqual(second["project"]["id"], entry["id"])
        # Current registry mapping wins, like the one-shot path.
        self.assertEqual(second["project"]["name"], "Demo")
        self.assertTrue(second["has_logseq_linkage"])
        # Shared single-owner mapping: the watch payload (minus the tag) is
        # byte-identical to what the one-shot command produces for the same
        # raw input.
        with patch.object(desktop_projects, "_fetch_current",
                          return_value=assoc):
            one_shot = desktop_projects.current_project(
                self.reg, "/bin/x", None)
        tagged = dict(one_shot)
        tagged["type"] = "current-project"
        self.assertEqual(
            json.loads(out_lines[1]),
            json.loads(json.dumps(tagged, ensure_ascii=False,
                                  separators=(",", ":"))))
        with patch.object(desktop_projects, "_fetch_current",
                          return_value=null_a):
            one_shot_null = desktop_projects.current_project(
                self.reg, "/bin/x", None)
        tagged_null = dict(one_shot_null)
        tagged_null["type"] = "current-project"
        # Timestamps ride along verbatim in the payload even though they do
        # not defeat the dedup key: normalize before comparing.
        for payload in (first, tagged_null):
            payload["context"].pop("observed_at_ms", None)
        self.assertEqual(first, tagged_null)

    def test_watch_forwards_registry_override_child_only(self):
        captured, _ = self._run_watch_capture(
            self._watch_proc([b""]), self.reg)
        self.assertEqual(captured["argv"], ["/bin/x", "watch"])
        self.assertIsNotNone(captured.get("env"))
        self.assertEqual(
            captured["env"].get(projects.ENV_VAR),
            str(projects.resolve_registry_file(self.reg)))
        # No global mutation.
        self.assertNotIn(projects.ENV_VAR, os.environ)
        captured2, _ = self._run_watch_capture(
            self._watch_proc([b""]), None)
        self.assertIsNone(captured2.get("env"))

    def test_watch_skips_bad_lines_on_stderr_keeps_stdout_pure(self):
        good = self._raw(None, ts=1)
        lines = [b"\n",
                 (json.dumps(good) + "\n").encode(),
                 b"not json\n",
                 b"[1, 2]\n",
                 b"\xff\xfe\n",
                 b""]
        proc = self._watch_proc(lines)
        buf = io.BytesIO()
        fake_stdout = unittest.mock.MagicMock()
        fake_stdout.buffer = buf
        err = io.StringIO()
        with patch("subprocess.Popen", return_value=proc):
            with patch.object(sys, "stdout", fake_stdout):
                with unittest.mock.patch("sys.stderr", err):
                    with self.assertRaises(desktop_projects.DesktopError):
                        desktop_projects.watch_current_project(
                            self.reg, "/bin/x", None)
        out_lines = buf.getvalue().decode("utf-8").splitlines()
        self.assertEqual(len(out_lines), 1)
        parsed = json.loads(out_lines[0])
        self.assertEqual(parsed["type"], "current-project")
        diagnostics = err.getvalue()
        self.assertIn("not valid JSON", diagnostics)
        self.assertIn("must be an object", diagnostics)
        self.assertIn("not valid UTF-8", diagnostics)

    def test_watch_missing_binary_is_clear_no_build(self):
        with self.assertRaises(desktop_projects.DesktopError) as ctx:
            desktop_projects.watch_current_project(
                self.reg, "/nonexistent-qs-watch-12345", None)
        self.assertIn("not found", str(ctx.exception))

    def test_watch_cli_child_death_exits_nonzero_stdout_pure(self):
        fake = self.base / "fake-watch-bin.sh"
        ghost = "00000000-0000-4000-8000-000000000000"
        fake.write_text(
            "#!/bin/sh\n"
            "echo '{\"available\":true,\"source\":\"hyprland\","
            "\"observed_at_ms\":1,\"focused_window\":null,"
            "\"workspace\":null,\"resource\":null,\"project\":null}'\n"
            "echo '{\"available\":true,\"source\":\"hyprland\","
            "\"observed_at_ms\":2,\"focused_window\":null,"
            "\"workspace\":null,\"resource\":null,\"project\":null}'\n"
            "echo '{\"available\":true,\"source\":\"hyprland\","
            "\"observed_at_ms\":3,\"focused_window\":null,"
            "\"workspace\":null,\"resource\":null,"
            "\"project\":{\"id\":\"" + ghost + "\",\"name\":\"Ghost\","
            "\"matched_by\":\"cwd\"}}'\n"
            "echo 'fake child diagnostics' >&2\n",
            encoding="utf-8")
        fake.chmod(0o755)
        completed = run_cli(
            ["--projects-file", self.reg, "--db", self.missing_db,
             "--desktop-bin", str(fake), "watch"])
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("watch source ended", completed.stderr)
        self.assertIn("fake child diagnostics", completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)
        out_lines = completed.stdout.splitlines()
        # Null, null-ts-churn (deduped), ghost => two bridge lines.
        self.assertEqual(len(out_lines), 2)
        first = json.loads(out_lines[0])
        second = json.loads(out_lines[1])
        for parsed in (first, second):
            self.assertEqual(parsed["type"], "current-project")
        self.assertEqual(first["status"], "unassociated")
        self.assertIsNone(first["project"])
        self.assertEqual(second["status"], "stale-removed")
        self.assertIsNone(second["project"])
        self.assertEqual(second["reported_project"]["id"], ghost)
        self.assertFalse(Path(self.missing_db).exists())

    def test_watch_cli_rejects_one_shot_flags(self):
        for extra in (["--limit", "5"],
                      ["--project",
                       "00000000-0000-4000-8000-000000000000"],
                      ["--query", "x"]):
            completed = run_cli(
                ["--projects-file", self.reg,
                 "--desktop-bin", "/bin/x", "watch", *extra])
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("does not accept", completed.stderr)
            self.assertEqual(completed.stdout, "")
            self.assertNotIn("Traceback", completed.stderr)

    def test_spawn_watch_child_holds_stdin_pipe(self):
        # The bridge must hold the Rust child's stdin write end for life:
        # ANY bridge death (including SIGKILL) closes it and the Rust
        # stdin-EOF watchdog exits the orphan on its own.
        captured = {}
        proc = unittest.mock.MagicMock()
        proc.stdout = unittest.mock.MagicMock()

        def fake_popen(argv, **kwargs):
            captured["argv"] = argv
            captured.update(kwargs)
            return proc

        with patch("subprocess.Popen", side_effect=fake_popen):
            got = desktop_projects._spawn_watch_child(
                Path("/bin/x"), None)
        self.assertIs(got, proc)
        self.assertEqual(captured["argv"], ["/bin/x", "watch"])
        self.assertEqual(captured.get("stdin"), subprocess.PIPE)
        desktop_projects._unregister_child(proc)


class WatchRealBinaryTests(unittest.TestCase):
    """Bridge over the real release binary: one line, tagged shape, no hang.

    Requires the release binary (same gate as ``RealBinaryBoundaryTests``).
    A bogus ``HYPRLAND_INSTANCE_SIGNATURE`` forces the unavailable object so
    the expectation is deterministic even on a live desktop.
    """

    BIN = ROOT / "services" / "agent-orchestrator" / "target" / "release" / "qs-desktop-context"

    def setUp(self):
        if not (self.BIN.is_file() and os.access(self.BIN, os.X_OK)):
            self.skipTest("real qs-desktop-context binary not built")
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.reg = str(self.base / "empty.toml")
        Path(self.reg).write_text("version = 1\n", encoding="utf-8")
        self.missing_db = str(self.base / "missing.db")
        self.old_reg = os.environ.get(projects.ENV_VAR)
        os.environ.pop(projects.ENV_VAR, None)

    def tearDown(self):
        if self.old_reg is None:
            os.environ.pop(projects.ENV_VAR, None)
        else:
            os.environ[projects.ENV_VAR] = self.old_reg
        self.temp.cleanup()

    def test_real_watch_bridge_startup_line_then_terminate(self):
        import queue
        import signal as _signal
        import threading
        import time as _time
        env = dict(os.environ)
        env["HYPRLAND_INSTANCE_SIGNATURE"] = "qs-watch-test-nonexistent"
        proc = subprocess.Popen(
            [sys.executable, "-u", str(ROOT / "scripts" / "desktop_projects.py"),
             "--projects-file", self.reg, "--db", self.missing_db,
             "--desktop-bin", str(self.BIN), "watch"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, env=env)
        rust_pid: list = []
        self.addCleanup(_reap_bridge_and_child, proc, rust_pid)
        try:
            lines: queue.Queue = queue.Queue()
            stop = threading.Event()

            def pump():
                assert proc.stdout is not None
                for raw in proc.stdout:
                    lines.put(raw)
                    if stop.is_set():
                        return

            reader = threading.Thread(target=pump, daemon=True)
            reader.start()
            try:
                raw_first = lines.get(timeout=25)
            except queue.Empty:
                self.fail("watch bridge emitted no startup line")
            first = json.loads(raw_first.decode("utf-8"))
            self.assertEqual(first["type"], "current-project")
            self.assertIn(first["status"],
                          ("unassociated", "stale-removed"))
            self.assertIsNone(first["project"])
            self.assertIsInstance(first["context"], dict)
            # Remember the Rust grandchild now (proves the bridge spawned
            # exactly one): cleanup kills it if the bridge didn't.
            rust_pid.extend(_watch_child_pids(proc.pid))
            # No second line while the (unavailable) state is unchanged.
            with self.assertRaises(queue.Empty):
                lines.get(timeout=5)
        finally:
            stop.set()
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
            self.assertIsNotNone(proc.returncode)
        _, stderr = proc.communicate(timeout=10)
        self.assertNotIn("Traceback", stderr.decode("utf-8", "replace"))
        self.assertFalse(Path(self.missing_db).exists())


class WatchSupervisionTests(unittest.TestCase):
    """Leak regression: the supervision chain cascades on ANY parent death.

    quickshell --(stdin pipe)--> ``desktop_projects.py watch``
    --(stdin pipe)--> ``qs-desktop-context watch``. A death at any level
    EOFs the level below (the kernel closes the dead process's pipe ends),
    so SIGKILL at any level cannot orphan a resident 2 s recheck loop.

    Requires the release binary (same gate as ``RealBinaryBoundaryTests``).
    A bogus ``HYPRLAND_INSTANCE_SIGNATURE`` forces the unavailable object
    so expectations are deterministic even on a live desktop.
    """

    BIN = ROOT / "services" / "agent-orchestrator" / "target" / "release" / "qs-desktop-context"

    def setUp(self):
        if not (self.BIN.is_file() and os.access(self.BIN, os.X_OK)):
            self.skipTest("real qs-desktop-context binary not built")
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.reg = str(self.base / "empty.toml")
        Path(self.reg).write_text("version = 1\n", encoding="utf-8")
        self.missing_db = str(self.base / "missing.db")
        self.old_reg = os.environ.get(projects.ENV_VAR)
        os.environ.pop(projects.ENV_VAR, None)

    def tearDown(self):
        if self.old_reg is None:
            os.environ.pop(projects.ENV_VAR, None)
        else:
            os.environ[projects.ENV_VAR] = self.old_reg
        self.temp.cleanup()

    def _spawn_bridge(self):
        import signal as _signal  # noqa: F401 (documents SIGKILL below)
        env = dict(os.environ)
        env["HYPRLAND_INSTANCE_SIGNATURE"] = "qs-watch-test-nonexistent"
        # stdin=PIPE simulates quickshell holding the bridge's stdin open.
        return subprocess.Popen(
            [sys.executable, "-u", str(ROOT / "scripts" / "desktop_projects.py"),
             "--projects-file", self.reg, "--db", self.missing_db,
             "--desktop-bin", str(self.BIN), "watch"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.PIPE, env=env)

    def _startup_line(self, bridge):
        import queue
        import threading
        lines: queue.Queue = queue.Queue()
        stop = threading.Event()

        def pump():
            assert bridge.stdout is not None
            for raw in bridge.stdout:
                lines.put(raw)
                if stop.is_set():
                    return

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        try:
            raw_first = lines.get(timeout=25)
        except queue.Empty:
            self.fail("watch bridge emitted no startup line")
        first = json.loads(raw_first.decode("utf-8"))
        self.assertEqual(first["type"], "current-project")
        self.assertIn(first["status"], ("unassociated", "stale-removed"))
        self.assertIsNone(first["project"])
        return stop

    def test_sigkill_bridge_cascades_to_rust_child(self):
        import time as _time
        bridge = self._spawn_bridge()
        rust_pids: list = []
        self.addCleanup(_reap_bridge_and_child, bridge, rust_pids)
        stop = self._startup_line(bridge)
        children = _watch_child_pids(bridge.pid)
        self.assertEqual(len(children), 1,
                         f"bridge must own exactly one Rust watch child, got {children}")
        rust = children[0]
        rust_pids.append(rust)
        # No signal is ever sent to the Rust child: SIGKILL the bridge and
        # the stdin-pipe cascade must reap the orphan on its own.
        stop.set()
        bridge.kill()  # SIGKILL: no handler in the bridge can run
        try:
            bridge.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.fail("SIGKILLed bridge did not die")
        self.assertIsNotNone(bridge.returncode)
        start = _time.monotonic()
        deadline = start + 10.0
        while not _pid_dead(rust):
            if _time.monotonic() >= deadline:
                self.fail(
                    f"orphaned qs-desktop-context watch (pid {rust}) still "
                    "alive 10 s after bridge SIGKILL (expected ~5 s)")
            _time.sleep(0.1)
        elapsed = _time.monotonic() - start
        self.assertLess(
            elapsed, 5.0,
            f"orphaned Rust child took {elapsed:.1f} s to exit (expected ~5 s)")
        self.assertFalse(Path(self.missing_db).exists())

    def test_closing_bridge_stdin_kills_rust_child_and_exits_bridge(self):
        import time as _time
        bridge = self._spawn_bridge()
        rust_pids: list = []
        self.addCleanup(_reap_bridge_and_child, bridge, rust_pids)
        stop = self._startup_line(bridge)
        children = _watch_child_pids(bridge.pid)
        self.assertEqual(len(children), 1,
                         f"bridge must own exactly one Rust watch child, got {children}")
        rust = children[0]
        rust_pids.append(rust)
        # Simulate quickshell teardown: close our write end of the bridge's
        # stdin. The bridge's EOF thread must SIGTERM its Rust child, see
        # stream end, and exit non-zero on its own.
        stop.set()
        assert bridge.stdin is not None
        bridge.stdin.close()
        try:
            code = bridge.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.fail("bridge did not exit after its stdin EOF")
        self.assertNotEqual(code, 0)
        deadline = _time.monotonic() + 5.0
        while not _pid_dead(rust):
            if _time.monotonic() >= deadline:
                self.fail(
                    f"Rust watch child (pid {rust}) still alive after "
                    "bridge stdin EOF")
            _time.sleep(0.1)
        try:
            _, stderr = bridge.communicate(timeout=10)
        except Exception:
            stderr = b""
        self.assertIn("watch source ended",
                      stderr.decode("utf-8", "replace"))
        self.assertFalse(Path(self.missing_db).exists())


if __name__ == "__main__":
    unittest.main()
