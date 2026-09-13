import json
import os
import stat
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "project_sessions.py"


class ProjectSessionWrapperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.graph = self.root / "graph"
        (self.graph / "pages").mkdir(parents=True)
        (self.graph / "pages" / "Project.md").write_text("# project\n", encoding="utf-8")
        (self.graph / "pages" / "Other.md").write_text("# other\n", encoding="utf-8")
        self.env = dict(os.environ, LOGSEQ_GRAPH=str(self.graph), XDG_STATE_HOME=str(self.root / "state"))
        self.header = json.dumps({
            "type": "session", "version": 3, "id": "fixture-id",
            "timestamp": "2026-01-01T00:00:00.000Z", "cwd": str(ROOT),
        }) + "\n"

    def tearDown(self):
        self.temp.cleanup()

    def run_wrapper(self, project="pages/Project.md", *extra, env=None):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--project", project, *extra],
            env=env or self.env, text=True, capture_output=True, timeout=5,
        )

    def fake_pi(self):
        bindir = self.root / "bin"
        bindir.mkdir()
        fake = bindir / "pi"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "print(json.dumps({'argv': sys.argv[1:], 'session_dir': os.environ.get('PI_CODING_AGENT_SESSION_DIR'), 'scope': os.environ.get('QS_PROJECT_SESSION_SCOPE')}))\n",
            encoding="utf-8",
        )
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        return bindir

    def scope_from_output(self, completed):
        self.assertEqual(completed.returncode, 0, completed.stderr)
        value = json.loads(completed.stdout)
        return value["argv"], Path(value["session_dir"])

    def test_scope_is_deterministic_and_separates_graphs_and_pages(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("project_sessions", SCRIPT)
        if spec is None or spec.loader is None:
            self.fail("could not load project_sessions.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        graph = module.resolved_graph(str(self.graph))
        base = self.root / "base"
        first = module.scope_for(graph, module.project_relative_page(graph, "pages/Project.md"), base)
        same = module.scope_for(graph, module.project_relative_page(graph, "pages/Project.md"), base)
        other_page = module.scope_for(graph, module.project_relative_page(graph, "pages/Other.md"), base)
        other_graph = self.root / "other-graph"
        (other_graph / "pages").mkdir(parents=True)
        (other_graph / "pages" / "Project.md").write_text("other\n", encoding="utf-8")
        graph_scope = module.scope_for(
            module.resolved_graph(str(other_graph)),
            module.project_relative_page(module.resolved_graph(str(other_graph)), "pages/Project.md"),
            base,
        )
        self.assertEqual(first, same)
        self.assertNotEqual(first, other_page)
        self.assertNotEqual(first, graph_scope)
        self.assertEqual(first.parent, base)
        self.assertEqual(first.stat().st_mode & 0o777, 0o700)

    def test_wrapper_executes_scoped_pi_and_selects_latest_safe_session(self):
        bindir = self.fake_pi()
        env = dict(self.env, PATH=f"{bindir}:{os.environ['PATH']}")
        argv, scope = self.scope_from_output(self.run_wrapper(env=env))
        self.assertEqual(argv[:5], ["--mode", "rpc", "--approve", "--session-dir", str(scope)])
        self.assertNotIn("--continue", argv)
        self.assertTrue(scope.is_dir())
        # The extension uses this private marker to reject direct/general-pool
        # project launches instead of falling back to the palette directory.
        self.assertEqual(json.loads(self.run_wrapper(env=env).stdout)["scope"], str(scope))
        self.assertNotIn("--system-prompt", argv)
        self.assertNotIn("--model", argv)

        safe = scope / "safe.jsonl"
        safe.write_text(self.header, encoding="utf-8")
        unsafe_target = self.root / "foreign.jsonl"
        unsafe_target.write_text(self.header.replace("fixture-id", "foreign-id"), encoding="utf-8")
        unsafe = scope / "newer.jsonl"
        unsafe.symlink_to(unsafe_target)
        invalid = scope / "invalid.jsonl"
        invalid.write_text('{"type":"not-a-session"}\n', encoding="utf-8")
        now = safe.stat().st_mtime_ns
        os.utime(safe, ns=(now - 10_000, now - 10_000))
        os.utime(unsafe_target, ns=(now + 10_000, now + 10_000))
        os.utime(invalid, ns=(now + 20_000, now + 20_000))
        argv, _ = self.scope_from_output(self.run_wrapper(env=env))
        self.assertEqual(argv[-2:], ["--session", str(safe)])

    def test_valid_cached_session_is_explicit_and_missing_fresh_session_is_not_resumed(self):
        bindir = self.fake_pi()
        env = dict(self.env, PATH=f"{bindir}:{os.environ['PATH']}")
        _, scope = self.scope_from_output(self.run_wrapper(env=env))
        cached = scope / "saved.jsonl"
        cached.write_text(self.header, encoding="utf-8")

        argv, _ = self.scope_from_output(self.run_wrapper(
            "pages/Project.md", "--session", str(cached), "--pending-name", "stale UI name", env=env,
        ))
        self.assertEqual(argv[-2:], ["--session", str(cached)])
        self.assertNotIn("--name", argv)
        missing = scope / "new-not-flushed.jsonl"
        argv, _ = self.scope_from_output(self.run_wrapper(
            "pages/Project.md", "--session", str(missing), env=env,
        ))
        self.assertNotIn("--continue", argv)
        self.assertNotIn("--session", argv)

        argv, _ = self.scope_from_output(self.run_wrapper(
            "pages/Project.md", "--session", str(missing), "--pending-name", "  New   draft  ", env=env,
        ))
        self.assertEqual(argv[-2:], ["--name", "New draft"])

    def test_traversal_symlink_and_nonregular_cached_sessions_are_rejected(self):
        bindir = self.fake_pi()
        env = dict(self.env, PATH=f"{bindir}:{os.environ['PATH']}")
        _, scope = self.scope_from_output(self.run_wrapper(env=env))
        foreign = self.root / "foreign.jsonl"
        foreign.write_text("foreign\n", encoding="utf-8")
        link = scope / "link.jsonl"
        link.symlink_to(foreign)
        directory = scope / "dir.jsonl"
        directory.mkdir()
        for candidate in (str(foreign), str(link), str(directory)):
            completed = self.run_wrapper("pages/Project.md", "--session", candidate, env=env)
            self.assertNotEqual(completed.returncode, 0, candidate)
            self.assertIn("refused", completed.stderr)

        traversal = self.run_wrapper("../outside.md", env=env)
        self.assertNotEqual(traversal.returncode, 0)
        self.assertIn("relative", traversal.stderr)

    def test_configured_palette_pool_is_only_a_parent_for_project_subtrees(self):
        bindir = self.fake_pi()
        palette_pool = self.root / "palette-pool"
        env = dict(self.env, PATH=f"{bindir}:{os.environ['PATH']}", PI_CODING_AGENT_SESSION_DIR=str(palette_pool))
        _, scope = self.scope_from_output(self.run_wrapper(env=env))
        self.assertTrue(scope.is_relative_to(palette_pool / "projects"))
        self.assertEqual(list((palette_pool / "projects").iterdir()), [scope])

    @unittest.skipUnless(shutil.which("pi"), "pi is not installed")
    def test_installed_pi_uses_safe_older_session_not_newer_symlink(self):
        # This is deliberately control-only: startup, get_state, and
        # get_messages never invoke a provider/model.
        from tests.test_pi_rpc import JsonlReader, PiRpc
        import importlib.util

        spec = importlib.util.spec_from_file_location("project_sessions_real", SCRIPT)
        if spec is None or spec.loader is None:
            self.fail("could not load project_sessions.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        pool = self.root / "pool"
        graph = module.resolved_graph(str(self.graph))
        project = module.project_relative_page(graph, "pages/Project.md")
        scope = module.scope_for(graph, project, pool / "projects")
        safe = scope / "safe.jsonl"
        safe.write_text(self.header + json.dumps({
            "type": "message", "id": "safe-msg", "parentId": None,
            "timestamp": "2026-01-01T00:00:01.000Z",
            "message": {"role": "user", "content": "SAFE ONLY", "timestamp": 1},
        }) + "\n", encoding="utf-8")
        foreign = self.root / "foreign.jsonl"
        foreign.write_text(self.header.replace("fixture-id", "foreign-id") + json.dumps({
            "type": "message", "id": "foreign-msg", "parentId": None,
            "timestamp": "2026-01-01T00:00:01.000Z",
            "message": {"role": "user", "content": "FOREIGN MUST NOT LOAD", "timestamp": 1},
        }) + "\n", encoding="utf-8")
        unsafe = scope / "newer.jsonl"
        unsafe.symlink_to(foreign)
        old = safe.stat().st_mtime_ns
        os.utime(safe, ns=(old - 10_000, old - 10_000))
        os.utime(foreign, ns=(old + 10_000, old + 10_000))

        env = dict(self.env, LOGSEQ_GRAPH=str(self.graph),
                   PI_CODING_AGENT_SESSION_DIR=str(pool), QS_PROJECT_PATH="pages/Project.md",
                   PI_OFFLINE="1")
        process = subprocess.Popen(
            [sys.executable, str(SCRIPT), "--project", "pages/Project.md"],
            cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        reader = JsonlReader(process.stdout)
        reader.start()
        rpc = PiRpc(process, reader)
        try:
            state = rpc.request({"type": "get_state"}, timeout=15)
            self.assertEqual(Path(state["data"]["sessionFile"]), safe)
            messages = rpc.request({"type": "get_messages"}, timeout=15)
            values = json.dumps(messages.get("data", {}))
            self.assertIn("SAFE ONLY", values)
            self.assertNotIn("FOREIGN MUST NOT LOAD", values)
        finally:
            reader.stop()
            reader.join(timeout=5)
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            if process.stdout and not process.stdout.closed:
                process.stdout.close()
            if process.stderr and not process.stderr.closed:
                process.stderr.close()

    @unittest.skipUnless(shutil.which("pi"), "pi is not installed")
    def test_empty_rename_survives_intentional_pause_restart_without_prompt(self):
        from tests.test_pi_rpc import JsonlReader, PiRpc

        pool = self.root / "pool"
        env = dict(self.env, LOGSEQ_GRAPH=str(self.graph),
                   PI_CODING_AGENT_SESSION_DIR=str(pool), QS_PROJECT_PATH="pages/Project.md",
                   PI_OFFLINE="1")

        def launch(*extra):
            process = subprocess.Popen(
                [sys.executable, str(SCRIPT), "--project", "pages/Project.md", *extra],
                cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, bufsize=1,
            )
            reader = JsonlReader(process.stdout)
            reader.start()
            return process, reader, PiRpc(process, reader)

        def close(process, reader):
            reader.stop()
            reader.join(timeout=5)
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            if process.stdout and not process.stdout.closed:
                process.stdout.close()
            if process.stderr and not process.stderr.closed:
                process.stderr.close()

        process, reader, rpc = launch()
        try:
            state = rpc.request({"type": "get_state"}, timeout=15)
            session_file = Path(state["data"]["sessionFile"])
            self.assertFalse(session_file.exists())
            rpc.request({"type": "set_session_name", "name": "Empty draft",}, timeout=15)
            renamed = rpc.request({"type": "get_state"}, timeout=15)
            self.assertEqual(renamed["data"].get("sessionName"), "Empty draft")
            self.assertFalse(session_file.exists())
        finally:
            close(process, reader)

        restarted, restarted_reader, restarted_rpc = launch(
            "--session", str(session_file), "--pending-name", "Empty draft",
        )
        try:
            state = restarted_rpc.request({"type": "get_state"}, timeout=15)
            self.assertEqual(state["data"].get("sessionName"), "Empty draft")
            self.assertIsNone(restarted.poll())
        finally:
            close(restarted, restarted_reader)

    @unittest.skipUnless(shutil.which("pi"), "pi is not installed")
    def test_installed_pi_new_and_restore_are_control_only_and_scoped(self):
        from tests.test_pi_rpc import JsonlReader, PiRpc

        pool = self.root / "pool"
        scope = self.root / "scope"
        scope.mkdir(mode=0o700)
        fixture = scope / "saved.jsonl"
        fixture.write_text(self.header + json.dumps({
            "type": "message", "id": "saved-msg", "parentId": None,
            "timestamp": "2026-01-01T00:00:01.000Z",
            "message": {"role": "user", "content": "saved project context", "timestamp": 1},
        }) + "\n", encoding="utf-8")
        env = dict(self.env, LOGSEQ_GRAPH=str(self.graph),
                   PI_CODING_AGENT_SESSION_DIR=str(pool), QS_PROJECT_PATH="pages/Project.md",
                   PI_OFFLINE="1")
        # The wrapper's deterministic scope is needed for the fixture; invoke
        # it once with a fake-free launch only after deriving that path.
        import importlib.util
        spec = importlib.util.spec_from_file_location("project_sessions_new", SCRIPT)
        if spec is None or spec.loader is None:
            self.fail("could not load project_sessions.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        real_scope = module.scope_for(
            module.resolved_graph(str(self.graph)),
            module.project_relative_page(module.resolved_graph(str(self.graph)), "pages/Project.md"),
            pool / "projects",
        )
        real_fixture = real_scope / fixture.name
        real_fixture.write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")

        process = subprocess.Popen(
            [sys.executable, str(SCRIPT), "--project", "pages/Project.md"],
            cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        reader = JsonlReader(process.stdout)
        reader.start()
        rpc = PiRpc(process, reader)
        try:
            old = rpc.request({"type": "get_state"}, timeout=15)
            self.assertEqual(Path(old["data"]["sessionFile"]), real_fixture)
            new = rpc.request({"type": "new_session"}, timeout=15)
            self.assertTrue(new.get("success"))
            self.assertFalse((new.get("data") or {}).get("cancelled", False))
            changed = rpc.request({"type": "get_state"}, timeout=15)
            self.assertNotEqual(changed["data"].get("sessionId"), old["data"].get("sessionId"))
            self.assertEqual(rpc.request({"type": "get_messages"}, timeout=15)["data"].get("messages"), [])
            restored = rpc.request({"type": "switch_session", "sessionPath": str(real_fixture)}, timeout=15)
            self.assertTrue(restored.get("success"))
            current = rpc.request({"type": "get_state"}, timeout=15)
            self.assertEqual(Path(current["data"]["sessionFile"]), real_fixture)
            values = rpc.request({"type": "get_messages"}, timeout=15)["data"].get("messages", [])
            self.assertTrue(any("saved project context" in json.dumps(value) for value in values))
        finally:
            reader.stop()
            reader.join(timeout=5)
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            if process.stdout and not process.stdout.closed:
                process.stdout.close()
            if process.stderr and not process.stderr.closed:
                process.stderr.close()


if __name__ == "__main__":
    unittest.main()
