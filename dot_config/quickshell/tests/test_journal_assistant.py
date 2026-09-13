import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import journal_assistant


class JournalAssistantTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.graph = Path(self.temp.name) / "graph"
        self.graph.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    @property
    def today(self):
        return datetime.date.today().strftime("%Y_%m_%d")

    def journal(self):
        return self.graph / "journals" / (self.today + ".md")

    def test_empty_and_pages_only_context_is_read_only(self):
        before = set(self.graph.iterdir())
        value = journal_assistant.context(self.graph)
        self.assertEqual(value["revision"], "missing")
        self.assertEqual(value["content"], "")
        self.assertEqual(value["pageNames"], [])
        self.assertEqual(set(self.graph.iterdir()), before)

        (self.graph / "pages").mkdir()
        (self.graph / "pages" / "Project___One.md").write_text(
            "# Project\n- link [[Café]]\n", encoding="utf-8")
        value = journal_assistant.context(self.graph, "café")
        self.assertEqual(value["pageNames"], ["Project/One"])
        self.assertEqual(value["matches"][0]["page"], "Project/One")
        self.assertFalse((self.graph / "journals").exists())

    def test_missing_and_empty_have_distinct_revisions_and_stale_prepare_fails(self):
        (self.graph / "journals").mkdir()
        missing = journal_assistant.context(self.graph)
        self.journal().write_bytes(b"")
        empty = journal_assistant.context(self.graph)
        self.assertNotEqual(missing["revision"], empty["revision"])
        self.assertEqual(empty["revision"], hashlib.sha256(b"").hexdigest())
        with self.assertRaises(journal_assistant.GraphError):
            journal_assistant.prepare(self.graph, self.today, missing["revision"], "new")

    def test_prepare_normalizes_and_append_preserves_raw_bytes_and_mode(self):
        (self.graph / "journals").mkdir()
        old = b"old\r\nraw-without-ending"
        self.journal().write_bytes(old)
        self.journal().chmod(0o640)
        current = journal_assistant.context(self.graph)
        prepared = journal_assistant.prepare(
            self.graph, self.today, current["revision"], "  pensée\r\n")
        self.assertEqual(prepared["addition"], "\n  pensée\n")
        result = journal_assistant.append(self.graph, self.today,
                                          prepared["revision"], prepared["addition"])
        expected = old + b"\n  pens\xc3\xa9e\n"
        self.assertEqual(self.journal().read_bytes(), expected)
        self.assertEqual(self.journal().stat().st_mode & 0o777, 0o640)
        self.assertEqual(result["line"], 3)
        self.assertEqual(result["revision"], hashlib.sha256(expected).hexdigest())

    def test_recent_sorting_query_unicode_and_caps(self):
        (self.graph / "pages").mkdir()
        (self.graph / "journals").mkdir()
        (self.graph / "pages" / "Unicode.md").write_text(
            "café project\n" + ("x" * 3000) + "\n", encoding="utf-8")
        for date, text in (("2024_01_01", "old"), ("2025_12_31", "new"),
                           (self.today, "today")):
            (self.graph / "journals" / (date + ".md")).write_text(text, encoding="utf-8")
        value = journal_assistant.context(self.graph, "café pasted thoughts")
        self.assertEqual([item["path"] for item in value["recentJournals"]],
                         ["journals/2025_12_31.md", "journals/2024_01_01.md"])
        self.assertNotIn(self.today, json.dumps(value["recentJournals"]))
        self.assertEqual(value["matches"][0]["text"], "café project")
        self.assertLessEqual(len(value["matches"]), journal_assistant.MATCH_LIMIT)
        self.assertLessEqual(
            len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()),
            journal_assistant.OUTPUT_LIMIT)

    def test_sensitive_markdown_stems_are_not_context(self):
        pages = self.graph / "pages"
        pages.mkdir()
        sensitive_stems = journal_assistant.SENSITIVE_NAMES | {".env", ".credentials"}
        for stem in sensitive_stems:
            for name in (stem + ".md", stem.upper() + ".MD"):
                (pages / name).write_text("do not expose", encoding="utf-8")
        (pages / "safe.md").write_text("safe", encoding="utf-8")

        value = journal_assistant.context(self.graph, "expose")

        self.assertEqual(value["pageNames"], ["safe"])
        self.assertEqual(value["matches"], [])
        self.assertNotIn("credentials", json.dumps(value).casefold())
        self.assertNotIn("secrets", json.dumps(value).casefold())

    def test_recent_candidates_keep_newest_bounded_excerpts(self):
        journals = self.graph / "journals"
        journals.mkdir()
        dates = [(datetime.date.today() - datetime.timedelta(days=days)).strftime(
            "%Y_%m_%d") for days in range(1, 8)]
        for date in dates:
            (journals / f"{date}.md").write_text(date, encoding="utf-8")

        value = journal_assistant.context(self.graph)

        self.assertEqual(
            [item["path"] for item in value["recentJournals"]],
            [f"journals/{date}.md" for date in dates[:5]])
        self.assertTrue(value["truncated"])
        self.assertEqual(value["recentJournals"][0]["content"], dates[0])

    def test_scan_budgets_bound_rejected_entries_depth_and_bytes(self):
        pages = self.graph / "pages"
        pages.mkdir()
        for number in range(5):
            (pages / f"rejected-{number}.txt").write_bytes(b"x" * 20)
        nested = pages
        for number in range(5):
            nested /= f"level-{number}"
            nested.mkdir()
        (nested / "deep.md").write_text("deep", encoding="utf-8")
        (pages / "after-budget.md").write_text("after", encoding="utf-8")
        byte_root = self.graph / "byte-budget"
        byte_root.mkdir()
        for number in range(3):
            (byte_root / f"rejected-{number}.txt").write_bytes(b"x" * 20)
        deep_root = self.graph / "deep-root"
        deep = deep_root
        for number in range(4):
            deep /= f"level-{number}"
            deep.mkdir(parents=number == 0)
        (deep / "too-deep.md").write_text("deep", encoding="utf-8")

        with mock.patch.multiple(
                journal_assistant, SCAN_ENTRY_LIMIT=3,
                SCAN_DIRECTORY_LIMIT=2, SCAN_DEPTH_LIMIT=1,
                SCAN_FILE_LIMIT=100, SCAN_BYTES_LIMIT=40):
            value = journal_assistant.context(self.graph)

        self.assertTrue(value["truncated"])
        self.assertLessEqual(len(value["pageNames"]), 500)
        self.assertLessEqual(len(value["matches"]), journal_assistant.MATCH_LIMIT)

        with mock.patch.multiple(
                journal_assistant, SCAN_ENTRY_LIMIT=100,
                SCAN_DIRECTORY_LIMIT=100, SCAN_DEPTH_LIMIT=1,
                SCAN_FILE_LIMIT=100, SCAN_BYTES_LIMIT=40):
            budget = journal_assistant._ScanBudget()
            root_fd = journal_assistant._open_directory(str(byte_root))
            try:
                list(journal_assistant._walk_markdown(
                    root_fd, "byte-budget", budget=budget))
            finally:
                os.close(root_fd)
            self.assertEqual(budget.entries, 3)
            self.assertEqual(budget.bytes, 60)
            self.assertTrue(budget.truncated)

            budget = journal_assistant._ScanBudget()
            root_fd = journal_assistant._open_directory(str(deep_root))
            try:
                self.assertEqual(list(journal_assistant._walk_markdown(
                    root_fd, "deep-root", budget=budget)), [])
            finally:
                os.close(root_fd)
            self.assertEqual(budget.directories, 2)
            self.assertTrue(budget.truncated)

    def test_scan_byte_reservation_handles_exact_and_over_remaining_files(self):
        roots = {}
        for name, files in (
                ("fits-exactly", (("exact.md", 16),)),
                ("leaves-less-than-own-size", (("first.md", 8), ("second.md", 12))),
                ("exceeds-remaining", (("too-large.md", 20),))):
            root = self.graph / name
            root.mkdir()
            for filename, size in files:
                (root / filename).write_bytes(b"x" * size)
            roots[name] = root

        def scan(root):
            budget = journal_assistant._ScanBudget()
            root_fd = journal_assistant._open_directory(str(root))
            try:
                found = list(journal_assistant._walk_markdown(
                    root_fd, root.name, budget=budget))
            finally:
                os.close(root_fd)
            return found, budget

        with mock.patch.multiple(
                journal_assistant, SCAN_ENTRY_LIMIT=100,
                SCAN_DIRECTORY_LIMIT=100, SCAN_DEPTH_LIMIT=2,
                SCAN_FILE_LIMIT=100, SCAN_BYTES_LIMIT=16):
            exact, exact_budget = scan(roots["fits-exactly"])
            partial, partial_budget = scan(roots["leaves-less-than-own-size"])
            over, over_budget = scan(roots["exceeds-remaining"])

        self.assertEqual(len(exact), 1)
        self.assertEqual(exact_budget.bytes, 16)
        self.assertFalse(exact_budget.truncated)
        self.assertEqual(len(partial), 1)
        self.assertEqual(partial_budget.bytes, 20)
        self.assertTrue(partial_budget.truncated)
        self.assertEqual(over, [])
        self.assertEqual(over_budget.bytes, 20)
        self.assertTrue(over_budget.truncated)

    def test_append_rejects_rollover_after_lock_without_writing(self):
        (self.graph / "journals").mkdir()
        tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).strftime(
            "%Y_%m_%d")
        with mock.patch.object(journal_assistant, "_today",
                               side_effect=[self.today, tomorrow]):
            with self.assertRaises(journal_assistant.GraphError):
                journal_assistant.append(self.graph, self.today, "missing", "x\n")
        self.assertFalse(self.journal().exists())
        self.assertEqual(list(self.journal().parent.glob(".journal-assistant-*.tmp")), [])

    def test_append_rechecks_rollover_immediately_before_replace(self):
        (self.graph / "journals").mkdir()
        tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).strftime(
            "%Y_%m_%d")
        with mock.patch.object(journal_assistant, "_today",
                               side_effect=[self.today, self.today, tomorrow]):
            with self.assertRaises(journal_assistant.GraphError):
                journal_assistant.append(self.graph, self.today, "missing", "x\n")
        self.assertFalse(self.journal().exists())
        self.assertEqual(list(self.journal().parent.glob(".journal-assistant-*.tmp")), [])

    def test_date_rollover_and_bounds_are_rejected(self):
        wrong = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%Y_%m_%d")
        with self.assertRaises(journal_assistant.GraphError):
            journal_assistant.prepare(self.graph, wrong, "missing", "x")
        with self.assertRaises(journal_assistant.GraphError):
            journal_assistant.prepare(self.graph, self.today, "missing", " \n\r")
        with self.assertRaises(journal_assistant.GraphError):
            journal_assistant.prepare(self.graph, self.today, "missing",
                                      "x" * (journal_assistant.JOURNAL_LIMIT + 1))

    def test_today_invalid_utf8_and_oversize_are_rejected(self):
        (self.graph / "journals").mkdir()
        self.journal().write_bytes(b"\xff")
        with self.assertRaises(journal_assistant.GraphError):
            journal_assistant.context(self.graph)
        self.journal().write_bytes(b"x" * (journal_assistant.JOURNAL_LIMIT + 1))
        with self.assertRaises(journal_assistant.GraphError):
            journal_assistant.prepare(self.graph, self.today, "missing", "x")

    def test_symlinks_fifos_and_lock_are_never_read_or_followed(self):
        outside = Path(self.temp.name) / "outside.md"
        outside.write_text("secret", encoding="utf-8")
        (self.graph / "pages").symlink_to(outside)
        with self.assertRaises(journal_assistant.GraphError):
            journal_assistant.context(self.graph)
        (self.graph / "pages").unlink()
        (self.graph / "journals").mkdir()
        os.mkfifo(self.journal())
        with self.assertRaises(journal_assistant.GraphError):
            journal_assistant.context(self.graph)
        self.journal().unlink()
        self.journal().symlink_to(outside)
        with self.assertRaises(journal_assistant.GraphError):
            journal_assistant.append(self.graph, self.today, "missing", "x\n")
        self.journal().unlink()
        lock = self.graph / "journals" / journal_assistant.LOCK_NAME
        lock.unlink()
        os.mkfifo(lock)
        with self.assertRaises(journal_assistant.GraphError):
            journal_assistant.append(self.graph, self.today, "missing", "x\n")

    def test_revision_conflict_and_concurrent_append_allow_one_writer(self):
        (self.graph / "journals").mkdir()
        revision = journal_assistant.context(self.graph)["revision"]
        results = []

        def write(value):
            try:
                results.append(journal_assistant.append(
                    self.graph, self.today, revision, value))
            except journal_assistant.GraphError as exc:
                results.append(exc)

        threads = [threading.Thread(target=write, args=(f"- {n}\n",))
                   for n in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(isinstance(item, dict) for item in results), 1)
        self.assertEqual(sum(isinstance(item, journal_assistant.GraphError)
                             for item in results), 1)

    def test_cli_json_contract_and_input_bound(self):
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "journal_assistant.py"),
             "--graph", str(self.graph), "context"],
            input=json.dumps({}), text=True, capture_output=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["revision"], "missing")
        too_large = json.dumps({"query": "x" * (journal_assistant.INPUT_LIMIT + 1)})
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "journal_assistant.py"),
             "--graph", str(self.graph), "context"],
            input=too_large, text=True, capture_output=True, check=False)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stdout, "")

    def test_lock_busy_is_bounded(self):
        (self.graph / "journals").mkdir()
        lock_path = self.graph / "journals" / journal_assistant.LOCK_NAME
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            with self.assertRaises(journal_assistant.GraphError):
                journal_assistant.append(self.graph, self.today, "missing", "x\n")
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


if __name__ == "__main__":
    unittest.main()
