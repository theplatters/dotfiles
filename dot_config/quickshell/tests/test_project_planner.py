import fcntl
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import project_planner


class ProjectPlannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.graph = Path(self.temp.name) / "graph"
        (self.graph / "pages" / "nested").mkdir(parents=True)
        (self.graph / "journals").mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def page(self, name="Work.md"):
        return self.graph / "pages" / name

    def test_list_is_graph_relative_nested_and_excludes_journals(self):
        self.page().write_text("not a task\n", encoding="utf-8")
        self.page("nested/Deep.md").write_text("- TODO deep\n", encoding="utf-8")
        self.page("large.md").write_bytes(b"x" * (project_planner.PAGE_LIMIT + 1))
        self.page("invalid.md").write_bytes(b"\xff")
        (self.graph / "journals" / "2026_09_09.md").write_text("journal\n", encoding="utf-8")
        result = project_planner.list_projects(self.graph)
        self.assertEqual(result["projects"], [
            {"path": "pages/nested/Deep.md", "page": "nested/Deep"},
            {"path": "pages/Work.md", "page": "Work"},
        ])

    def test_page_revision_and_task_scan_include_done_forms(self):
        content = "  - TODO: open\r\n- DONE closed\r\n- [x] checked\r\n- [ ] later\r\n  :PROPERTIES:\r\n"
        self.page().write_bytes(content.encode("utf-8"))
        result = project_planner.read_page(self.graph, "pages/Work.md")
        self.assertEqual(result["content"], content)
        self.assertEqual(result["revision"], hashlib.sha256(content.encode()).hexdigest())
        self.assertEqual(result["todos"], [
            {"line": 1, "task": "open", "marker": "TODO", "done": False},
            {"line": 2, "task": "closed", "marker": "DONE", "done": True},
            {"line": 3, "task": "checked", "marker": "", "done": True},
            {"line": 4, "task": "later", "marker": "", "done": False},
        ])

    def test_toggle_todo_and_checkbox_preserves_other_lines_and_newlines(self):
        before = "  - TODO ship\r\n- [ ] check\r\n  :PROPERTIES:\r\n  :foo: bar\r\n"
        self.page().write_bytes(before.encode())
        first = project_planner.read_page(self.graph, "pages/Work.md")
        completed = project_planner.toggle_task(
            self.graph, "pages/Work.md", first["revision"], 1, True)
        self.assertEqual(completed["content"],
                         "  - DONE ship\r\n- [ ] check\r\n  :PROPERTIES:\r\n  :foo: bar\r\n")
        reopened = project_planner.toggle_task(
            self.graph, "pages/Work.md", completed["revision"], 1, False)
        self.assertEqual(reopened["content"], before)
        checked = project_planner.toggle_task(
            self.graph, "pages/Work.md", reopened["revision"], 2, True)
        self.assertEqual(checked["content"],
                         "  - TODO ship\r\n- [x] check\r\n  :PROPERTIES:\r\n  :foo: bar\r\n")
        self.assertTrue(checked["todos"][1]["done"])
        self.assertEqual(self.page().read_bytes().count(b"\r\n"), 4)

    def test_update_preserves_mode_and_rejects_stale_revision(self):
        self.page().write_bytes(b"old\r\n")
        self.page().chmod(0o640)
        current = project_planner.read_page(self.graph, "pages/Work.md")
        updated = project_planner.update_page(
            self.graph, "pages/Work.md", current["revision"], "new\r\n")
        self.assertEqual(updated["content"], "new\r\n")
        self.assertEqual(self.page().stat().st_mode & 0o777, 0o640)
        with self.assertRaises(project_planner.GraphError):
            project_planner.update_page(self.graph, "pages/Work.md",
                                        current["revision"], "stale\n")

    def test_pages_only_graph_creates_safe_lock_directory_for_writes(self):
        bare = Path(self.temp.name) / "bare"
        (bare / "pages").mkdir(parents=True)
        page = bare / "pages" / "Work.md"
        page.write_text("- TODO task\n", encoding="utf-8")
        current = project_planner.read_page(bare, "pages/Work.md")
        project_planner.update_page(bare, "pages/Work.md", current["revision"],
                                    "- DONE task\n")
        self.assertEqual(page.read_text(encoding="utf-8"), "- DONE task\n")
        self.assertTrue((bare / "journals" / project_planner.LOCK_NAME).is_file())

    def test_path_symlink_fifo_and_size_are_rejected_without_following(self):
        outside = Path(self.temp.name) / "outside.md"
        outside.write_text("secret\n", encoding="utf-8")
        self.page().symlink_to(outside)
        with self.assertRaises(project_planner.GraphError):
            project_planner.read_page(self.graph, "pages/Work.md")
        self.page().unlink()

        (self.graph / "pages" / "link").symlink_to(self.graph / "pages" / "nested",
                                                     target_is_directory=True)
        with self.assertRaises(project_planner.GraphError):
            project_planner.read_page(self.graph, "pages/link/Work.md")
        (self.graph / "pages" / "link").unlink()

        os.mkfifo(self.page())
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "project_planner.py"),
             "--graph", str(self.graph), "page"],
            input=json.dumps({"path": "pages/Work.md"}), text=True,
            capture_output=True, timeout=2, check=False,
        )
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stdout, "")
        self.page().unlink()
        self.page().write_bytes(b"x" * (project_planner.PAGE_LIMIT + 1))
        with self.assertRaises(project_planner.GraphError):
            project_planner.read_page(self.graph, "pages/Work.md")

        with self.assertRaises(project_planner.GraphError):
            project_planner.read_page(self.graph, "pages/../journals/Work.md")
        self.page().write_bytes(b"\xff\xfe")
        with self.assertRaises(project_planner.GraphError):
            project_planner.read_page(self.graph, "pages/Work.md")

    def test_stale_check_happens_before_replace_and_lock_fifo_is_safe(self):
        self.page().write_text("- TODO task\n", encoding="utf-8")
        current = project_planner.read_page(self.graph, "pages/Work.md")
        original = project_planner._replace_page

        def change_then_replace(graph, relative, expected, replacement):
            self.page().write_text("- TODO changed\n", encoding="utf-8")
            return original(graph, relative, expected, replacement)

        with patch.object(project_planner, "_replace_page", change_then_replace):
            with self.assertRaises(project_planner.GraphError):
                project_planner.update_page(self.graph, "pages/Work.md",
                                            current["revision"], "new\n")
        self.assertEqual(self.page().read_text(encoding="utf-8"), "- TODO changed\n")

        (self.graph / "journals" / project_planner.LOCK_NAME).unlink()
        os.mkfifo(self.graph / "journals" / project_planner.LOCK_NAME)
        with self.assertRaises(project_planner.GraphError):
            project_planner.update_page(self.graph, "pages/Work.md",
                                        project_planner.read_page(self.graph, "pages/Work.md")["revision"],
                                        "new\n")

    def test_relocated_parent_is_rejected_before_replace(self):
        self.page().write_text("old\n", encoding="utf-8")
        current = project_planner.read_page(self.graph, "pages/Work.md")
        original = project_planner._open_page_parent_context
        calls = 0

        def relocate_on_final_check(graph, relative):
            nonlocal calls
            calls += 1
            if calls == 2:
                (self.graph / "pages").rename(self.graph / "pages-old")
                (self.graph / "pages").mkdir()
            return original(graph, relative)

        with patch.object(project_planner, "_open_page_parent_context",
                          relocate_on_final_check):
            with self.assertRaises(project_planner.GraphError):
                project_planner.update_page(self.graph, "pages/Work.md",
                                            current["revision"], "new\n")
        self.assertEqual((self.graph / "pages-old" / "Work.md").read_text(), "old\n")
        self.assertFalse((self.graph / "pages" / "Work.md").exists())

    def test_concurrent_writes_allow_one_revision_and_reject_the_other(self):
        self.page().write_text("- TODO task\n", encoding="utf-8")
        revision = project_planner.read_page(self.graph, "pages/Work.md")["revision"]
        results = []

        def update(value):
            try:
                results.append(project_planner.update_page(
                    self.graph, "pages/Work.md", revision, value))
            except project_planner.GraphError as exc:
                results.append(exc)

        threads = [threading.Thread(target=update, args=("one\n",)),
                   threading.Thread(target=update, args=("two\n",))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(isinstance(value, dict) for value in results), 1)
        self.assertEqual(sum(isinstance(value, project_planner.GraphError)
                             for value in results), 1)

    def test_input_and_page_output_are_bounded(self):
        old = sys.stdin
        try:
            sys.stdin = type("Input", (), {"buffer": self._BytesReader(
                b"x" * (project_planner.INPUT_LIMIT + 1))})()
            with self.assertRaises(project_planner.GraphError):
                project_planner._read_input()
        finally:
            sys.stdin = old

    def test_cli_accepts_escaped_transport_and_quoted_nonascii_paths(self):
        quoted = self.graph / "pages" / 'Quó"ted.md'
        quoted.write_text("- TODO café\n", encoding="utf-8")
        payload = json.dumps({"path": "pages/Quó\"ted.md"}, ensure_ascii=False)
        page = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "project_planner.py"),
             "--graph", str(self.graph), "page"],
            input=payload, text=True, capture_output=True, check=False,
        )
        self.assertEqual(page.returncode, 0, page.stderr)
        self.assertEqual(json.loads(page.stdout)["content"], "- TODO café\n")
        self.assertEqual(page.stdout.count("\n"), 1)

        self.page().write_bytes(b"old\n")
        old_page = project_planner.read_page(self.graph, "pages/Work.md")
        replacement = "é" * (project_planner.PAGE_LIMIT // 2)
        update = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "project_planner.py"),
             "--graph", str(self.graph), "update"],
            input=json.dumps({"path": old_page["path"], "revision": old_page["revision"],
                              "content": replacement}),
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(update.returncode, 0, update.stderr)
        self.assertEqual(json.loads(update.stdout)["content"], replacement)
        self.assertEqual(len(self.page().read_bytes()), project_planner.PAGE_LIMIT)

    def test_cli_lock_busy_fails_bounded_without_mutation(self):
        self.page().write_text("- TODO task\n", encoding="utf-8")
        current = project_planner.read_page(self.graph, "pages/Work.md")
        lock_path = self.graph / "journals" / project_planner.LOCK_NAME
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            completed = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "project_planner.py"),
                 "--graph", str(self.graph), "update"],
                input=json.dumps({"path": current["path"], "revision": current["revision"],
                                  "content": "changed\n"}),
                text=True, capture_output=True,
                timeout=project_planner.LOCK_TIMEOUT + 2, check=False,
            )
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        self.assertEqual(completed.returncode, 1)
        self.assertIn("busy", completed.stderr)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(self.page().read_text(encoding="utf-8"), "- TODO task\n")

    class _BytesReader:
        def __init__(self, value):
            self.value = value

        def read(self, _limit):
            return self.value


if __name__ == "__main__":
    unittest.main()
