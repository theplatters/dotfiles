"""Focused tests for the deterministic Resume backend (Phase 6)."""

import copy
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import desktop_resume as resume
import projects

DEV = "a" * 32
FOREIGN = "b" * 32


def run_cli(args, env_extra=None):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "desktop_resume.py"), *args],
        text=True, capture_output=True, timeout=20, check=False, env=env,
    )
    return completed


def make_session(sid, pid, device, end=100, start=10, status="closed"):
    return {
        "session_id": sid,
        "device_id": device,
        "project": {"id": pid, "name": "OldName"},
        "start_ms": start,
        "end_ms": end,
        "first_activity_id": 1,
        "last_activity_id": 9,
        "event_count": 4,
        "status": status,
        "ended_reason": "inactivity" if status == "closed" else None,
        "unresolved_start_ms": None,
        "active": False,
        "effective_status": status,
        "applications": ["kitty", "nvim"],
        "gap_ms": 1800000,
        "interruption_ms": 120000,
    }


def make_rollup(sid, identity, count=1, seen=50, branch="main"):
    if identity.startswith("file:/"):
        portable, local = None, identity
    elif identity.startswith(("url:", "page:", "cwd:", "file:")):
        portable, local = identity, None
    else:
        portable, local = None, None
    resource = {
        "adapter": "neovim",
        "file": None,
        "cwd": None,
        "git_root": "/repo",
        "git_branch": branch,
        "git_remote": None,
        "url": None,
        "page": None,
        "title": "t",
    }
    if portable and portable.startswith("file:"):
        resource["file"] = "/repo/" + portable[len("file:"):]
    return {
        "session_id": sid,
        "resource_key": f"portable:{identity}" if portable else f"local:{identity}",
        "kind": "portable" if portable else "local",
        "portable_identity": portable,
        "local_identity": local,
        "resource": resource,
        "occurrence_count": count,
        "first_seen_ms": seen - 10,
        "last_seen_ms": seen,
        "first_activity_id": 1,
        "last_activity_id": 2,
    }


def make_activity(sid, device, workspace="3"):
    snapshot = {"available": True, "source": "hyprland",
                "observed_at_ms": 100}
    if workspace is not None:
        snapshot["workspace"] = {"id": "1", "name": workspace}
    return {
        "id": 9,
        "observed_at_ms": 100,
        "kind": "focus",
        "source": "hyprland",
        "snapshot": snapshot,
        "event_id": "e" * 32,
        "device_id": device,
        "session_id": sid,
    }


class ListAndResolveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.reg = str(self.base / "projects.toml")
        old = os.environ.get(projects.ENV_VAR)
        self._old = old
        os.environ.pop(projects.ENV_VAR, None)

    def tearDown(self):
        if self._old is None:
            os.environ.pop(projects.ENV_VAR, None)
        else:
            os.environ[projects.ENV_VAR] = self._old
        self.temp.cleanup()

    def add(self, name, **extra):
        payload = {"name": name}
        payload.update(extra)
        return projects.create_project(payload, self.reg)["project"]

    def test_uuid_exact_resolves(self):
        entry = self.add("Demo")
        out = resume.resolve_identifier(entry["id"], self.reg)
        self.assertEqual(out["id"], entry["id"])
        out2 = resume.resolve_identifier(entry["id"].upper(), self.reg)
        self.assertEqual(out2["id"], entry["id"])

    def test_unknown_uuid_rejected(self):
        self.add("Demo")
        with self.assertRaises(resume.ResumeError) as ctx:
            resume.resolve_identifier(
                "00000000-0000-4000-8000-000000000000", self.reg)
        self.assertIn("unknown", str(ctx.exception))

    def test_exact_casefold_wins_over_prefix(self):
        self.add("Alpha")
        self.add("Alpha Beta")
        out = resume.resolve_identifier("alpha", self.reg)
        self.assertEqual(out["name"], "Alpha")

    def test_beforeit_prefix_resolves_uniquely(self):
        entry = self.add("BeforeIT ECS-Rewrite",
                         logseq_path="pages/BeforeIT ECS-Rewrite.md")
        out = resume.resolve_identifier("BeforeIT", self.reg)
        self.assertEqual(out["id"], entry["id"])
        listed = resume.list_entries("BeforeIT", None, self.reg)
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["entries"][0]["match"], "prefix")

    def test_ambiguous_prefix_rejected(self):
        self.add("Alpha One")
        self.add("Alpha Two")
        with self.assertRaises(resume.ResumeError) as ctx:
            resume.resolve_identifier("Alpha", self.reg)
        self.assertIn("ambiguous", str(ctx.exception))
        # Listing still ranks both predictably.
        listed = resume.list_entries("Alpha", None, self.reg)
        self.assertEqual(listed["count"], 2)
        self.assertTrue(all(e["match"] == "prefix" for e in listed["entries"]))
        names = [e["name"] for e in listed["entries"]]
        self.assertEqual(names, sorted(names, key=str.casefold))

    def test_ambiguous_substring_rejected(self):
        self.add("One Alpha")
        self.add("Two Alpha")
        with self.assertRaises(resume.ResumeError) as ctx:
            resume.resolve_identifier("Alpha", self.reg)
        self.assertIn("ambiguous", str(ctx.exception))

    def test_fuzzy_lists_but_plan_rejects(self):
        self.add("BeforeIT ECS-Rewrite")
        listed = resume.list_entries("BfrIT", None, self.reg)
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["entries"][0]["match"], "fuzzy")
        with self.assertRaises(resume.ResumeError) as ctx:
            resume.resolve_identifier("BfrIT", self.reg)
        self.assertIn("unknown", str(ctx.exception))

    def test_unknown_name_rejected(self):
        self.add("Demo")
        with self.assertRaises(resume.ResumeError) as ctx:
            resume.resolve_identifier("Nope", self.reg)
        self.assertIn("unknown", str(ctx.exception))

    def test_ranking_exact_prefix_substring_fuzzy(self):
        self.add("it")
        self.add("Item")
        self.add("Big it box")
        self.add("ixt")
        listed = resume.list_entries("it", None, self.reg)
        kinds = [e["match"] for e in listed["entries"]]
        # exact(it) < prefix(Item) < substring(Big it box) < fuzzy(ixt)
        self.assertEqual(kinds, ["exact", "prefix", "substring", "fuzzy"])

    def test_list_limit_bounded(self):
        for bad in (0, 101, "x", True):
            with self.assertRaises(resume.ResumeError):
                resume.list_entries(None, bad, self.reg)
        out = resume.list_entries(None, 1, self.reg)
        self.assertLessEqual(out["count"], 1)


class SessionSelectionTests(unittest.TestCase):
    PID = "11111111-2222-4333-8444-555555555555"

    def test_prefers_current_device_most_recent(self):
        old = make_session("0" * 32, self.PID, FOREIGN, end=9999)
        mid = make_session("1" * 32, self.PID, DEV, end=50)
        new = make_session("2" * 32, self.PID, DEV, end=70)
        other_project = make_session(
            "3" * 32, "99999999-2222-4333-8444-555555555555", DEV, end=99999)
        picked, reason = resume.select_current_device_session(
            [old, other_project, mid, new], self.PID, DEV)
        self.assertEqual(picked["session_id"], "2" * 32)
        self.assertEqual(reason, "")

    def test_foreign_only_is_no_session(self):
        picked, reason = resume.select_current_device_session(
            [make_session("0" * 32, self.PID, FOREIGN)], self.PID, DEV)
        self.assertIsNone(picked)
        self.assertIn("no work session", reason)

    def test_missing_device_is_no_session(self):
        picked, reason = resume.select_current_device_session(
            [make_session("0" * 32, self.PID, DEV)], self.PID, None)
        self.assertIsNone(picked)
        self.assertIn("device", reason)


class ResourceSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "proj"
        (self.root / "src").mkdir(parents=True)
        for name in ("a.py", "b.py", "c.py", "d.py", "e.py"):
            (self.root / "src" / name).write_text(f"# {name}\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    SESSION = {"start_ms": 0, "end_ms": 100}

    def test_scoring_balanced_recency_and_frequency_stable_ties(self):
        sid = "c" * 32
        items = [
            make_rollup(sid, "file:src/b.py", count=1, seen=90),
            make_rollup(sid, "file:src/a.py", count=3, seen=10),
            make_rollup(sid, "file:src/c.py", count=3, seen=80),
            make_rollup(sid, "file:src/d.py", count=3, seen=80),
        ]
        scored = resume._score_resources(items, self.SESSION)
        idents = [resume._identity_of(i) for i in scored]
        # c/d tie on the balanced score: stable resource_key order wins.
        # b (recent, rare) beats a (stale, frequent) on recency weight.
        self.assertEqual(
            idents,
            ["file:src/c.py", "file:src/d.py", "file:src/b.py", "file:src/a.py"])

    def test_ancient_frequent_loses_to_session_end(self):
        sid = "c" * 32
        items = [
            make_rollup(sid, "file:src/old.py", count=50, seen=0),
            make_rollup(sid, "file:src/new.py", count=1, seen=100),
        ]
        scored = resume._score_resources(items, self.SESSION)
        idents = [resume._identity_of(i) for i in scored]
        self.assertEqual(idents, ["file:src/new.py", "file:src/old.py"])

    def test_scoring_without_session_span_uses_stable_rank(self):
        sid = "c" * 32
        items = [
            make_rollup(sid, "file:src/a.py", count=1, seen=10),
            make_rollup(sid, "file:src/b.py", count=1, seen=90),
        ]
        first = resume._score_resources(items)
        second = resume._score_resources(items)
        self.assertEqual(
            [resume._identity_of(i) for i in first],
            [resume._identity_of(i) for i in second])
        self.assertEqual(resume._identity_of(first[0]), "file:src/b.py")

    def test_selects_at_most_four_portable(self):
        sid = "c" * 32
        items = [make_rollup(sid, f"file:src/{n}.py", count=5 - i, seen=100 - i)
                 for i, n in enumerate("abcde")]
        files, unavailable = resume._select_files(
            resume._score_resources(items, self.SESSION), self.root, "")
        self.assertEqual(len(files), 4)
        rels = sorted(f["relative"] for f in files)
        self.assertEqual(rels, ["src/a.py", "src/b.py", "src/c.py", "src/d.py"])

    def test_deleted_outside_transient_skipped_with_reasons(self):
        sid = "c" * 32
        items = [
            make_rollup(sid, "file:src/gone.py", count=9, seen=99),
            make_rollup(sid, "file:src/a.py", count=8, seen=98),
            {"session_id": sid, "resource_key": "local:file:/etc/passwd",
             "kind": "local", "portable_identity": None,
             "local_identity": "file:/etc/passwd",
             "resource": {"adapter": "x", "file": "/etc/passwd"},
             "occurrence_count": 7, "first_seen_ms": 1, "last_seen_ms": 97,
             "first_activity_id": 1, "last_activity_id": 2},
            make_rollup(sid, "url:https://example.com/x", count=6, seen=96),
            make_rollup(sid, "page:Some Page", count=5, seen=95),
            make_rollup(sid, "cwd:src", count=4, seen=94),
        ]
        files, unavailable = resume._select_files(
            resume._score_resources(items, self.SESSION), self.root, "")
        rels = [f["relative"] for f in files]
        self.assertIn("src/a.py", rels)
        reasons = " ".join(u["reason"] for u in unavailable)
        self.assertIn("deleted or missing", reasons)
        self.assertIn("outside the project folder", reasons)
        self.assertIn("not reopened", reasons)

    def test_local_absolute_inside_root_converted(self):
        sid = "c" * 32
        target = str(self.root / "src" / "b.py")
        item = {"session_id": sid, "resource_key": f"local:file:{target}",
                "kind": "local", "portable_identity": None,
                "local_identity": f"file:{target}",
                "resource": {"adapter": "kitty", "file": target},
                "occurrence_count": 2, "first_seen_ms": 1, "last_seen_ms": 60,
                "first_activity_id": 1, "last_activity_id": 2}
        files, _un = resume._select_files([item], self.root, "")
        self.assertEqual([f["relative"] for f in files], ["src/b.py"])

    def test_symlink_is_not_reopened(self):
        link = self.root / "src" / "link.py"
        try:
            link.symlink_to(self.root / "src" / "a.py")
        except OSError:
            self.skipTest("symlinks unavailable")
        sid = "c" * 32
        item = make_rollup(sid, "file:src/link.py", count=9, seen=99)
        files, unavailable = resume._select_files([item], self.root, "")
        self.assertEqual(files, [])
        self.assertIn("symlink", unavailable[0]["reason"])

    def test_no_root_marks_all_unavailable(self):
        sid = "c" * 32
        files, unavailable = resume._select_files(
            [make_rollup(sid, "file:src/a.py")], None, "no mapping")
        self.assertEqual(files, [])
        self.assertIn("no mapping", unavailable[0]["reason"])


class PlanBuilderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.reg = str(self.base / "projects.toml")
        old = os.environ.get(projects.ENV_VAR)
        self._old_reg = old
        os.environ.pop(projects.ENV_VAR, None)
        self.graph = self.base / "graph"
        (self.graph / "pages").mkdir(parents=True)
        (self.graph / "journals").mkdir()
        self.proj = self.base / "work"
        (self.proj / "src").mkdir(parents=True)
        (self.proj / "src" / "main.py").write_text("print(1)\n", encoding="utf-8")
        (self.graph / "pages" / "Demo.md").write_text(
            "file:: " + str(self.proj) + "\n\n- TODO ship it\n- DONE old\n",
            encoding="utf-8")
        created = projects.create_project(
            {"name": "Demo", "logseq_path": "pages/Demo.md",
             "local_folder": str(self.proj)}, self.reg)
        self.entry = created["project"]
        self.sid = "d" * 32
        # Keep Pi scopes inside temp; never touch the real state home.
        self.pi_base = self.base / "pibase"
        self.pi_base.mkdir()
        self._old_pi = os.environ.get("PI_CODING_AGENT_SESSION_DIR")
        self._old_scope = os.environ.get("QS_PROJECT_SESSION_SCOPE")
        os.environ["PI_CODING_AGENT_SESSION_DIR"] = str(self.pi_base)
        os.environ.pop("QS_PROJECT_SESSION_SCOPE", None)

    def tearDown(self):
        if self._old_reg is None:
            os.environ.pop(projects.ENV_VAR, None)
        else:
            os.environ[projects.ENV_VAR] = self._old_reg
        if self._old_pi is None:
            os.environ.pop("PI_CODING_AGENT_SESSION_DIR", None)
        else:
            os.environ["PI_CODING_AGENT_SESSION_DIR"] = self._old_pi
        if self._old_scope is None:
            os.environ.pop("QS_PROJECT_SESSION_SCOPE", None)
        else:
            os.environ["QS_PROJECT_SESSION_SCOPE"] = self._old_scope
        self.temp.cleanup()

    def _plan(self, **over):
        session = make_session(self.sid, self.entry["id"], DEV)
        rollups = [make_rollup(self.sid, "file:src/main.py", count=4, seen=90)]
        activity = make_activity(self.sid, DEV, workspace="3")
        params = dict(
            device_id=DEV, sessions=[session], rollups=rollups,
            activity=activity, graph=str(self.graph),
            which=lambda name: "/usr/bin/" + name,
        )
        params.update(over)
        payload = {"query": {"project": self.entry["id"], "device": DEV,
                             "limit": 1},
                   "sessions": params["sessions"] if isinstance(
                       params["sessions"], list) else [],
                   "count": len(params["sessions"]) if isinstance(
                       params["sessions"], list) else 0}
        with patch.object(resume, "_fetch_device_id",
                          return_value=params["device_id"]), \
             patch.object(resume, "_fetch_search_sessions",
                          return_value=payload), \
             patch.object(resume, "_fetch_project_sessions",
                          side_effect=AssertionError(
                              "indexed search must replace the 50-row scan")), \
             patch.object(resume, "_fetch_session_resources",
                          return_value=params["rollups"]), \
             patch.object(resume, "_fetch_last_activity",
                          return_value=params["activity"]):
            return resume.plan_for_project(
                self.entry["id"], registry_file=self.reg,
                desktop_bin="/bin/false", db="/tmp/qs-resume-test.db",
                graph=params["graph"], which=params["which"])

    def test_full_plan_shape_and_no_content_leak(self):
        plan = self._plan()
        self.assertEqual(plan["version"], 1)
        self.assertEqual(plan["project"]["name"], "Demo")
        self.assertEqual(plan["device_id"], DEV)
        self.assertEqual(plan["session"]["session_id"], self.sid)
        self.assertEqual([f["relative"] for f in plan["files"]], ["src/main.py"])
        self.assertNotIn("content", json.dumps(plan))
        self.assertEqual(plan["logseq"]["open_count"], 1)
        self.assertEqual(plan["logseq"]["open_todos"][0]["task"], "ship it")
        kinds = [op["kind"] for op in plan["operations"]]
        self.assertEqual(kinds, list(resume.OPERATION_KINDS))
        editor = next(op for op in plan["operations"] if op["kind"] == "open_editor")
        self.assertEqual(editor["params"]["files"], ["src/main.py"])
        ws = next(op for op in plan["operations"] if op["kind"] == "focus_workspace")
        self.assertTrue(ws["available"])
        self.assertEqual(ws["params"]["workspace"], "3")
        # Delegated handoff is available with a linked page even though no
        # Pi session is saved; params say so explicitly.
        agent = next(op for op in plan["operations"]
                     if op["kind"] == "open_project_agent")
        self.assertTrue(agent["available"])
        self.assertEqual(agent["params"]["page"], "Demo")
        self.assertFalse(agent["params"]["has_saved_session"])
        self.assertIsNone(agent["params"]["session_file"])
        self.assertTrue(agent["params"]["scope"].startswith(
            str(self.pi_base / "projects")))
        self.assertFalse(plan["pi_session"]["available"])

    def test_todos_verbatim_from_read_page(self):
        import project_planner as planner
        sentinel = {"line": 7, "task": "custom-item", "marker": "TODO",
                    "done": False, "extra": "kept-verbatim"}
        page = {"path": "pages/Demo.md", "page": "Demo", "graphName": "g",
                "revision": "r" * 64, "content": "file:: x\n",
                "todos": [sentinel,
                          {"line": 8, "task": "old", "marker": "DONE",
                           "done": True}]}
        with patch.object(planner, "read_page", return_value=page):
            plan = self._plan()
        self.assertEqual(plan["logseq"]["open_count"], 1)
        self.assertEqual(plan["logseq"]["total_count"], 2)
        self.assertEqual(plan["logseq"]["open_todos"], [sentinel])
        # No private reparse of content in the backend.
        source = (ROOT / "scripts" / "desktop_resume.py").read_text(
            encoding="utf-8")
        self.assertNotIn("_planner._task", source)
        self.assertNotIn("project_planner._task", source)

    def test_missing_apps_listed_in_plan(self):
        plan = self._plan(which=lambda name: None)
        by_kind = {op["kind"]: op for op in plan["operations"]}
        self.assertFalse(by_kind["focus_workspace"]["available"])
        self.assertIn("hyprctl", by_kind["focus_workspace"]["reason"])
        self.assertFalse(by_kind["open_editor"]["available"])
        self.assertIn("kitty", by_kind["open_editor"]["reason"])
        self.assertIn("nvim", by_kind["open_editor"]["reason"])
        self.assertFalse(by_kind["open_terminal"]["available"])
        self.assertIn("kitty", by_kind["open_terminal"]["reason"])
        self.assertFalse(by_kind["open_logseq_page"]["available"])
        self.assertIn("xdg-open", by_kind["open_logseq_page"]["reason"])
        # Delegated handoff needs no executable.
        self.assertTrue(by_kind["open_project_agent"]["available"])
        # Byte-stable given the same availability input.
        again = json.dumps(self._plan(which=lambda name: None),
                           sort_keys=True, separators=(",", ":"))
        first = json.dumps(plan, sort_keys=True, separators=(",", ":"))
        self.assertEqual(first, again)

    def test_byte_stable_over_unchanged_inputs(self):
        first = json.dumps(self._plan(), sort_keys=True, separators=(",", ":"))
        second = json.dumps(self._plan(), sort_keys=True, separators=(",", ":"))
        self.assertEqual(first, second)

    def test_no_project_content_mutation(self):
        before = {}
        for path in sorted(self.proj.rglob("*")):
            if path.is_file() and not path.is_symlink():
                before[str(path)] = (path.read_bytes(), path.stat().st_mtime_ns)
        plan = self._plan()
        spawned = []
        checked = []

        def fake_spawn(argv, cwd=None):
            spawned.append((list(argv), cwd))
            return None

        def fake_run(argv, timeout=5.0):
            checked.append(list(argv))
            if argv[0] == "hyprctl":
                return 0, "ok\n", ""
            return 0, "", ""

        with patch.object(resume, "_spawn_detached",
                          side_effect=fake_spawn), \
             patch.object(resume, "_run_checked", side_effect=fake_run), \
             patch.object(resume, "_which", return_value="/usr/bin/x"):
            out = resume.execute_operations(
                plan, run_checked=fake_run, spawn_detached=fake_spawn,
                which=lambda name: "/usr/bin/" + name)
        # Exact spawn behavior: one checked hyprctl (modern Lua dispatcher)
        # + one checked xdg-open, two detached kitty launches (editor with
        # list-form files, terminal).
        self.assertEqual(len(checked), 2)
        self.assertEqual(
            checked[0],
            ["hyprctl", "dispatch", 'hl.dsp.focus({workspace="3"})'])
        self.assertEqual(checked[1][0], "xdg-open")
        self.assertTrue(checked[1][1].startswith("logseq://"))
        self.assertEqual(len(spawned), 2)
        editor_argv = spawned[0][0]
        terminal_argv = spawned[1][0]
        self.assertEqual(editor_argv[:5],
                         ["kitty", "--directory", str(self.proj),
                          "--title", editor_argv[4]])
        self.assertIn("nvim", editor_argv)
        self.assertIn("--", editor_argv)
        self.assertIn("src/main.py", editor_argv)
        self.assertTrue(all(isinstance(a, str) for a in editor_argv))
        self.assertEqual(spawned[0][1], str(self.proj))
        self.assertEqual(terminal_argv[:2], ["kitty", "--directory"])
        self.assertEqual(spawned[1][1], str(self.proj))
        self.assertNotIn("nvim", terminal_argv)
        by_kind = {r["kind"]: r for r in out["results"]}
        self.assertEqual(by_kind["open_editor"]["status"], "launched")
        self.assertEqual(by_kind["open_terminal"]["status"], "launched")
        after = {}
        for path in sorted(self.proj.rglob("*")):
            if path.is_file() and not path.is_symlink():
                after[str(path)] = (path.read_bytes(), path.stat().st_mtime_ns)
        self.assertEqual(before, after)

    def test_renamed_registry_uses_current_name(self):
        listed = projects.list_projects(self.reg)
        projects.update_project(
            {"id": self.entry["id"], "revision": listed["revision"],
             "name": "Demo Renamed", "logseq_path": "pages/Demo.md",
             "local_folder": str(self.proj), "github_url": ""}, self.reg)
        plan = self._plan()
        # plan_for_project resolves by UUID identity; current name wins.
        self.assertEqual(plan["project"]["name"], "Demo Renamed")
        resolved = resume.resolve_identifier("demo renamed", self.reg)
        self.assertEqual(resolved["id"], self.entry["id"])

    def test_fetch_failures_degrade_with_warnings(self):
        import desktop_projects as dp
        with patch.object(resume, "_fetch_device_id",
                          side_effect=dp.DesktopError("boom device")), \
             patch.object(resume, "_fetch_search_sessions",
                          side_effect=AssertionError("no session fetch expected")), \
             patch.object(resume, "_fetch_session_resources",
                          side_effect=AssertionError("no resource fetch expected")), \
             patch.object(resume, "_fetch_last_activity",
                          side_effect=AssertionError("no activity fetch expected")):
            plan = resume.plan_for_project(
                self.entry["id"], registry_file=self.reg,
                desktop_bin="/bin/false", db="/tmp/qs-resume-test.db",
                graph=str(self.graph),
                which=lambda name: "/usr/bin/" + name)
        self.assertIsNone(plan["device_id"])
        self.assertIsNone(plan["session"])
        self.assertTrue(any("device identity unavailable" in w
                            for w in plan["warnings"]))

    def test_search_failure_degrades_with_warning(self):
        import desktop_projects as dp
        with patch.object(resume, "_fetch_device_id", return_value=DEV), \
             patch.object(resume, "_fetch_search_sessions",
                          side_effect=dp.DesktopError("boom search")), \
             patch.object(resume, "_fetch_session_resources",
                          side_effect=AssertionError("no resource fetch expected")), \
             patch.object(resume, "_fetch_last_activity",
                          side_effect=AssertionError("no activity fetch expected")):
            plan = resume.plan_for_project(
                self.entry["id"], registry_file=self.reg,
                desktop_bin="/bin/false", db="/tmp/qs-resume-test.db",
                graph=str(self.graph),
                which=lambda name: "/usr/bin/" + name)
        self.assertIsNone(plan["session"])
        self.assertTrue(any("work sessions unavailable: boom search" in w
                            for w in plan["warnings"]))

    def test_no_prior_session_graceful(self):
        plan = self._plan(device_id=DEV, sessions=[],
                          rollups=[], activity=None)
        self.assertIsNone(plan["session"])
        ws = next(op for op in plan["operations"] if op["kind"] == "focus_workspace")
        self.assertFalse(ws["available"])
        # Root mapping still exists: editor/terminal degrade to root-only.
        editor = next(op for op in plan["operations"] if op["kind"] == "open_editor")
        self.assertTrue(editor["available"])
        self.assertEqual(editor["params"]["files"], [])

    def test_no_logseq_todo_page(self):
        (self.graph / "pages" / "Demo.md").write_text(
            "file:: " + str(self.proj) + "\n", encoding="utf-8")
        plan = self._plan()
        self.assertTrue(plan["logseq"]["available"])
        self.assertEqual(plan["logseq"]["open_count"], 0)
        logseq_op = next(op for op in plan["operations"]
                         if op["kind"] == "open_logseq_page")
        self.assertTrue(logseq_op["available"])

    def test_missing_page_disables_logseq_op(self):
        (self.graph / "pages" / "Demo.md").unlink()
        plan = self._plan()
        self.assertFalse(plan["logseq"]["available"])
        logseq_op = next(op for op in plan["operations"]
                         if op["kind"] == "open_logseq_page")
        self.assertFalse(logseq_op["available"])

    def test_workspace_mismatch_and_invalid_names(self):
        other = make_activity("e" * 32, DEV, workspace="3")
        plan = self._plan(activity=other)
        ws = next(op for op in plan["operations"] if op["kind"] == "focus_workspace")
        self.assertFalse(ws["available"])
        self.assertIn("another session", ws["reason"])
        for bad, _hint in (("", "empty"), ("special:magic", "special"),
                           ("a;b", "unsafe"), ("x" * 65, "overlong")):
            plan = self._plan(activity=make_activity(self.sid, DEV, workspace=bad))
            ws = next(op for op in plan["operations"]
                      if op["kind"] == "focus_workspace")
            self.assertFalse(ws["available"], bad)

    def test_pi_scope_never_created(self):
        plan = self._plan()
        self.assertFalse(plan["pi_session"]["available"])
        # No scope directory may appear under the temp Pi base.
        self.assertEqual(list((self.pi_base / "projects").glob("*"))
                         if (self.pi_base / "projects").exists() else [], [])
        # The agent handoff still carries a derived (nonexistent) scope.
        agent = next(op for op in plan["operations"]
                     if op["kind"] == "open_project_agent")
        self.assertTrue(agent["available"])
        self.assertFalse(agent["params"]["has_saved_session"])
        self.assertFalse(Path(agent["params"]["scope"]).exists())

    def test_scoped_invocation_does_not_nest(self):
        os.environ["QS_PROJECT_SESSION_SCOPE"] = str(self.pi_base / "leaf-scope")
        os.environ["PI_CODING_AGENT_SESSION_DIR"] = str(self.pi_base / "leaf-scope")
        plan = self._plan()
        self.assertFalse(plan["pi_session"]["available"])
        self.assertFalse((self.pi_base / "leaf-scope" / "projects").exists())

    def test_scoped_leaf_reuses_custom_base(self):
        # A safe existing leaf reuses its parent as the projects base
        # (custom PI base honored, no nesting, nothing created).
        leaf = self.pi_base / "projects" / "current-leaf"
        leaf.mkdir(parents=True)
        before = sorted(p.name for p in (self.pi_base / "projects").iterdir())
        os.environ["QS_PROJECT_SESSION_SCOPE"] = str(leaf)
        os.environ["PI_CODING_AGENT_SESSION_DIR"] = str(leaf)
        plan = self._plan()
        agent = next(op for op in plan["operations"]
                     if op["kind"] == "open_project_agent")
        self.assertTrue(agent["available"])
        scope = Path(agent["params"]["scope"])
        self.assertEqual(scope.parent, leaf.parent)
        self.assertFalse(str(scope).startswith(str(Path.home() / ".local")))
        self.assertEqual(sorted(p.name for p in (self.pi_base / "projects").iterdir()),
                         before)
        self.assertFalse(plan["pi_session"]["available"])

    def test_scoped_leaf_same_target_resolves_saved_session(self):
        import hashlib
        import json as _json
        leaf = self.pi_base / "projects" / "current-leaf"
        leaf.mkdir(parents=True)
        os.environ["QS_PROJECT_SESSION_SCOPE"] = str(leaf)
        os.environ["PI_CODING_AGENT_SESSION_DIR"] = str(leaf)
        # Compute the expected digest for this graph+page and plant the
        # scope with a valid session header: same target, same digest.
        graph_key = str(self.graph.resolve(strict=True))
        digest = hashlib.sha256(
            f"{graph_key}\0pages/Demo.md".encode("utf-8")).hexdigest()
        scope = self.pi_base / "projects" / digest
        scope.mkdir(parents=True, exist_ok=True)
        header = {"type": "session", "id": "pi-sess-9", "version": 1,
                  "timestamp": "2024-01-01T00:00:00Z", "cwd": "/tmp"}
        (scope / "saved.jsonl").write_text(
            _json.dumps(header) + "\n", encoding="utf-8")
        plan = self._plan()
        self.assertTrue(plan["pi_session"]["available"])
        self.assertEqual(plan["pi_session"]["session_id"], "pi-sess-9")
        agent = next(op for op in plan["operations"]
                     if op["kind"] == "open_project_agent")
        self.assertTrue(agent["params"]["has_saved_session"])
        self.assertEqual(agent["params"]["session_file"],
                         str(scope / "saved.jsonl"))
        self.assertEqual(agent["params"]["session_id"], "pi-sess-9")

    def test_scoped_leaf_missing_falls_back(self):
        # Missing leaf: fall back to the configured PI base (setUp default).
        os.environ["QS_PROJECT_SESSION_SCOPE"] = str(self.pi_base / "no-such-leaf")
        plan = self._plan()
        agent = next(op for op in plan["operations"]
                     if op["kind"] == "open_project_agent")
        self.assertTrue(agent["available"])
        self.assertTrue(agent["params"]["scope"].startswith(
            str(self.pi_base / "projects")))
        self.assertFalse((self.pi_base / "no-such-leaf").exists())


class IndexedSessionLookupTests(unittest.TestCase):
    PID = "11111111-2222-4333-8444-555555555555"

    def test_search_receives_project_and_device_limit_one(self):
        import desktop_projects as dp
        local = make_session("1" * 32, self.PID, DEV, end=70)
        seen = {}

        def fake_search(binary, db, project, app, resource, device, query,
                        from_ms, to_ms, limit, timeout=None):
            seen.update(project=project, device=device, limit=limit)
            return {"query": {}, "sessions": [local], "count": 1}

        with patch.object(dp, "_fetch_search", side_effect=fake_search):
            row, reason = resume._fetch_current_device_session(
                "/bin/x", None, self.PID, DEV)
        self.assertEqual(seen["project"], self.PID)
        self.assertEqual(seen["device"], DEV)
        self.assertEqual(seen["limit"], 1)
        self.assertEqual(row["session_id"], "1" * 32)
        self.assertEqual(reason, "")

    def test_foreign_flood_cannot_hide_local(self):
        import desktop_projects as dp
        foreign = [make_session(f"{i:032x}"[-32:], self.PID, FOREIGN,
                                end=1000 + i)
                   for i in range(60)]
        local = make_session("1" * 32, self.PID, DEV, end=10)
        payload = {"query": {}, "sessions": foreign + [local], "count": 61}
        seen = {}

        def fake_search(binary, db, project, app, resource, device, query,
                        from_ms, to_ms, limit, timeout=None):
            seen.update(project=project, device=device, limit=limit)
            # Defense in depth: the real Rust search with limit=1 never
            # returns 61 rows; the Python filter must still hold if it did.
            return payload

        with patch.object(dp, "_fetch_search", side_effect=fake_search):
            row, reason = resume._fetch_current_device_session(
                "/bin/x", None, self.PID, DEV)
        self.assertEqual(seen["limit"], 1)
        self.assertEqual(seen["project"], self.PID)
        self.assertEqual(seen["device"], DEV)
        self.assertEqual(row["session_id"], "1" * 32)
        self.assertEqual(reason, "")

    def test_search_empty_is_no_session(self):
        import desktop_projects as dp
        with patch.object(dp, "_fetch_search",
                          return_value={"query": {}, "sessions": [], "count": 0}):
            row, reason = resume._fetch_current_device_session(
                "/bin/x", None, self.PID, DEV)
        self.assertIsNone(row)
        self.assertIn("no work session", reason)


class ExecuteTests(unittest.TestCase):
    def _plan(self, apps=None, root="/tmp/xyz"):
        entry = {"id": "11111111-2222-4333-8444-555555555555", "name": "Demo",
                 "logseq_path": "pages/Demo.md", "local_folder": "/tmp/xyz",
                 "github_url": ""}
        session = make_session("d" * 32, entry["id"], DEV)
        return resume.build_resume_plan(
            entry=entry, device_id=DEV, session_row=session,
            files=[{"relative": "src/main.py", "identity": "file:src/main.py",
                    "occurrence_count": 2, "last_seen_ms": 90}],
            repository={"available": True, "branch": "main",
                        "remote": None, "root_observed": "/tmp/xyz", "reason": ""},
            logseq_info={"path": "pages/Demo.md", "page": "Demo",
                         "graphName": "Notes", "revision": "r" * 64},
            open_todos=[], open_count=0, total_todos=0,
            pi={"available": True, "scope": "/tmp/scope",
                "session_file": "/tmp/scope/a.jsonl",
                "session_id": "pi-1", "reason": ""},
            root=root, workspace="3", warnings=[],
            app_availability=apps or {name: True for name in resume.APP_NAMES},
            agent_scope="/tmp/scope")

    def test_partial_failures_continue(self):
        plan = self._plan()
        calls = []

        def fake_run(argv, timeout=5.0):
            calls.append(list(argv))
            if argv[0] == "hyprctl":
                return 1, "", "boom"
            return 0, "", ""

        spawned = []

        def fake_spawn(argv, cwd=None):
            spawned.append((list(argv), cwd))
            return None

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "src").mkdir()
            (Path(tmp) / "src" / "main.py").write_text("x\n", encoding="utf-8")
            with patch.object(resume, "_which", return_value="/usr/bin/x"), \
                 patch("pathlib.Path.resolve", return_value=Path(tmp)):
                out = resume.execute_operations(
                    plan, run_checked=fake_run, spawn_detached=fake_spawn)
        by_kind = {r["kind"]: r for r in out["results"]}
        self.assertEqual(by_kind["focus_workspace"]["status"], "failed")
        # Both modern and legacy fail: the reason names both attempts.
        self.assertIn("modern exited 1", by_kind["focus_workspace"]["reason"])
        self.assertIn("legacy exited 1", by_kind["focus_workspace"]["reason"])
        # Detached GUI launches report launched (unconfirmed), not ok;
        # xdg-open uses bounded checked execution and reports failures.
        self.assertEqual(by_kind["open_editor"]["status"], "launched")
        self.assertEqual(by_kind["open_terminal"]["status"], "launched")
        self.assertEqual(by_kind["open_logseq_page"]["status"], "launched")
        self.assertEqual(by_kind["open_project_agent"]["status"], "delegated")
        self.assertEqual(out["count"], 5)
        # Fixed argv, list form, no shell strings: modern Lua dispatcher
        # first, then the legacy fallback for older Hyprland.
        hypr_calls = [a for a in calls if a[0] == "hyprctl"]
        xdg_calls = [a for a in calls if a[0] == "xdg-open"]
        self.assertEqual(len(hypr_calls), 2)
        self.assertEqual(len(xdg_calls), 1)
        for argv in calls:
            self.assertIsInstance(argv, list)
        # Numeric workspace uses the numeric selector directly in both forms.
        self.assertEqual(
            hypr_calls[0],
            ["hyprctl", "dispatch", 'hl.dsp.focus({workspace="3"})'])
        self.assertEqual(
            hypr_calls[1], ["hyprctl", "dispatch", "workspace", "3"])
        self.assertEqual(xdg_calls[0][0], "xdg-open")
        for argv, _cwd in spawned:
            self.assertIsInstance(argv, list)

    def test_operations_subset_validated(self):
        plan = self._plan()
        out = resume.execute_operations(
            plan, operations="open_terminal,focus_workspace",
            run_checked=lambda argv, timeout=5.0: (0, "ok\n", ""),
            spawn_detached=lambda argv, cwd=None: None,
            which=lambda name: "/usr/bin/" + name)
        self.assertEqual(out["operations"], ["focus_workspace", "open_terminal"])
        with self.assertRaises(resume.ResumeError):
            resume.execute_operations(plan, operations="rm_rf")
        with self.assertRaises(resume.ResumeError):
            resume.execute_operations(plan, operations="")

    def test_missing_apps_fail_gracefully(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = self._plan(root=tmp)
            out = resume.execute_operations(
                plan, run_checked=lambda argv, timeout=5.0: (0, "", ""),
                spawn_detached=lambda argv, cwd=None: None,
                which=lambda name: None)
        by_kind = {r["kind"]: r for r in out["results"]}
        self.assertEqual(by_kind["focus_workspace"]["status"], "failed")
        self.assertIn("hyprctl", by_kind["focus_workspace"]["reason"])
        self.assertEqual(by_kind["open_editor"]["status"], "failed")
        self.assertIn("kitty", by_kind["open_editor"]["reason"])
        self.assertEqual(by_kind["open_terminal"]["status"], "failed")
        self.assertEqual(by_kind["open_logseq_page"]["status"], "failed")
        self.assertEqual(by_kind["open_project_agent"]["status"], "delegated")

    def test_unknown_operation_objects_are_skipped(self):
        plan = self._plan()
        spawned = []

        def boom_spawn(argv, cwd=None):
            raise AssertionError(f"must not spawn: {argv}")

        bad_ops = [
            {"id": "evil", "kind": "evil", "available": True,
             "params": {}, "reason": ""},
            {"id": "open_editor", "kind": "open_terminal", "available": True,
             "params": {"root": "/tmp"}, "reason": ""},
        ]
        for operation in bad_ops:
            result = resume._execute_one(
                operation, plan, run_checked=lambda argv, timeout=5.0: (0, "", ""),
                spawn_detached=boom_spawn,
                which=lambda name: "/usr/bin/" + name)
            self.assertEqual(result["status"], "skipped", operation)
            self.assertIn("unknown operation", result["reason"])
        captured = []
        good = {"id": "open_terminal", "kind": "open_terminal", "available": True,
                "params": {"root": "/tmp"}, "reason": ""}
        result = resume._execute_one(
            good, plan, run_checked=lambda argv, timeout=5.0: (0, "", ""),
            spawn_detached=lambda argv, cwd=None: captured.append(list(argv)),
            which=lambda name: "/usr/bin/" + name)
        self.assertEqual(result["status"], "launched")
        self.assertEqual(len(captured), 1)

    def test_editor_exec_skips_sensitive_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "ok.py").write_text("x\n", encoding="utf-8")
            (root / ".env").write_text("SECRET=1\n", encoding="utf-8")
            operation = {"id": "open_editor", "kind": "open_editor",
                         "available": True, "reason": "",
                         "params": {"root": str(root),
                                    "files": ["src/ok.py", ".env"]}}
            plan = {"project": {"name": "Demo"}}
            captured = []

            def fake_spawn(argv, cwd=None):
                captured.append(list(argv))
                return None

            result = resume._execute_one(
                operation, plan,
                run_checked=lambda argv, timeout=5.0: (0, "", ""),
                spawn_detached=fake_spawn,
                which=lambda name: "/usr/bin/" + name)
            # One survivor + one sensitive exclusion -> partial with details.
            self.assertEqual(result["status"], "partial")
            self.assertIn("src/ok.py", captured[0])
            self.assertNotIn(".env", captured[0])
            # Selected file args stay list-form behind "--".
            self.assertIn("--", captured[0])
            self.assertEqual(captured[0][captured[0].index("--") + 1:],
                             ["src/ok.py"])
            self.assertTrue(any(s["file"] == ".env"
                                for s in result.get("skipped", [])))

    def test_unavailable_ops_are_skipped(self):
        plan = self._plan()
        plan["operations"][0]["available"] = False
        plan["operations"][0]["reason"] = "no work session"
        out = resume.execute_operations(
            plan, run_checked=lambda argv, timeout=5.0: (0, "", ""),
            spawn_detached=lambda argv, cwd=None: None,
            which=lambda name: "/usr/bin/" + name)
        first = next(r for r in out["results"] if r["kind"] == "focus_workspace")
        self.assertEqual(first["status"], "skipped")

    def test_safe_argv_no_shell(self):
        source = (ROOT / "scripts" / "desktop_resume.py").read_text(encoding="utf-8")
        self.assertIn("shell=False", source)
        self.assertNotIn("shell=True", source)
        self.assertNotIn("os.system", source)

    def test_workspace_selector_numeric_vs_literal(self):
        self.assertEqual(resume._workspace_selector("3"), "3")
        self.assertEqual(resume._workspace_selector("12"), "12")
        self.assertEqual(resume._workspace_selector("code"), "name:code")
        # Never pass raw dispatcher syntax for named workspaces.
        self.assertEqual(resume._workspace_selector("previous"),
                         "name:previous")
        self.assertEqual(resume._workspace_selector("My Proj-1.2"),
                         "name:My Proj-1.2")

    def test_workspace_execution_rederives_selector(self):
        captured = []

        def fake_run(argv, timeout=5.0):
            captured.append(list(argv))
            return 0, "ok\n", ""

        for literal, expected in (("3", "3"), ("code", "name:code"),
                                  ("previous", "name:previous")):
            captured.clear()
            op = {"id": "focus_workspace", "kind": "focus_workspace",
                  "available": True, "reason": "",
                  "params": {"workspace": literal}}
            result = resume._execute_one(
                op, {"project": {"name": "Demo"}},
                run_checked=fake_run,
                spawn_detached=lambda argv, cwd=None: None,
                which=lambda name: "/usr/bin/" + name)
            self.assertEqual(result["status"], "ok", literal)
            # Modern Lua dispatcher form succeeds first; legacy is not
            # attempted. The returned argv is the modern command that ran.
            modern = ["hyprctl", "dispatch",
                      f'hl.dsp.focus({{workspace="{expected}"}})']
            self.assertEqual(captured, [modern], literal)
            self.assertEqual(result.get("argv"), modern, literal)
            self.assertIsInstance(captured[0], list)

    def test_workspace_dispatch_argvs_numeric_and_named(self):
        modern, legacy = resume._workspace_dispatch_argvs("1")
        self.assertEqual(
            modern, ["hyprctl", "dispatch",
                     'hl.dsp.focus({workspace="1"})'])
        self.assertEqual(legacy, ["hyprctl", "dispatch", "workspace", "1"])
        modern_named, legacy_named = resume._workspace_dispatch_argvs("code")
        self.assertEqual(
            modern_named, ["hyprctl", "dispatch",
                           'hl.dsp.focus({workspace="name:code"})'])
        self.assertEqual(
            legacy_named, ["hyprctl", "dispatch", "workspace", "name:code"])
        # All argv elements stay plain strings in list form (no shell).
        for argv in (modern, legacy, modern_named, legacy_named):
            self.assertIsInstance(argv, list)
            self.assertTrue(all(isinstance(a, str) for a in argv))

    def test_workspace_fallback_modern_fails_legacy_succeeds(self):
        calls = []

        def fake_run(argv, timeout=5.0):
            calls.append(list(argv))
            if argv[2].startswith("hl.dsp.focus"):
                return 1, "", "unknown dispatcher"
            return 0, "ok", ""

        op = {"id": "focus_workspace", "kind": "focus_workspace",
              "available": True, "reason": "",
              "params": {"workspace": "code"}}
        result = resume._execute_one(
            op, {"project": {"name": "Demo"}},
            run_checked=fake_run,
            spawn_detached=lambda argv, cwd=None: None,
            which=lambda name: "/usr/bin/" + name)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            calls[0], ["hyprctl", "dispatch",
                       'hl.dsp.focus({workspace="name:code"})'])
        self.assertEqual(
            calls[1], ["hyprctl", "dispatch", "workspace", "name:code"])
        # The returned argv reflects the legacy command that succeeded.
        self.assertEqual(result.get("argv"), calls[1])

    def test_workspace_both_fail_reports_both_attempts(self):
        calls = []

        def fake_run(argv, timeout=5.0):
            calls.append(list(argv))
            if argv[2].startswith("hl.dsp.focus"):
                return 1, "", "modern boom"
            return 2, "", "legacy boom"

        op = {"id": "focus_workspace", "kind": "focus_workspace",
              "available": True, "reason": "",
              "params": {"workspace": "3"}}
        result = resume._execute_one(
            op, {"project": {"name": "Demo"}},
            run_checked=fake_run,
            spawn_detached=lambda argv, cwd=None: None,
            which=lambda name: "/usr/bin/" + name)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(calls), 2)
        self.assertIn("modern exited 1", result["reason"])
        self.assertIn("modern boom", result["reason"])
        self.assertIn("legacy exited 2", result["reason"])
        self.assertIn("legacy boom", result["reason"])
        self.assertNotIn("argv", result)

    def test_workspace_modern_zero_exit_error_falls_back_to_legacy(self):
        calls = []

        def fake_run(argv, timeout=5.0):
            calls.append(list(argv))
            if argv[2].startswith("hl.dsp.focus"):
                return 0, "error: unknown dispatcher\n", ""
            return 0, "ok\n", ""

        op = {"id": "focus_workspace", "kind": "focus_workspace",
              "available": True, "reason": "",
              "params": {"workspace": "code"}}
        result = resume._execute_one(
            op, {"project": {"name": "Demo"}},
            run_checked=fake_run,
            spawn_detached=lambda argv, cwd=None: None,
            which=lambda name: "/usr/bin/" + name)
        # Exit zero alone is not success: the modern stdout error is a
        # semantic rejection, so legacy is attempted and wins.
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(calls), 2)
        self.assertEqual(result.get("argv"), calls[1])
        self.assertEqual(
            calls[1], ["hyprctl", "dispatch", "workspace", "name:code"])

    def test_workspace_legacy_zero_exit_error_reports_both(self):
        calls = []

        def fake_run(argv, timeout=5.0):
            calls.append(list(argv))
            if argv[2].startswith("hl.dsp.focus"):
                return 1, "", "modern boom"
            return 0, "error: no such workspace\n", ""

        op = {"id": "focus_workspace", "kind": "focus_workspace",
              "available": True, "reason": "",
              "params": {"workspace": "3"}}
        result = resume._execute_one(
            op, {"project": {"name": "Demo"}},
            run_checked=fake_run,
            spawn_detached=lambda argv, cwd=None: None,
            which=lambda name: "/usr/bin/" + name)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(calls), 2)
        self.assertIn("modern exited 1", result["reason"])
        self.assertIn("modern boom", result["reason"])
        self.assertIn("legacy exited 0", result["reason"])
        self.assertIn("no such workspace", result["reason"])
        self.assertNotIn("argv", result)

    def test_workspace_both_zero_exit_errors_fail_closed(self):
        calls = []

        def fake_run(argv, timeout=5.0):
            calls.append(list(argv))
            if argv[2].startswith("hl.dsp.focus"):
                return 0, "error: modern bad\n", ""
            return 0, "error: legacy bad\n", ""

        op = {"id": "focus_workspace", "kind": "focus_workspace",
              "available": True, "reason": "",
              "params": {"workspace": "3"}}
        result = resume._execute_one(
            op, {"project": {"name": "Demo"}},
            run_checked=fake_run,
            spawn_detached=lambda argv, cwd=None: None,
            which=lambda name: "/usr/bin/" + name)
        # Unknown/empty output fails closed even with exit zero.
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(calls), 2)
        self.assertIn("modern exited 0", result["reason"])
        self.assertIn("modern bad", result["reason"])
        self.assertIn("legacy exited 0", result["reason"])
        self.assertIn("legacy bad", result["reason"])
        self.assertNotIn("argv", result)

    def test_workspace_stdout_only_failure_reports_stdout(self):
        calls = []

        def fake_run(argv, timeout=5.0):
            calls.append(list(argv))
            if argv[2].startswith("hl.dsp.focus"):
                return 1, "modern stdout only\n", ""
            return 2, "legacy stdout only\n", ""

        op = {"id": "focus_workspace", "kind": "focus_workspace",
              "available": True, "reason": "",
              "params": {"workspace": "3"}}
        result = resume._execute_one(
            op, {"project": {"name": "Demo"}},
            run_checked=fake_run,
            spawn_detached=lambda argv, cwd=None: None,
            which=lambda name: "/usr/bin/" + name)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(calls), 2)
        # Stderr is empty on both attempts: the bounded stdout text must
        # still appear alongside the exit status.
        self.assertIn("modern exited 1", result["reason"])
        self.assertIn("modern stdout only", result["reason"])
        self.assertIn("legacy exited 2", result["reason"])
        self.assertIn("legacy stdout only", result["reason"])
        self.assertNotIn("argv", result)

    def test_workspace_long_outputs_keep_both_attempts_bounded(self):
        calls = []

        def fake_run(argv, timeout=5.0):
            calls.append(list(argv))
            if argv[2].startswith("hl.dsp.focus"):
                return 1, "MODERN-OUT-" + "m" * 500 + "\n", \
                    "MODERN-ERR-" + "n" * 500 + "\n"
            return 2, "LEGACY-OUT-" + "l" * 500 + "\n", \
                "LEGACY-ERR-" + "e" * 500 + "\n"

        op = {"id": "focus_workspace", "kind": "focus_workspace",
              "available": True, "reason": "",
              "params": {"workspace": "3"}}
        result = resume._execute_one(
            op, {"project": {"name": "Demo"}},
            run_checked=fake_run,
            spawn_detached=lambda argv, cwd=None: None,
            which=lambda name: "/usr/bin/" + name)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(calls), 2)
        reason = result["reason"]
        # Both exit statuses survive the 300-char bound.
        self.assertIn("modern exited 1", reason)
        self.assertIn("legacy exited 2", reason)
        # Both stdout/stderr diagnostic summaries survive (clipped heads).
        self.assertIn("MODERN-OUT-", reason)
        self.assertIn("MODERN-ERR-", reason)
        self.assertIn("LEGACY-OUT-", reason)
        self.assertIn("LEGACY-ERR-", reason)
        self.assertLessEqual(len(reason), resume.MAX_REASON)
        self.assertNotIn("argv", result)

    def test_run_checked_decodes_byte_completed_process(self):
        completed = subprocess.CompletedProcess(
            args=["hyprctl", "dispatch"], returncode=0,
            stdout=b"ok\n", stderr=b"")
        with patch.object(resume.subprocess, "run",
                           return_value=completed) as mock_run:
            code, out, err = resume._run_checked(
                ["hyprctl", "dispatch", "workspace", "3"])
        mock_run.assert_called_once()
        self.assertEqual(code, 0)
        self.assertEqual(out, "ok\n")
        self.assertEqual(err, "")
        # The live byte shape trims to the recognized success token.
        self.assertTrue(resume._hyprctl_output_ok(out))
        self.assertFalse(resume._hyprctl_output_ok(""))
        self.assertFalse(resume._hyprctl_output_ok("error: nope\n"))

    def test_workspace_no_caller_controlled_lua_injection(self):
        seen = []

        def fake_run(argv, timeout=5.0):
            seen.append(list(argv))
            return 0, "ok", ""

        # A supplied selector string is never trusted: execution re-derives
        # the backend selector from the validated literal.
        op = {"id": "focus_workspace", "kind": "focus_workspace",
              "available": True, "reason": "",
              "params": {"workspace": "code",
                         "selector": 'x"}) .. os.execute("evil") --'}}
        result = resume._execute_one(
            op, {"project": {"name": "Demo"}},
            run_checked=fake_run,
            spawn_detached=lambda argv, cwd=None: None,
            which=lambda name: "/usr/bin/" + name)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(seen), 1)
        payload = seen[0][2]
        self.assertEqual(
            seen[0], ["hyprctl", "dispatch",
                      'hl.dsp.focus({workspace="name:code"})'])
        self.assertNotIn("os.execute", payload)
        self.assertNotIn("evil", payload)
        self.assertEqual(result.get("argv"), seen[0])
        # Unsafe literals never reach hyprctl: fail-closed skip.
        for bad in ('a"b', "a\\b", "a{b}c", "a;b", "a`b", "a$b",
                    'hl.dsp.focus({workspace="1"})'):
            bad_op = {"id": "focus_workspace", "kind": "focus_workspace",
                      "available": True, "reason": "",
                      "params": {"workspace": bad}}
            bad_result = resume._execute_one(
                bad_op, {"project": {"name": "Demo"}},
                run_checked=lambda argv, timeout=5.0: (_ for _ in ()).throw(
                    AssertionError(f"must not run hyprctl for {bad!r}")),
                spawn_detached=lambda argv, cwd=None: None,
                which=lambda name: "/usr/bin/" + name)
            self.assertEqual(bad_result["status"], "skipped", bad)

    def test_xdg_open_checked_failure_reported(self):
        op = {"id": "open_logseq_page", "kind": "open_logseq_page",
              "available": True, "reason": "",
              "params": {"uri": "logseq://graph/Notes?page=Demo"}}
        result = resume._execute_one(
            op, {"project": {"name": "Demo"}},
            run_checked=lambda argv, timeout=5.0: (1, "", "no handler"),
            spawn_detached=lambda argv, cwd=None: (_ for _ in ()).throw(
                AssertionError("xdg-open must use checked execution")),
            which=lambda name: "/usr/bin/" + name)
        self.assertEqual(result["status"], "failed")
        self.assertIn("xdg-open exited 1", result["reason"])

    def test_xdg_open_success_is_launched(self):
        seen = []

        def fake_run(argv, timeout=5.0):
            seen.append(list(argv))
            return 0, "", ""

        op = {"id": "open_logseq_page", "kind": "open_logseq_page",
              "available": True, "reason": "",
              "params": {"uri": "logseq://graph/Notes?page=Demo"}}
        result = resume._execute_one(
            op, {"project": {"name": "Demo"}},
            run_checked=fake_run,
            spawn_detached=lambda argv, cwd=None: (_ for _ in ()).throw(
                AssertionError("xdg-open must use checked execution")),
            which=lambda name: "/usr/bin/" + name)
        self.assertEqual(result["status"], "launched")
        self.assertEqual(seen[0], ["xdg-open", "logseq://graph/Notes?page=Demo"])

    def test_editor_partial_and_empty_never_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "keep.py").write_text("x\n", encoding="utf-8")
            plan = {"project": {"name": "Demo"}}
            # Some survive -> partial with skipped details.
            op = {"id": "open_editor", "kind": "open_editor",
                  "available": True, "reason": "",
                  "params": {"root": str(root),
                             "files": ["src/keep.py", "src/gone.py"]}}
            captured = []
            result = resume._execute_one(
                op, plan,
                run_checked=lambda argv, timeout=5.0: (0, "", ""),
                spawn_detached=lambda argv, cwd=None: captured.append(
                    list(argv)),
                which=lambda name: "/usr/bin/" + name)
            self.assertEqual(result["status"], "partial")
            self.assertIn("src/keep.py", captured[0])
            self.assertNotIn("gone", " ".join(captured[0]))
            self.assertTrue(any(s["file"] == "src/gone.py"
                                for s in result["skipped"]))
            # Nonempty planned list with no survivors -> failed, no spawn.
            op2 = {"id": "open_editor", "kind": "open_editor",
                   "available": True, "reason": "",
                   "params": {"root": str(root),
                              "files": ["src/gone.py"]}}

            def boom_spawn(argv, cwd=None):
                raise AssertionError(f"must not launch empty nvim: {argv}")

            result2 = resume._execute_one(
                op2, plan,
                run_checked=lambda argv, timeout=5.0: (0, "", ""),
                spawn_detached=boom_spawn,
                which=lambda name: "/usr/bin/" + name)
            self.assertEqual(result2["status"], "failed")
            self.assertIn("skipped", result2)
            # Intentionally zero files -> opening the root remains valid.
            op3 = {"id": "open_editor", "kind": "open_editor",
                   "available": True, "reason": "",
                   "params": {"root": str(root), "files": []}}
            captured3 = []
            result3 = resume._execute_one(
                op3, plan,
                run_checked=lambda argv, timeout=5.0: (0, "", ""),
                spawn_detached=lambda argv, cwd=None: captured3.append(
                    list(argv)),
                which=lambda name: "/usr/bin/" + name)
            self.assertEqual(result3["status"], "launched")
            self.assertEqual(len(captured3), 1)
            self.assertNotIn("--", captured3[0])

    def test_build_plan_is_pure_no_which_probe(self):
        entry = {"id": "11111111-2222-4333-8444-555555555555", "name": "Demo",
                 "logseq_path": "", "local_folder": "", "github_url": ""}
        with patch.object(resume.shutil, "which",
                          side_effect=AssertionError("must not probe PATH")):
            plan = resume.build_resume_plan(entry=entry, device_id=None)
        by_kind = {op["kind"]: op for op in plan["operations"]}
        self.assertFalse(by_kind["focus_workspace"]["available"])
        self.assertTrue(by_kind["focus_workspace"]["reason"])
        # Injected map is canonicalized to booleans without probing.
        plan2 = resume.build_resume_plan(
            entry=entry, device_id=None,
            app_availability={"kitty": 1, "nvim": "", "hyprctl": None,
                              "xdg-open": True, "extra": True})
        self.assertEqual(resume._canonical_app_availability(
            {"kitty": 1}), {"kitty": True, "nvim": False,
                            "hyprctl": False, "xdg-open": False})
        self.assertEqual(resume._canonical_app_availability(None),
                         {name: False for name in resume.APP_NAMES})


class FileRestorationSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "proj"
        (self.root / "src").mkdir(parents=True)
        (self.root / "src" / "a.py").write_text("x\n", encoding="utf-8")
        (self.root / "src" / "b.py").write_text("y\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def _rollup(self, identity, portable=True):
        if portable:
            return make_rollup("c" * 32, identity)
        return {"session_id": "c" * 32,
                "resource_key": f"local:{identity}", "kind": "local",
                "portable_identity": None, "local_identity": identity,
                "resource": {"adapter": "x"}, "occurrence_count": 1,
                "first_seen_ms": 1, "last_seen_ms": 50,
                "first_activity_id": 1, "last_activity_id": 2}

    def test_final_symlink_rejected(self):
        link = self.root / "src" / "link.py"
        try:
            link.symlink_to(self.root / "src" / "a.py")
        except OSError:
            self.skipTest("symlinks unavailable")
        files, unavailable = resume._select_files(
            [make_rollup("c" * 32, "file:src/link.py")], self.root, "")
        self.assertEqual(files, [])
        self.assertIn("symlink", unavailable[0]["reason"])

    def test_local_absolute_symlink_rejected_before_canonicalization(self):
        link = self.root / "src" / "local-link.py"
        try:
            link.symlink_to(self.root / "src" / "a.py")
        except OSError:
            self.skipTest("symlinks unavailable")
        item = self._rollup(f"file:{link}", portable=False)
        files, unavailable = resume._select_files([item], self.root, "")
        self.assertEqual(files, [])
        self.assertIn("symlink", unavailable[0]["reason"])

    def test_ancestor_symlink_rejected(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (outside / "evil.py").write_text("z\n", encoding="utf-8")
        linkdir = self.root / "src" / "linkdir"
        try:
            linkdir.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("symlinks unavailable")
        files, unavailable = resume._select_files(
            [make_rollup("c" * 32, "file:src/linkdir/evil.py")],
            self.root, "")
        self.assertEqual(files, [])
        self.assertTrue(
            "symlink" in unavailable[0]["reason"]
            or "outside" in unavailable[0]["reason"])

    def test_alias_into_git_and_protected_rejected(self):
        gitdir = self.root / ".git"
        gitdir.mkdir(exist_ok=True)
        (gitdir / "config").write_text("secret\n", encoding="utf-8")
        # Lexical .git path.
        files, unavailable = resume._select_files(
            [make_rollup("c" * 32, "file:.git/config")], self.root, "")
        self.assertEqual(files, [])
        self.assertIn("sensitive or protected", unavailable[0]["reason"])
        # Resolved alias: symlink dir into .git (lexical passes, resolved
        # is sensitive; ancestor-symlink policy also refuses).
        alias = self.root / "src" / "aliasdir"
        try:
            alias.symlink_to(gitdir, target_is_directory=True)
        except OSError:
            self.skipTest("symlinks unavailable")
        files2, unavailable2 = resume._select_files(
            [make_rollup("c" * 32, "file:src/aliasdir/config")],
            self.root, "")
        self.assertEqual(files2, [])
        self.assertTrue(unavailable2)
        # Protected .env lexical + resolved both refused.
        (self.root / ".env").write_text("S=1\n", encoding="utf-8")
        files3, unavailable3 = resume._select_files(
            [make_rollup("c" * 32, "file:.env")], self.root, "")
        self.assertEqual(files3, [])
        self.assertIn("sensitive or protected", unavailable3[0]["reason"])

    def test_symlink_loop_never_aborts(self):
        a = self.root / "src" / "loop_a"
        b = self.root / "src" / "loop_b"
        try:
            a.symlink_to(b)
            b.symlink_to(a)
        except OSError:
            self.skipTest("symlinks unavailable")
        items = [make_rollup("c" * 32, "file:src/loop_a"),
                 make_rollup("c" * 32, "file:src/a.py")]
        try:
            files, unavailable = resume._select_files(items, self.root, "")
        except Exception as exc:
            self.fail(f"_select_files raised on symlink loop: {exc}")
        rels = [f["relative"] for f in files]
        self.assertIn("src/a.py", rels)
        self.assertTrue(any("loop_a" in u["identity"] for u in unavailable))

    def test_bad_resource_runtime_error_is_unavailable(self):
        items = [make_rollup("c" * 32, "file:src/a.py")]
        with patch.object(Path, "resolve",
                          side_effect=RuntimeError("symlink loop")):
            try:
                files, unavailable = resume._select_files(
                    items, self.root, "")
            except Exception as exc:
                self.fail(f"_select_files raised RuntimeError: {exc}")
        self.assertEqual(files, [])
        self.assertTrue(unavailable)
        # is_excluded RuntimeError likewise never aborts.
        import project_files as _pf
        with patch.object(_pf, "is_excluded",
                          side_effect=RuntimeError("loop")):
            files2, unavailable2 = resume._select_files(items, self.root, "")
        self.assertEqual(files2, [])
        self.assertTrue(unavailable2)


class PiScopeSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.pi_base = self.base / "pibase"
        self.pi_base.mkdir()
        self._old_pi = os.environ.get("PI_CODING_AGENT_SESSION_DIR")
        self._old_scope = os.environ.get("QS_PROJECT_SESSION_SCOPE")
        os.environ["PI_CODING_AGENT_SESSION_DIR"] = str(self.pi_base)
        os.environ.pop("QS_PROJECT_SESSION_SCOPE", None)

    def tearDown(self):
        if self._old_pi is None:
            os.environ.pop("PI_CODING_AGENT_SESSION_DIR", None)
        else:
            os.environ["PI_CODING_AGENT_SESSION_DIR"] = self._old_pi
        if self._old_scope is None:
            os.environ.pop("QS_PROJECT_SESSION_SCOPE", None)
        else:
            os.environ["QS_PROJECT_SESSION_SCOPE"] = self._old_scope
        self.temp.cleanup()

    def test_missing_scope_is_clean_unavailable_no_create(self):
        scope = str(self.pi_base / "projects" / ("d" * 16))
        before = sorted(p.name for p in self.pi_base.iterdir())
        info = resume._pi_latest_in_scope(scope)
        self.assertFalse(info["available"])
        self.assertIn("no saved Pi session", info["reason"])
        self.assertEqual(sorted(p.name for p in self.pi_base.iterdir()), before)
        self.assertFalse(Path(scope).exists())

    def test_symlinked_scope_ancestor_rejected(self):
        real = self.pi_base / "real-projects"
        real.mkdir()
        link = self.pi_base / "projects"
        try:
            link.symlink_to(real, target_is_directory=True)
        except OSError:
            self.skipTest("symlinks unavailable")
        scope = str(link / ("e" * 16))
        (real / ("e" * 16)).mkdir()
        info = resume._pi_latest_in_scope(scope)
        self.assertFalse(info["available"])
        self.assertIn("unsafe", info["reason"])

    def test_symlinked_scope_leaf_rejected(self):
        projects_dir = self.pi_base / "projects"
        projects_dir.mkdir(exist_ok=True)
        target = self.pi_base / "elsewhere"
        target.mkdir()
        leaf = projects_dir / ("f" * 16)
        try:
            leaf.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("symlinks unavailable")
        info = resume._pi_latest_in_scope(str(leaf))
        self.assertFalse(info["available"])


class RustBoundaryTests(unittest.TestCase):
    BIN = ROOT / "services" / "agent-orchestrator" / "target" / "release" / "qs-desktop-context"
    PID = "11111111-2222-4333-8444-555555555555"

    def test_search_argv_passes_project_device_limit_one(self):
        import desktop_projects as dp
        seen = {}

        def fake_search(binary, db, project, app, resource, device, query,
                        from_ms, to_ms, limit, timeout=None):
            seen.update(project=project, device=device, limit=limit)
            return {"query": {}, "sessions": [], "count": 0}

        with patch.object(dp, "_fetch_search", side_effect=fake_search):
            row, reason = resume._fetch_current_device_session(
                "/bin/x", None, self.PID, DEV)
        self.assertEqual(seen["project"], self.PID)
        self.assertEqual(seen["device"], DEV)
        self.assertEqual(seen["limit"], 1)
        self.assertIsNone(row)
        self.assertIn("no work session", reason)

    def test_real_search_project_device_limit_one_no_create(self):
        if not (self.BIN.is_file() and os.access(self.BIN, os.X_OK)):
            self.skipTest("real qs-desktop-context binary not built")
        import desktop_projects as dp
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "missing.db")
            value = dp._fetch_search(
                self.BIN, missing, self.PID, None, None, DEV,
                None, None, None, 1)
            self.assertIsInstance(value, dict)
            self.assertEqual(value.get("sessions"), [])
            self.assertEqual(value.get("count"), 0)
            query = value.get("query", {})
            self.assertEqual(query.get("project"), self.PID)
            self.assertEqual(query.get("device"), DEV)
            self.assertEqual(query.get("limit"), 1)
            self.assertFalse(Path(missing).exists())
            # The indexed helper validates the single row before use.
            row, reason = resume.select_current_device_session(
                value.get("sessions"), self.PID, DEV)
            self.assertIsNone(row)
            self.assertIn("no work session", reason)


class DeviceIdTests(unittest.TestCase):
    BIN = ROOT / "services" / "agent-orchestrator" / "target" / "release" / "qs-desktop-context"

    def test_wrapper_canonicalizes_and_rejects(self):
        import desktop_projects as dp
        with patch.object(dp, "_run_desktop_cli",
                          return_value=b'"' + DEV.upper().encode() + b'"'):
            out = dp.device_id("/bin/x", None)
        self.assertEqual(out["device_id"], DEV)
        self.assertEqual(out["reason"], "")
        with patch.object(dp, "_run_desktop_cli", return_value=b"null"):
            out = dp.device_id("/bin/x", None)
        self.assertIsNone(out["device_id"])
        with patch.object(dp, "_run_desktop_cli", return_value=b"[]"):
            with self.assertRaises(dp.DesktopError):
                dp.device_id("/bin/x", None)

    def test_missing_db_reads_null_no_create(self):
        if not (self.BIN.is_file() and os.access(self.BIN, os.X_OK)):
            self.skipTest("real qs-desktop-context binary not built")
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "missing.db")
            import desktop_projects as dp
            out = dp.device_id(str(self.BIN), missing)
            self.assertIsNone(out["device_id"])
            self.assertFalse(Path(missing).exists())
            # CLI gate parity: device-id rejects every filter flag.
            for flag in ("--project", "--limit", "--session", "--from",
                         "--application", "--resource-limit", "--include-events"):
                value = "1" if flag in ("--limit", "--resource-limit") else "x"
                args = ["--desktop-bin", str(self.BIN), "device-id",
                        flag] + ([] if flag == "--include-events" else [value])
                rejected = subprocess.run(
                    [sys.executable, str(ROOT / "scripts" / "desktop_projects.py"),
                     *args],
                    text=True, capture_output=True, timeout=15, check=False)
                self.assertNotEqual(rejected.returncode, 0, flag)
                self.assertIn("does not accept", rejected.stderr, flag)


class CliErrorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.reg = str(Path(self.temp.name) / "r.toml")
        Path(self.reg).write_text("version = 1\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def assertCliError(self, args, *needles):
        completed = run_cli(args)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("error:", completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)
        self.assertEqual(completed.stdout, "")
        for needle in needles:
            self.assertIn(needle, completed.stderr)

    def test_plan_needs_project(self):
        self.assertCliError(["plan"], "--project")

    def test_unknown_project(self):
        self.assertCliError(
            ["--projects-file", self.reg, "plan", "--project", "Nope"], "unknown")

    def test_bad_operations(self):
        self.assertCliError(
            ["--projects-file", self.reg, "execute", "--project", "x",
             "--operations", "nope"], "unknown operation")

    def test_flag_gates(self):
        self.assertCliError(["list", "--project", "x"], "does not accept")
        self.assertCliError(["plan", "--project", "x", "--query", "y"],
                            "does not accept")
        self.assertCliError(["execute", "--project", "x", "--limit", "3"],
                            "does not accept")

    def test_stdout_json_only_on_success(self):
        completed = run_cli(["--projects-file", self.reg, "list", "--limit", "2"])
        self.assertEqual(completed.returncode, 0)
        payload = json.loads(completed.stdout)
        self.assertIn("entries", payload)


if __name__ == "__main__":
    unittest.main()
