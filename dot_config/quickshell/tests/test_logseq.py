import os
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from logseq_common import GraphError, graph_path
from logseq_graph import append_journal, search
from logseq_todos import main as todos_main, todos
import screen_capture


class LogseqTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.graph = Path(self.temp.name)
        (self.graph / "pages").mkdir()
        (self.graph / "journals").mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_search_and_todos_citations(self):
        page = self.graph / "pages" / "Project___One.md"
        page.write_text("- TODO: ship [url](https://example.test)\nneedle here\n- [x] NOW done\n")
        self.assertEqual(todos(graph_path(self.graph))[0]["page"], "Project/One")
        match = search(self.graph, "NEEDLE")[0]
        self.assertEqual((match["page"], match["line"], match["text"]), ("Project/One", 2, "needle here"))

    def test_todos_query_matches_task_page_and_marker_case_insensitively(self):
        (self.graph / "pages" / "Alpha.md").write_text("- TODO: ship it\n- NOW: wait\n")
        with patch("sys.stdout") as stdout:
            self.assertEqual(todos_main([str(self.graph), "--query", "ALPHA"]), 0)
        self.assertIn('"page": "Alpha"', stdout.write.call_args_list[0].args[0])
        self.assertEqual(len(todos(self.graph, "wait")), 1)
        self.assertEqual(len(todos(self.graph, "todo")), 1)

    def test_append_preserves_content_mode_and_date(self):
        journal = self.graph / "journals" / "2024_01_02.md"
        journal.write_text("old\n")
        journal.chmod(0o640)
        result = append_journal(self.graph, "- new", "2024_01_02")
        self.assertEqual(result["line"], 2)
        self.assertEqual(journal.read_text(), "old\n- new\n")
        self.assertEqual(journal.stat().st_mode & 0o777, 0o640)

    def test_append_line_numbers_for_empty_and_missing_newline(self):
        empty = append_journal(self.graph, "first", "2024_01_01")
        self.assertEqual(empty["line"], 1)
        self.assertEqual((self.graph / "journals" / "2024_01_01.md").read_text(), "first\n")
        no_newline = self.graph / "journals" / "2024_01_02.md"
        no_newline.write_text("old")
        result = append_journal(self.graph, "new", "2024_01_02")
        self.assertEqual(result["line"], 2)
        self.assertEqual(no_newline.read_text(), "old\nnew\n")

    def test_page_name_uses_direct_graph_roots(self):
        nested = self.graph / "journals" / "nested.md"
        nested.write_text("needle\n")
        self.assertEqual(search(self.graph, "needle")[0]["page"], "nested")
        graph_with_name = Path(self.temp.name) / "journals" / "graph"
        graph_with_name.mkdir()
        (graph_with_name / "pages").mkdir()
        page = graph_with_name / "pages" / "page.md"
        page.write_text("needle\n")
        self.assertEqual(search(graph_with_name, "needle")[0]["page"], "page")

    def test_rejects_bad_dates_and_symlink_journal_directory(self):
        with self.assertRaises(GraphError):
            append_journal(self.graph, "x", "2024_02_31")
        outside = Path(self.temp.name).parent / (Path(self.temp.name).name + "-outside")
        outside.mkdir(exist_ok=True)
        (self.graph / "journals").rmdir()
        (self.graph / "journals").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(GraphError):
            append_journal(self.graph, "x", "2024_01_01")
        outside.rmdir()

    def test_rejects_symlink_lock_and_nonregular_journal(self):
        outside = Path(self.temp.name).parent / (Path(self.temp.name).name + "-lock")
        outside.write_text("lock")
        (self.graph / "journals" / ".logseq_graph.lock").symlink_to(outside)
        with self.assertRaises(GraphError):
            append_journal(self.graph, "x", "2024_01_01")
        (self.graph / "journals" / ".logseq_graph.lock").unlink()
        (self.graph / "journals" / "2024_01_01.md").mkdir()
        with self.assertRaises(GraphError):
            append_journal(self.graph, "x", "2024_01_01")
        outside.unlink()

    def test_fifo_inputs_fail_without_blocking(self):
        blocked_page = self.graph / "pages" / "blocked.md"
        os.mkfifo(blocked_page)
        todos = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "logseq_todos.py"), str(self.graph)],
            check=False, text=True, capture_output=True, timeout=2,
        )
        self.assertEqual(todos.returncode, 0, todos.stderr)
        self.assertEqual(todos.stdout.strip(), "[]")
        blocked_page.unlink()

        lock = self.graph / "journals" / ".logseq_graph.lock"
        os.mkfifo(lock)
        append = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "logseq_graph.py"),
             "--graph", str(self.graph), "append", "--text", "- item", "--date", "2024_01_01"],
            check=False, text=True, capture_output=True, timeout=2,
        )
        self.assertEqual(append.returncode, 1, append.stderr)
        self.assertIn("unsafe", append.stderr)
        lock.unlink()

        blocked_journal = self.graph / "journals" / "2024_01_01.md"
        os.mkfifo(blocked_journal)
        append = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "logseq_graph.py"),
             "--graph", str(self.graph), "append", "--text", "- item", "--date", "2024_01_01"],
            check=False, text=True, capture_output=True, timeout=2,
        )
        self.assertEqual(append.returncode, 1, append.stderr)
        self.assertIn("unsafe", append.stderr)
        blocked_journal.unlink()

        control = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "logseq_graph.py"),
             "--graph", str(self.graph), "append", "--text", "- item", "--date", "2024_01_01"],
            check=False, text=True, capture_output=True, timeout=2,
        )
        self.assertEqual(control.returncode, 0, control.stderr)

    def test_concurrent_appends_are_serialized(self):
        errors = []
        def append(number):
            try:
                append_journal(self.graph, f"- item {number}", "2024_01_01")
            except Exception as exc:  # report failures from worker threads
                errors.append(exc)
        threads = [threading.Thread(target=append, args=(n,)) for n in range(8)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(errors, [])
        self.assertEqual((self.graph / "journals" / "2024_01_01.md").read_text().count("- item"), 8)

    def test_search_does_not_follow_file_symlinks(self):
        outside = Path(self.temp.name).parent / (Path(self.temp.name).name + "-secret")
        outside.write_text("needle")
        (self.graph / "pages" / "escape.md").symlink_to(outside)
        self.assertEqual(search(self.graph, "needle"), [])
        outside.unlink()

    @patch("screen_capture.shutil.which", side_effect=lambda name: "/bin/" + name)
    @patch("screen_capture.subprocess.Popen")
    def test_screen_capture_json_and_array_commands(self, popen, _which):
        png = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\r" + b"IHDR" + b"\x00" * 13
               + b"\x00" * 4 + b"\x00" * 4 + b"IEND" + b"\x00" * 4)
        selection = type("Process", (), {
            "pid": 4242,
            "returncode": 0,
            "poll": lambda self: 0,
            "communicate": lambda self, **kwargs: (b"0,0 1x1\n", b""),
        })()
        capture = type("Process", (), {
            "pid": 4243,
            "returncode": 0,
            "poll": lambda self: 0,
            "communicate": lambda self, **kwargs: (png, b""),
        })()
        popen.side_effect = [selection, capture]
        value = screen_capture.capture()
        self.assertEqual(value["images"][0]["mimeType"], "image/png")
        self.assertEqual(popen.call_args_list[0].args[0], ["/bin/slurp", "-d"])
        self.assertEqual(popen.call_args_list[1].args[0],
                         ["/bin/grim", "-g", "0,0 1x1", "-t", "png", "-"])
        self.assertTrue(popen.call_args_list[0].kwargs["start_new_session"])
        self.assertTrue(popen.call_args_list[1].kwargs["start_new_session"])


if __name__ == "__main__":
    unittest.main()
