import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "journal_sessions.py"


class JournalSessionWrapperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.graph = self.root / "graph"
        (self.graph / "pages").mkdir(parents=True)
        (self.graph / "pages" / "Project.md").write_text("# project\n", encoding="utf-8")
        self.env = dict(os.environ, LOGSEQ_GRAPH=str(self.graph),
                        XDG_STATE_HOME=str(self.root / "state"))

    def tearDown(self):
        self.temp.cleanup()

    def fake_pi(self):
        bindir = self.root / "bin"
        bindir.mkdir()
        fake = bindir / "pi"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "print(json.dumps({'argv': sys.argv[1:], 'scope': os.environ.get('QS_JOURNAL_SESSION_SCOPE'), 'project': os.environ.get('QS_PROJECT_PATH'), 'project_scope': os.environ.get('QS_PROJECT_SESSION_SCOPE'), 'mode': os.environ.get('QS_JOURNAL_MODE'), 'graph': os.environ.get('LOGSEQ_GRAPH')}))\n",
            encoding="utf-8",
        )
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        return bindir

    def run_wrapper(self, *args, env=None):
        return subprocess.run([sys.executable, str(SCRIPT), *args], env=env or self.env,
                              text=True, capture_output=True, timeout=5)

    def test_graph_scope_is_deterministic_and_disjoint_from_project_scope(self):
        import importlib.util
        sys.path.insert(0, str(ROOT / "scripts"))
        spec = importlib.util.spec_from_file_location("journal_sessions_test", SCRIPT)
        self.assertIsNotNone(spec)
        assert spec is not None
        module = importlib.util.module_from_spec(spec)
        self.assertIsNotNone(spec.loader)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        graph = module.resolved_graph(str(self.graph))
        base = self.root / "pool"
        journal = module.journal_scope(graph, base)
        project = module._project.scope_for(
            graph, module._project.project_relative_page(graph, "pages/Project.md"), base / "projects")
        self.assertNotEqual(journal, project)
        self.assertEqual(journal.parent, base / "journals")
        self.assertEqual(journal, module.journal_scope(graph, base))
        self.assertEqual(journal.stat().st_mode & 0o777, 0o700)

    def test_wrapper_sets_journal_scope_clears_project_and_never_continues(self):
        bindir = self.fake_pi()
        env = dict(self.env, PATH=f"{bindir}:{os.environ['PATH']}", QS_PROJECT_PATH="pages/Project.md")
        completed = self.run_wrapper(env=env)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        value = json.loads(completed.stdout)
        self.assertEqual(value["mode"], "1")
        self.assertIsNone(value["project"])
        self.assertIsNone(value["project_scope"])
        self.assertEqual(Path(value["graph"]), self.graph.resolve())
        self.assertNotIn("--continue", value["argv"])
        self.assertEqual(value["argv"][:5], ["--mode", "rpc", "--approve", "--session-dir", value["scope"]])
        scope = Path(value["scope"])
        self.assertEqual(scope.parent.name, "journals")
        self.assertNotIn("projects", scope.parts)

    def test_symlink_graph_is_canonicalized_before_child_and_stays_stable_after_retarget(self):
        graph_a = self.root / "graph-a"
        graph_b = self.root / "graph-b"
        graph_c = self.root / "graph-c"
        for graph in (graph_b, graph_c):
            (graph / "pages").mkdir(parents=True)
            (graph / "pages" / "Page.md").write_text(graph.name, encoding="utf-8")
        graph_a.symlink_to(graph_b, target_is_directory=True)
        bindir = self.root / "bin-retarget"
        bindir.mkdir()
        fake = bindir / "pi"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os\n"
            "alias = os.environ['ALIAS']\n"
            "os.unlink(alias); os.symlink(os.environ['RETARGET'], alias, target_is_directory=True)\n"
            "print(json.dumps({'graph': os.environ['LOGSEQ_GRAPH']}))\n",
            encoding="utf-8",
        )
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        env = dict(self.env, LOGSEQ_GRAPH=str(graph_a), PATH=f"{bindir}:{os.environ['PATH']}",
                   ALIAS=str(graph_a), RETARGET=str(graph_c))
        completed = self.run_wrapper(env=env)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["graph"], str(graph_b.resolve()))

    def test_cached_session_is_revalidated_and_foreign_path_rejected(self):
        bindir = self.fake_pi()
        env = dict(self.env, PATH=f"{bindir}:{os.environ['PATH']}")
        first = json.loads(self.run_wrapper(env=env).stdout)
        scope = Path(first["scope"])
        header = json.dumps({"type": "session", "version": 3, "id": "id",
                             "timestamp": "2026-01-01T00:00:00Z", "cwd": str(ROOT)}) + "\n"
        cached = scope / "saved.jsonl"
        cached.write_text(header, encoding="utf-8")
        selected = json.loads(self.run_wrapper("--session", str(cached), env=env).stdout)
        self.assertEqual(selected["argv"][-2:], ["--session", str(cached)])
        foreign = self.root / "foreign.jsonl"
        foreign.write_text(header, encoding="utf-8")
        refused = self.run_wrapper("--session", str(foreign), env=env)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("refused", refused.stderr)


if __name__ == "__main__":
    unittest.main()
