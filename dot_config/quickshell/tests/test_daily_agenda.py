import hashlib
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
import daily_agenda
import project_planner


class AgendaCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.graph = Path(self.temp.name) / "graph"
        (self.graph / "pages").mkdir(parents=True)
        (self.graph / "journals").mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name="Work.md", content="- TODO alpha\n"):
        path = self.graph / "pages" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def read(self, name="Work.md"):
        return project_planner.read_page(self.graph, f"pages/{name}")

    def cli(self, command, payload, raw=None):
        argv = [sys.executable, str(ROOT / "scripts" / "daily_agenda.py"),
                "--graph", str(self.graph), command]
        data = raw if raw is not None else json.dumps(payload)
        return subprocess.run(argv, input=data, text=True, capture_output=True,
                              check=False)


class ListTests(AgendaCase):
    def test_list_open_tasks_and_done_scheduled_only(self):
        self.write(content="- TODO open\n- DONE finished\n- TODO scheduled\n")
        current = self.read()
        daily_agenda.select_task(self.graph, current["path"], current["revision"],
                                 3, "2026-09-13", True)
        current = self.read()
        daily_agenda.select_task(self.graph, current["path"], current["revision"],
                                 2, "2026-09-13", True)
        result = daily_agenda.list_agenda(self.graph, "2026-09-13")
        by_line = {entry["line"]: entry for entry in result["tasks"]}
        self.assertEqual(result["date"], "2026-09-13")
        self.assertFalse(result["truncated"])
        # Open task line 1 included unscheduled.
        self.assertEqual(by_line[1]["scheduledDate"], "")
        self.assertFalse(by_line[1]["done"])
        # Done task scheduled on the date is included.
        self.assertTrue(any(e["done"] and e["scheduledDate"] == "2026-09-13"
                            for e in result["tasks"]))
        # Done unscheduled task on another date is excluded.
        other = daily_agenda.list_agenda(self.graph, "2026-09-14")
        self.assertFalse(any(e["done"] for e in other["tasks"]))

    def test_list_excludes_journals_and_reports_revision(self):
        self.write(content="- TODO work\n")
        (self.graph / "journals" / "2026_09_13.md").write_text("- TODO journal\n",
                                                              encoding="utf-8")
        result = daily_agenda.list_agenda(self.graph, "2026-09-13")
        self.assertEqual(len(result["tasks"]), 1)
        entry = result["tasks"][0]
        self.assertEqual(entry["path"], "pages/Work.md")
        self.assertEqual(entry["page"], "Work")
        self.assertEqual(entry["task"], "work")
        self.assertEqual(entry["marker"], "TODO")
        self.assertEqual(entry["scheduledDate"], "")
        self.assertEqual(entry["revision"], self.read()["revision"])

    def test_list_prioritizes_scheduled_when_truncated(self):
        self.write(content="".join(f"- TODO task{i:03d}\n" for i in range(20)))
        current = self.read()
        # Schedule the very last task; it must survive a tiny cap.
        daily_agenda.select_task(self.graph, current["path"], current["revision"],
                                 20, "2026-09-13", True)
        with patch.object(daily_agenda, "AGENDA_LIMIT", 5):
            result = daily_agenda.list_agenda(self.graph, "2026-09-13")
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["tasks"]), 5)
        self.assertEqual(result["tasks"][0]["line"], 20)
        self.assertEqual(result["tasks"][0]["scheduledDate"], "2026-09-13")

    def test_list_truncation_flag_false_when_within_cap(self):
        self.write(content="- TODO one\n")
        with patch.object(daily_agenda, "AGENDA_LIMIT", 10):
            result = daily_agenda.list_agenda(self.graph, "2026-09-13")
        self.assertFalse(result["truncated"])
        self.assertEqual(len(result["tasks"]), 1)

    def test_list_ignores_invalid_agenda_value(self):
        self.write(content="- TODO task\n  quickshell-agenda:: not-a-date\n")
        result = daily_agenda.list_agenda(self.graph, "2026-09-13")
        self.assertEqual(result["tasks"][0]["scheduledDate"], "")

    def test_list_rejects_malformed_dates(self):
        for bad in ("2026-13-01", "2026-02-30", "13-09-2026", "2026/09/13",
                    "", None, 20260913, True, " 2026-09-13"):
            with self.assertRaises(project_planner.GraphError, msg=repr(bad)):
                daily_agenda.list_agenda(self.graph, bad)

    def test_list_skips_oversize_and_symlink_pages(self):
        self.write(content="- TODO ok\n")
        big = self.graph / "pages" / "Big.md"
        big.write_bytes(b"x" * (project_planner.PAGE_LIMIT + 1))
        link = self.graph / "pages" / "Link.md"
        outside = Path(self.temp.name) / "outside.md"
        outside.write_text("- TODO secret\n", encoding="utf-8")
        try:
            link.symlink_to(outside)
        except OSError:
            pass
        result = daily_agenda.list_agenda(self.graph, "2026-09-13")
        self.assertEqual([t["path"] for t in result["tasks"]], ["pages/Work.md"])


class SelectTests(AgendaCase):
    def test_select_inserts_property_before_child(self):
        self.write(content="- TODO parent\n  - TODO child\n")
        current = self.read()
        out = daily_agenda.select_task(self.graph, current["path"],
                                       current["revision"], 1,
                                       "2026-09-13", True)
        content = out["page"]["content"]
        self.assertEqual(content,
                          "- TODO parent\n"
                          "  quickshell-agenda:: 2026-09-13\n"
                          "  - TODO child\n")
        listed = daily_agenda.list_agenda(self.graph, "2026-09-13")
        parent = [e for e in listed["tasks"] if e["line"] == 1][0]
        child = [e for e in listed["tasks"] if e["task"] == "child"][0]
        self.assertEqual(parent["scheduledDate"], "2026-09-13")
        self.assertEqual(child["scheduledDate"], "")

    def test_select_child_does_not_leak_to_parent(self):
        self.write(content="- TODO parent\n  - TODO child\n")
        current = self.read()
        daily_agenda.select_task(self.graph, current["path"], current["revision"],
                                 2, "2026-09-14", True)
        listed = daily_agenda.list_agenda(self.graph, "2026-09-14")
        by_task = {e["task"]: e for e in listed["tasks"]}
        self.assertEqual(by_task["child"]["scheduledDate"], "2026-09-14")
        self.assertEqual(by_task["parent"]["scheduledDate"], "")

    def test_select_updates_existing_and_dedupes(self):
        self.write(content="- TODO task\n"
                           "  quickshell-agenda:: 2026-09-13\n"
                           "  quickshell-agenda:: 2026-09-14\n")
        current = self.read()
        out = daily_agenda.select_task(self.graph, current["path"],
                                       current["revision"], 1,
                                       "2026-09-20", True)
        self.assertEqual(out["page"]["content"].count("quickshell-agenda::"), 1)
        self.assertIn("quickshell-agenda:: 2026-09-20", out["page"]["content"])
        listed = daily_agenda.list_agenda(self.graph, "2026-09-20")
        self.assertEqual(listed["tasks"][0]["scheduledDate"], "2026-09-20")

    def test_deselect_removes_property_and_keeps_siblings(self):
        self.write(content="- TODO keep\n"
                           "  quickshell-agenda:: 2026-09-13\n"
                           "  alias:: demo\n"
                           "- TODO other\n")
        current = self.read()
        out = daily_agenda.select_task(self.graph, current["path"],
                                       current["revision"], 1,
                                       "2026-09-13", False)
        self.assertNotIn("quickshell-agenda", out["page"]["content"])
        self.assertIn("alias:: demo", out["page"]["content"])
        self.assertIn("- TODO other", out["page"]["content"])

    def test_deselect_unscheduled_is_noop_success(self):
        self.write(content="- TODO plain\n")
        current = self.read()
        out = daily_agenda.select_task(self.graph, current["path"],
                                       current["revision"], 1,
                                       "2026-09-13", False)
        self.assertEqual(out["page"]["content"], "- TODO plain\n")

    def test_select_preserves_tab_indentation(self):
        self.write(content="\t- TODO tabbed\n\t  - TODO kid\n")
        current = self.read()
        out = daily_agenda.select_task(self.graph, current["path"],
                                       current["revision"], 1,
                                       "2026-09-13", True)
        lines = out["page"]["content"].splitlines()
        self.assertTrue(lines[1].startswith("\t"))
        self.assertIn("quickshell-agenda:: 2026-09-13", lines[1])

    def test_select_preserves_crlf_and_no_final_newline(self):
        self.write(content="- TODO a\r\n- TODO b\r\n")
        current = self.read()
        out = daily_agenda.select_task(self.graph, current["path"],
                                       current["revision"], 1,
                                       "2026-09-13", True)
        raw = (self.graph / "pages" / "Work.md").read_bytes()
        self.assertIn(b"\r\n", raw)
        self.assertNotIn(b"\n  quickshell-agenda", raw.replace(b"\r\n", b"\n@@"))
        self.assertEqual(raw.count(b"\r\n"), 3)

        target = self.graph / "pages" / "Flat.md"
        target.write_bytes(b"- TODO solo")
        solo = project_planner.read_page(self.graph, "pages/Flat.md")
        daily_agenda.select_task(self.graph, solo["path"], solo["revision"],
                                 1, "2026-09-13", True)
        raw2 = target.read_bytes()
        self.assertTrue(raw2.startswith(b"- TODO solo\n"))
        self.assertFalse(raw2.endswith(b"\n"))
        daily_agenda.select_task(
            self.graph, "pages/Flat.md",
            project_planner.read_page(self.graph, "pages/Flat.md")["revision"],
            1, "2026-09-13", False)
        self.assertEqual(target.read_bytes(), b"- TODO solo")

    def test_select_rejects_stale_invalid_and_unsafe(self):
        self.write(content="- TODO task\nplain note\n")
        current = self.read()
        with self.assertRaises(project_planner.GraphError):
            daily_agenda.select_task(self.graph, current["path"],
                                     "0" * 64, 1, "2026-09-13", True)
        with self.assertRaises(project_planner.GraphError):
            daily_agenda.select_task(self.graph, current["path"],
                                     current["revision"], 2,
                                     "2026-09-13", True)
        for bad_line in (0, -1, "1", 1.5, True, None):
            with self.assertRaises(project_planner.GraphError, msg=repr(bad_line)):
                daily_agenda.select_task(self.graph, current["path"],
                                         current["revision"], bad_line,
                                         "2026-09-13", True)
        for bad_date in ("tomorrow", "2026-02-30", "", None, 5):
            with self.assertRaises(project_planner.GraphError, msg=repr(bad_date)):
                daily_agenda.select_task(self.graph, current["path"],
                                         current["revision"], 1, bad_date, True)
        with self.assertRaises(project_planner.GraphError):
            daily_agenda.select_task(self.graph, current["path"],
                                     current["revision"], 1,
                                     "2026-09-13", "yes")
        for bad_path in ("pages/../journals/X.md", "/abs/pages/Work.md",
                         "pages/Work.txt", "pages/Work.md\x00", ""):
            with self.assertRaises(project_planner.GraphError, msg=repr(bad_path)):
                daily_agenda.select_task(self.graph, bad_path,
                                         current["revision"], 1,
                                         "2026-09-13", True)


class CompleteTests(AgendaCase):
    def test_complete_marks_done_and_appends_dated_child(self):
        self.write(content="- TODO ship\n  - TODO existing\n")
        current = self.read()
        out = daily_agenda.complete_task(self.graph, current["path"],
                                         current["revision"], 1,
                                         "wrote tests", "2026-09-13")
        content = out["page"]["content"]
        self.assertIn("- DONE ship", content.splitlines()[0])
        self.assertIn("  - [2026-09-13] wrote tests", content)
        # Progress child is not itself a task.
        self.assertEqual(len(out["page"]["todos"]), 2)
        # Atomic: single write produced both effects.
        self.assertEqual(content.count("- DONE ship"), 1)

    def test_complete_multiline_note_stays_nested(self):
        self.write(content="- TODO parent\n")
        current = self.read()
        out = daily_agenda.complete_task(self.graph, current["path"],
                                         current["revision"], 1,
                                         "first\nsecond\n\nfourth",
                                         "2026-09-14")
        lines = out["page"]["content"].splitlines()
        self.assertEqual(lines[0], "- DONE parent")
        self.assertEqual(lines[1], "  - [2026-09-14] first")
        self.assertEqual(lines[2], "    second")
        self.assertEqual(lines[3].strip(), "")
        self.assertTrue(lines[3].startswith("  "))
        self.assertEqual(lines[4], "    fourth")
        # Every nonblank inserted line is indented deeper than the TODO.
        for extra in lines[1:]:
            self.assertTrue(extra.startswith("  "))

    def test_complete_on_already_done_appends_note(self):
        self.write(content="- DONE old\n")
        current = self.read()
        out = daily_agenda.complete_task(self.graph, current["path"],
                                         current["revision"], 1,
                                         "follow-up", "2026-09-13")
        self.assertIn("- DONE old", out["page"]["content"])
        self.assertIn("- [2026-09-13] follow-up", out["page"]["content"])

    def test_complete_preserves_crlf(self):
        before = "- TODO task\r\n  - TODO kid\r\n"
        (self.graph / "pages" / "Work.md").write_bytes(before.encode())
        current = self.read()
        daily_agenda.complete_task(self.graph, current["path"],
                                   current["revision"], 1,
                                   "note", "2026-09-13")
        raw = (self.graph / "pages" / "Work.md").read_bytes()
        self.assertEqual(raw.count(b"\r\n"), 3)
        self.assertNotIn(b"\n  - [2026-09-13]", raw.replace(b"\r\n", b"@@"))

    def test_complete_requires_nonblank_note_and_valid_date(self):
        self.write(content="- TODO task\n")
        current = self.read()
        for bad_note in ("", "   ", "\n\t\n", None, 5):
            with self.assertRaises(project_planner.GraphError, msg=repr(bad_note)):
                daily_agenda.complete_task(self.graph, current["path"],
                                           current["revision"], 1,
                                           bad_note, "2026-09-13")
        with self.assertRaises(project_planner.GraphError):
            daily_agenda.complete_task(self.graph, current["path"],
                                       current["revision"], 1,
                                       "note", "not-a-date")
        with self.assertRaises(project_planner.GraphError):
            daily_agenda.complete_task(self.graph, current["path"],
                                       "0" * 64, 1, "note", "2026-09-13")

    def test_complete_rejects_non_task_line(self):
        self.write(content="just a note\n- TODO real\n")
        current = self.read()
        with self.assertRaises(project_planner.GraphError):
            daily_agenda.complete_task(self.graph, current["path"],
                                       current["revision"], 1,
                                       "note", "2026-09-13")


class MixedIndentTests(AgendaCase):
    def test_complete_mixed_tabs_spaces_sibling_before(self):
        # "\t" and "    " share visual depth 4: B is a sibling of A, so the
        # progress child must land before B at a deeper tab indent instead of
        # after B nested under it by Markdown tab semantics.
        self.write(content="\t- TODO A\n    - TODO B\n")
        current = self.read()
        out = daily_agenda.complete_task(self.graph, current["path"],
                                         current["revision"], 1,
                                         "did a", "2026-09-13")
        self.assertEqual(out["page"]["content"],
                         "\t- DONE A\n"
                         "\t\t- [2026-09-13] did a\n"
                         "    - TODO B\n")

    def test_complete_mixed_sibling_sequential_stays_before_sibling(self):
        self.write(content="\t- TODO A\n    - TODO B\n")
        current = self.read()
        daily_agenda.complete_task(self.graph, current["path"],
                                   current["revision"], 1,
                                   "first", "2026-09-13")
        second = self.read()
        out = daily_agenda.complete_task(self.graph, second["path"],
                                         second["revision"], 1,
                                         "second", "2026-09-13")
        self.assertEqual(out["page"]["content"],
                         "\t- DONE A\n"
                         "\t\t- [2026-09-13] first\n"
                         "\t\t- [2026-09-13] second\n"
                         "    - TODO B\n")

    def test_select_mixed_tabs_spaces_sibling_before(self):
        self.write(content="\t- TODO A\n    - TODO B\n")
        current = self.read()
        out = daily_agenda.select_task(self.graph, current["path"],
                                       current["revision"], 1,
                                       "2026-09-13", True)
        self.assertEqual(out["page"]["content"],
                         "\t- TODO A\n"
                         "\t\tquickshell-agenda:: 2026-09-13\n"
                         "    - TODO B\n")

    def test_complete_mixed_descendants_preserves_child_convention(self):
        # Both "\t" and "    " are visual children (depth 4) of the
        # top-level parent; the direct-child level is the minimum (4) and
        # the new bullet preserves the first raw at that level instead of
        # guessing the parent-plus-two-spaces indent.
        self.write(content="- TODO P\n\t- TODO Q\n    - TODO R\n")
        current = self.read()
        out = daily_agenda.complete_task(self.graph, current["path"],
                                         current["revision"], 1,
                                         "note", "2026-09-13")
        self.assertEqual(out["page"]["content"],
                         "- DONE P\n"
                         "\t- TODO Q\n"
                         "    - TODO R\n"
                         "\t- [2026-09-13] note\n")
        self.assertProgressNotNested(out["page"]["content"], [1, 2], 3)

    def assertProgressNotNested(self, content, pre_desc, new_index):
        bodies = project_planner._split_lines(content)[0::2]
        for desc in pre_desc:
            _parent, _base, _child, _agenda, _prop, last, _end = (
                daily_agenda._block_info(bodies, desc)
            )
            self.assertFalse(
                desc < new_index <= last,
                f"new note {new_index} nested under descendant {desc}",
            )

    def test_complete_decreasing_mixed_rejected_unchanged(self):
        # Reviewer P1: Q depth 4 then R depth 2. Copying Q's tab would nest
        # the new tab note under R by _block_info(R). Reject unchanged.
        before = "- TODO P\n\t- TODO Q\n  - TODO R\n"
        self.write(content=before)
        current = self.read()
        with self.assertRaises(project_planner.GraphError):
            daily_agenda.complete_task(self.graph, current["path"],
                                       current["revision"], 1,
                                       "note", "2026-09-13")
        self.assertEqual((self.graph / "pages" / "Work.md").read_text(
            encoding="utf-8"), before)
        current = self.read()
        with self.assertRaises(project_planner.GraphError):
            daily_agenda.select_task(self.graph, current["path"],
                                     current["revision"], 1,
                                     "2026-09-13", True)
        self.assertEqual((self.graph / "pages" / "Work.md").read_text(
            encoding="utf-8"), before)

    def test_complete_decreasing_all_spaces_rejected_unchanged(self):
        before = "- TODO P\n    - TODO Q\n  - TODO R\n"
        self.write(content=before)
        current = self.read()
        with self.assertRaises(project_planner.GraphError):
            daily_agenda.complete_task(self.graph, current["path"],
                                       current["revision"], 1,
                                       "note", "2026-09-13")
        self.assertEqual((self.graph / "pages" / "Work.md").read_text(
            encoding="utf-8"), before)

    def test_complete_minimum_preserved_mixed_increasing_owned(self):
        # Direct level is the minimum (2, "  "); the deeper tab child stays
        # nested under P without owning the new note.
        self.write(content="- TODO P\n  - TODO R\n\t- TODO Q\n")
        current = self.read()
        out = daily_agenda.complete_task(self.graph, current["path"],
                                         current["revision"], 1,
                                         "note", "2026-09-13")
        self.assertEqual(out["page"]["content"],
                         "- DONE P\n"
                         "  - TODO R\n"
                         "\t- TODO Q\n"
                         "  - [2026-09-13] note\n")
        self.assertProgressNotNested(out["page"]["content"], [1, 2], 3)

    def test_complete_minimum_preserved_nested_increasing_owned(self):
        self.write(content="- TODO P\n  - TODO Q\n    - TODO R\n")
        current = self.read()
        out = daily_agenda.complete_task(self.graph, current["path"],
                                         current["revision"], 1,
                                         "note", "2026-09-13")
        self.assertEqual(out["page"]["content"],
                         "- DONE P\n"
                         "  - TODO Q\n"
                         "    - TODO R\n"
                         "  - [2026-09-13] note\n")
        self.assertProgressNotNested(out["page"]["content"], [1, 2], 3)

    def test_complete_preserves_tab_plus_spaces_child_indent(self):
        self.write(content="\t- TODO P\n\t  - TODO Q\n")
        current = self.read()
        out = daily_agenda.complete_task(self.graph, current["path"],
                                         current["revision"], 1,
                                         "note", "2026-09-13")
        self.assertEqual(out["page"]["content"],
                         "\t- DONE P\n"
                         "\t  - TODO Q\n"
                         "\t  - [2026-09-13] note\n")

    def test_ambiguous_nested_mixed_rejected_unchanged(self):
        before = "  - TODO parent\n\t- TODO kid\n"
        self.write(content=before)
        current = self.read()
        with self.assertRaises(project_planner.GraphError):
            daily_agenda.complete_task(self.graph, current["path"],
                                       current["revision"], 1,
                                       "note", "2026-09-13")
        self.assertEqual((self.graph / "pages" / "Work.md").read_text(
            encoding="utf-8"), before)
        current = self.read()
        with self.assertRaises(project_planner.GraphError):
            daily_agenda.select_task(self.graph, current["path"],
                                     current["revision"], 1,
                                     "2026-09-13", True)
        self.assertEqual((self.graph / "pages" / "Work.md").read_text(
            encoding="utf-8"), before)


class ToggleTests(AgendaCase):
    def test_toggle_wraps_planner(self):
        self.write(content="- TODO task\n")
        current = self.read()
        out = daily_agenda.toggle_agenda_task(self.graph, current["path"],
                                              current["revision"], 1, True)
        self.assertIn("page", out)
        self.assertIn("- DONE task", out["page"]["content"])
        reopened = daily_agenda.toggle_agenda_task(
            self.graph, out["page"]["path"], out["page"]["revision"], 1, False)
        self.assertIn("- TODO task", reopened["page"]["content"])

    def test_toggle_rejects_non_boolean(self):
        self.write(content="- TODO task\n")
        current = self.read()
        with self.assertRaises(project_planner.GraphError):
            daily_agenda.toggle_agenda_task(self.graph, current["path"],
                                            current["revision"], 1, "true")


class SafetyTests(AgendaCase):
    def test_symlink_fifo_and_size_rejected(self):
        outside = Path(self.temp.name) / "outside.md"
        outside.write_text("- TODO secret\n", encoding="utf-8")
        link = self.graph / "pages" / "Work.md"
        link.symlink_to(outside)
        # Agenda listing skips unsafe pages instead of failing the whole day.
        skipped = daily_agenda.list_agenda(self.graph, "2026-09-13")
        self.assertEqual(skipped["tasks"], [])
        # Direct writes still reject the unsafe target.
        with self.assertRaises(project_planner.GraphError):
            project_planner.read_page(self.graph, "pages/Work.md")
        link.unlink()
        self.write(content="- TODO ok\n")
        current = self.read()
        big_note = "x" * (project_planner.PAGE_LIMIT + 1)
        with self.assertRaises(project_planner.GraphError):
            daily_agenda.complete_task(self.graph, current["path"],
                                       current["revision"], 1,
                                       big_note, "2026-09-13")

    def test_unsafe_paths_rejected_everywhere(self):
        self.write(content="- TODO ok\n")
        current = self.read()
        for bad in ("pages/../journals/X.md", "journals/2026_09_13.md",
                    "PAGES/Work.md", "C:\\pages\\Work.md"):
            with self.assertRaises(project_planner.GraphError, msg=bad):
                daily_agenda.select_task(self.graph, bad, current["revision"],
                                         1, "2026-09-13", True)
            with self.assertRaises(project_planner.GraphError, msg=bad):
                daily_agenda.complete_task(self.graph, bad, current["revision"],
                                           1, "note", "2026-09-13")

    def test_unrelated_notes_untouched(self):
        before = ("- TODO keep\n"
                  "  note:: preserve me\n"
                  "  - TODO kid\n"
                  "    detail line\n"
                  "- TODO second\n")
        self.write(content=before)
        current = self.read()
        out = daily_agenda.select_task(self.graph, current["path"],
                                       current["revision"], 1,
                                       "2026-09-13", True)
        after = out["page"]["content"]
        self.assertIn("note:: preserve me", after)
        self.assertIn("detail line", after)
        self.assertIn("- TODO second", after)


class CliContractTests(AgendaCase):
    def test_cli_list_shape(self):
        self.write(content="- TODO item\n")
        completed = self.cli("list", {"date": "2026-09-13"})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        value = json.loads(completed.stdout)
        self.assertEqual(value["date"], "2026-09-13")
        self.assertIn("graphName", value)
        self.assertIn("tasks", value)
        self.assertIn("truncated", value)
        entry = value["tasks"][0]
        for key in ("path", "page", "revision", "line", "task", "marker",
                    "done", "scheduledDate"):
            self.assertIn(key, entry)

    def test_cli_select_complete_toggle_shapes(self):
        self.write(content="- TODO item\n")
        current = self.read()
        selected = self.cli("select", {"path": current["path"],
                                       "revision": current["revision"],
                                       "line": 1, "date": "2026-09-13",
                                       "selected": True})
        self.assertEqual(selected.returncode, 0, selected.stderr)
        self.assertIn("page", json.loads(selected.stdout))
        current = self.read()
        done = self.cli("complete", {"path": current["path"],
                                     "revision": current["revision"],
                                     "line": 1, "note": "did it",
                                     "date": "2026-09-13"})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("page", json.loads(done.stdout))
        current = self.read()
        toggled = self.cli("toggle", {"path": current["path"],
                                      "revision": current["revision"],
                                      "line": 1, "done": False})
        self.assertEqual(toggled.returncode, 0, toggled.stderr)
        self.assertIn("page", json.loads(toggled.stdout))

    def test_cli_errors_are_json_nonzero(self):
        self.write(content="- TODO item\n")
        bad_date = self.cli("list", {"date": "nope"})
        self.assertEqual(bad_date.returncode, 1)
        self.assertEqual(set(json.loads(bad_date.stdout)), {"error"})
        current = self.read()
        stale = self.cli("select", {"path": current["path"],
                                    "revision": "0" * 64, "line": 1,
                                    "date": "2026-09-13", "selected": True})
        self.assertEqual(stale.returncode, 1)
        self.assertIn("error", json.loads(stale.stdout))
        blank = self.cli("complete", {"path": current["path"],
                                      "revision": current["revision"],
                                      "line": 1, "note": "  ",
                                      "date": "2026-09-13"})
        self.assertEqual(blank.returncode, 1)
        self.assertIn("error", json.loads(blank.stdout))
        malformed = self.cli("list", {"date": 5})
        self.assertEqual(malformed.returncode, 1)
        self.assertIn("error", json.loads(malformed.stdout))
        invalid_json = self.cli("list", None, raw="{oops")
        self.assertEqual(invalid_json.returncode, 1)
        self.assertIn("error", json.loads(invalid_json.stdout))

    def test_cli_non_object_input_is_json_error(self):
        completed = self.cli("list", None, raw="[1,2]")
        self.assertEqual(completed.returncode, 1)
        self.assertIn("error", json.loads(completed.stdout))


if __name__ == "__main__":
    unittest.main()
