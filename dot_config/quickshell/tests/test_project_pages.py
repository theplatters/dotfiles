import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import project_planner
from logseq_common import GraphError


def run_cli(graph, args, stdin_text):
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "project_planner.py"),
         "--graph", str(graph), *args],
        input=stdin_text, text=True, capture_output=True, timeout=15,
        check=False)


TEMPLATES = (
    "type:: template\n"
    "- ## Project\n"
    "\t- template:: project\n"
    "\t  template-including-parent:: false\n"
    "\t\t- type:: project\n"
    "\t\t  status:: active\n"
    "\t\t- Desired outcome\n"
    "\t\t\t-\n"
    "\t\t- Current state\n"
    "\t\t\t-\n"
    "\t\t- Next action\n"
    "\t\t\t- TODO\n"
    "\t\t- Open questions\n"
    "\t\t\t-\n"
    "- ## Talk\n"
    "\t- template:: talk\n"
    "\t  template-including-parent:: false\n"
    "\t\t- page-type:: [[Vortrag]]\n"
    "\t\t  speaker::\n"
    "\t\t  tags::\n"
    "\t\t  date:: <% today %>\n"
    "\t\t  place::\n"
    "\t\t  note:: <% future %>\n"
    "\t\t- Notes\n"
    "\t\t\t- ## Notizen\n"
    "- ## Meeting\n"
    "\t- Meeting notes\n"
    "\t  template:: meeting\n"
    "\t  template-including-parent:: true\n"
    "\t\t- Attendees\n"
    "\t\t\t-\n"
)


def expected_today():
    import datetime as _datetime
    today = _datetime.date.today()
    day = today.day
    if 11 <= day % 100 <= 13:
        suffix = "th"
    elif day % 10 == 1:
        suffix = "st"
    elif day % 10 == 2:
        suffix = "nd"
    elif day % 10 == 3:
        suffix = "rd"
    else:
        suffix = "th"
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug",
              "Sep", "Oct", "Nov", "Dec")
    return f"{months[today.month - 1]} {day}{suffix}, {today.year}"


class ProjectPagesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.graph = Path(self.temp.name) / "graph"
        (self.graph / "pages").mkdir(parents=True)
        (self.graph / "journals").mkdir()
        (self.graph / "pages" / "Templates.md").write_text(
            TEMPLATES, encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self, **fields):
        request = {"stage": "prepare", **fields}
        return project_planner.create_page(self.graph, request)

    def commit(self, **fields):
        request = {"stage": "commit", **fields}
        return project_planner.create_page(self.graph, request)

    # -- prepare rendering -------------------------------------------------

    def test_prepare_renders_project_template_with_leading_merge(self):
        result = self.prepare(page="Schulworkshops", template="project",
                              template_page="Templates",
                              properties={"project-type": "Arbeit",
                                          "status": "on-hold"})
        self.assertEqual(result["stage"], "prepare")
        self.assertEqual(result["target"], "pages/Schulworkshops.md")
        self.assertEqual(result["template"], "project")
        self.assertFalse(result["template_including_parent"])
        # Template keys keep their order; request keys override in place
        # and non-conflicting keys append after the template's.
        self.assertEqual(result["content"],
                         "type:: project\n"
                         "status:: on-hold\n"
                         "project-type:: Arbeit\n"
                         "- Desired outcome\n"
                         "\t-\n"
                         "- Current state\n"
                         "\t-\n"
                         "- Next action\n"
                         "\t- TODO\n"
                         "- Open questions\n"
                         "\t-\n")
        self.assertEqual(result["content_sha256"],
                         hashlib.sha256(
                             result["content"].encode("utf-8")).hexdigest())
        # Prepare writes nothing.
        self.assertFalse((self.graph / "pages" / "Schulworkshops.md").exists())

    def test_prepare_defaults_template_page_and_appends_properties(self):
        result = self.prepare(page="pages/Schulworkshops.md",
                              template="project",
                              properties={"project-type": "Arbeit"})
        self.assertTrue(result["content"].startswith(
            "type:: project\nstatus:: active\nproject-type:: Arbeit\n"))

    def test_prepare_expands_today_and_leaves_other_macros(self):
        result = self.prepare(page="TalkTest", template="talk")
        today = expected_today()
        self.assertIn(f"date:: {today}\n", result["content"])
        self.assertIn("note:: <% future %>\n", result["content"])
        # The property-only first block becomes the page leading block.
        self.assertTrue(result["content"].startswith(
            "page-type:: [[Vortrag]]\n"))
        self.assertIn("- Notes\n\t- ## Notizen\n", result["content"])

    def test_prepare_including_parent_true_keeps_own_content(self):
        result = self.prepare(page="Standup", template="meeting")
        self.assertTrue(result["template_including_parent"])
        self.assertEqual(result["content"],
                         "- Meeting notes\n\t- Attendees\n\t\t-\n")

    def test_prepare_without_template_is_properties_only(self):
        result = self.prepare(page="Plain",
                              properties={"type": "project",
                                          "status": "active"})
        self.assertEqual(result["template"], "")
        self.assertFalse(result["template_including_parent"])
        self.assertEqual(result["content"],
                         "type:: project\nstatus:: active\n")
        empty = self.prepare(page="Empty")
        self.assertEqual(empty["content"], "")
        self.assertEqual(empty["content_sha256"],
                         hashlib.sha256(b"").hexdigest())

    def test_template_not_found_and_ambiguous(self):
        with self.assertRaises(GraphError) as missing:
            self.prepare(page="X", template="nope")
        self.assertIn("template not found: nope", str(missing.exception))
        with self.assertRaises(GraphError) as absent_page:
            self.prepare(page="X", template="project",
                         template_page="Missing")
        self.assertIn("unsafe or missing",
                      str(absent_page.exception))
        # Duplicate template names are rejected instead of picked silently.
        with (self.graph / "pages" / "Templates.md").open(
                "a", encoding="utf-8") as handle:
            handle.write("- ## Dup\n\t- template:: dupe\n"
                         "- ## Dup2\n\t- template:: dupe\n")
        with self.assertRaises(GraphError) as ambiguous:
            self.prepare(page="X", template="dupe")
        self.assertIn("template is ambiguous: dupe",
                      str(ambiguous.exception))

    def test_page_name_validation_rejects_and_writes_nothing(self):
        bad = ["", "/abs.md", "/etc/passwd", "..", ".", "a/../b", "a/./b",
               "a//b", "../evil", "pages/../journals/x.md", "pages/",
               "pages", "pages/Work.txt", "back\\slash", "nul\x00name",
               "with\nnewline", ".md", "x" * 5000,
               "pages/" + "y" * 5000 + ".md"]
        for name in bad:
            if name == "pages":
                continue  # bare "pages" is a legal page name -> skip
            with self.subTest(page=name):
                with self.assertRaises(GraphError):
                    self.prepare(page=name, template="project")
        for name in bad:
            if name == "pages":
                continue
            with self.subTest(commit=name):
                with self.assertRaises(GraphError):
                    self.commit(page=name, template="project",
                                expected_sha256="0" * 64)
        self.assertEqual(sorted(p.name for p in
                                (self.graph / "pages").iterdir()),
                         ["Templates.md"])

    def test_page_name_namespace_mapping(self):
        result = self.prepare(page="Workshops/AG", template="project")
        self.assertEqual(result["target"], "pages/Workshops___AG.md")
        committed = self.commit(
            page="Workshops/AG", template="project",
            expected_sha256=result["content_sha256"])
        self.assertEqual(committed["target"], "pages/Workshops___AG.md")
        self.assertTrue(
            (self.graph / "pages" / "Workshops___AG.md").is_file())
        # Explicit pages/ spellings stay literal (nested supported).
        (self.graph / "pages" / "nested").mkdir()
        nested = self.prepare(page="pages/nested/Deep.md", template="project")
        self.assertEqual(nested["target"], "pages/nested/Deep.md")

    def test_bare_namespace_agrees_with_registry_spelling(self):
        # FIX 1: a bare "a/b" normalizes (TS and backend alike) to the flat
        # on-disk spelling, and the registry stores that normalized value
        # verbatim — so the registry entry and the created file agree.
        import projects as _projects
        target = project_planner._normalize_create_target("a/b")
        self.assertEqual(target.as_posix(), "pages/a___b.md")
        self.assertEqual(_projects._validate_logseq_path(target.as_posix()),
                         "pages/a___b.md")
        prepared = self.prepare(page="a/b", template="project")
        self.assertEqual(prepared["target"], "pages/a___b.md")
        committed = self.commit(
            page="a/b", template="project",
            expected_sha256=prepared["content_sha256"])
        self.assertEqual(committed["target"], "pages/a___b.md")
        self.assertTrue((self.graph / "pages" / "a___b.md").is_file())
        self.assertFalse((self.graph / "pages" / "a").exists())

    def test_nested_literal_without_parent_dir_fails_cleanly(self):
        # FIX 1: an explicit nested spelling whose parent directory is
        # absent prepares fine but the commit fails without writing.
        prepared = self.prepare(page="pages/nested/Deep.md",
                                template="project")
        self.assertEqual(prepared["target"], "pages/nested/Deep.md")
        with self.assertRaises(GraphError):
            self.commit(page="pages/nested/Deep.md", template="project",
                        expected_sha256=prepared["content_sha256"])
        self.assertFalse((self.graph / "pages" / "nested").exists())

    def test_bare_pages_name_is_legal(self):
        # Bare "pages" is a legal page name (not the pages/ prefix).
        prepared = self.prepare(page="pages", template="project")
        self.assertEqual(prepared["target"], "pages/pages.md")
        committed = self.commit(
            page="pages", template="project",
            expected_sha256=prepared["content_sha256"])
        self.assertEqual(committed["target"], "pages/pages.md")
        self.assertTrue((self.graph / "pages" / "pages.md").is_file())

    def test_continuation_at_exact_parent_depth_stays_with_block(self):
        # FIX 4: a continuation/property line indented with exactly `depth`
        # tabs belongs to that block, not to a shallower ancestor.
        text = ("- ## T\n"
                "\t- template:: t\n"
                "\t  template-including-parent:: false\n"
                "\t\t- title\n"
                "\t\tspeaker:: Jane\n"
                "\t\t- next\n")
        roots = project_planner._parse_template_blocks(text)
        self.assertEqual(len(roots), 1)
        marker = roots[0].children[0]
        self.assertEqual(marker.children[0].content, "title")
        title = marker.children[0]
        self.assertIn("speaker:: Jane",
                      [item for _, item in title.conts])
        # The marker itself keeps only its own continuation line.
        self.assertEqual([item for _, item in marker.conts],
                         ["template-including-parent:: false"])
        self.assertEqual(
            [child.content for child in marker.children],
            ["title", "next"])

    def test_commit_wrong_expected_target_writes_nothing(self):
        # FIX 5: a commit whose re-rendered target differs from the
        # prepared (approved) target fails before any write.
        prepared = self.prepare(page="Bound", template="project")
        with self.assertRaises(GraphError) as bound:
            self.commit(page="Bound", template="project",
                        expected_sha256=prepared["content_sha256"],
                        expected_target="pages/Other.md")
        self.assertIn("target changed", str(bound.exception))
        self.assertFalse((self.graph / "pages" / "Bound.md").exists())
        self.assertFalse((self.graph / "pages" / "Other.md").exists())
        # The approved target still commits cleanly (and without any
        # expected_target, for backward compatibility).
        ok = self.commit(page="Bound", template="project",
                         expected_sha256=prepared["content_sha256"],
                         expected_target=prepared["target"])
        self.assertTrue(ok["created"])
        self.assertTrue((self.graph / "pages" / "Bound.md").is_file())

    def test_properties_validation(self):
        with self.assertRaises(GraphError):
            self.prepare(page="X", properties={"bad key": "v"})
        with self.assertRaises(GraphError):
            self.prepare(page="X", properties={"k" * 65: "v"})
        with self.assertRaises(GraphError):
            self.prepare(page="X", properties={"template": "v"})
        with self.assertRaises(GraphError):
            self.prepare(page="X",
                         properties={"Template-Including-Parent": "x"})
        with self.assertRaises(GraphError):
            self.prepare(page="X", properties={"k": "a\nb"})
        with self.assertRaises(GraphError):
            self.prepare(page="X", properties={"k": "x" * 513})
        with self.assertRaises(GraphError):
            self.prepare(page="X", properties={"k": 3})
        with self.assertRaises(GraphError):
            self.prepare(page="X",
                         properties={f"k{i}": "v" for i in range(17)})
        with self.assertRaises(GraphError):
            self.prepare(page="X", properties=["not", "object"])
        with self.assertRaises(GraphError):
            self.prepare(page="X", template="")
        with self.assertRaises(GraphError):
            self.prepare(page="X", template="project\nx")

    def test_unknown_and_missing_fields_rejected(self):
        with self.assertRaises(GraphError) as unknown:
            self.prepare(page="X", template="project", bogus=1)
        self.assertIn("unknown field", str(unknown.exception))
        with self.assertRaises(GraphError):
            # expected_sha256 is not a prepare field.
            project_planner.create_page(
                self.graph, {"stage": "prepare", "page": "X",
                             "expected_sha256": "0" * 64})
        with self.assertRaises(GraphError):
            self.commit(page="X", template="project", bogus=1,
                        expected_sha256="0" * 64)
        with self.assertRaises(GraphError):
            project_planner.create_page(self.graph, {"page": "X"})
        with self.assertRaises(GraphError):
            project_planner.create_page(
                self.graph, {"stage": "preview", "page": "X"})
        with self.assertRaises(GraphError):
            project_planner.create_page(self.graph, {"stage": "prepare"})
        with self.assertRaises(GraphError):
            self.commit(page="X", template="project")
        with self.assertRaises(GraphError):
            self.commit(page="X", template="project",
                        expected_sha256="not-hex")
        with self.assertRaises(GraphError):
            project_planner.create_page(self.graph, ["prepare"])

    def test_properties_boundary_agrees_with_tool_limits(self):
        # FIX 3: the backend boundary is exactly 16 entries / 64-char keys
        # / 512-char values — the same limits the palette tool enforces
        # before any approval, so boundary input passes here.
        boundary = {f"k{i:02d}": "v" for i in range(16)}
        boundary["k" * 64] = "x" * 512
        del boundary["k00"]
        result = self.prepare(page="Boundary", properties=boundary)
        self.assertIn(("k" * 64) + ":: " + "x" * 512 + "\n",
                      result["content"])

    def test_rendered_output_over_limit_rejected(self):
        filler = "".join("\t\t- %s\n" % ("x" * 100) for _ in range(1200))
        big = ("type:: template\n- ## Big\n\t- template:: big\n"
               "\t  template-including-parent:: false\n"
               "\t\t- type:: project\n"
               + filler)
        encoded = big.encode("utf-8")
        self.assertLess(len(encoded), project_planner.PAGE_LIMIT)
        (self.graph / "pages" / "Big.md").write_bytes(encoded)
        without_props = self.prepare(page="Big1", template="big",
                                     template_page="Big")
        self.assertLess(len(without_props["content"].encode("utf-8")),
                        project_planner.PAGE_LIMIT)
        fat = {f"k{i:02d}": "v" * 512 for i in range(16)}
        with self.assertRaises(GraphError) as oversize:
            self.prepare(page="Big2", template="big", template_page="Big",
                         properties=fat)
        self.assertIn("larger than 128 KiB", str(oversize.exception))
        self.assertFalse((self.graph / "pages" / "Big1.md").exists())
        self.assertFalse((self.graph / "pages" / "Big2.md").exists())

    # -- commit ----------------------------------------------------------

    def test_commit_happy_path_writes_exact_bytes(self):
        prepared = self.prepare(
            page="Schulworkshops", template="project",
            template_page="Templates", properties={"project-type": "Arbeit"})
        result = self.commit(
            page="Schulworkshops", template="project",
            template_page="Templates", properties={"project-type": "Arbeit"},
            expected_sha256=prepared["content_sha256"])
        self.assertEqual(result, {
            "stage": "commit",
            "created": True,
            "target": "pages/Schulworkshops.md",
            "content_sha256": prepared["content_sha256"],
        })
        target = self.graph / "pages" / "Schulworkshops.md"
        self.assertEqual(target.read_bytes(),
                         prepared["content"].encode("utf-8"))
        self.assertEqual(target.stat().st_mode & 0o777, 0o644)

    def test_commit_wrong_sha_writes_nothing(self):
        self.prepare(page="Schulworkshops", template="project")
        with self.assertRaises(GraphError) as stale:
            self.commit(page="Schulworkshops", template="project",
                        expected_sha256="0" * 64)
        self.assertIn("content changed, re-run prepare",
                      str(stale.exception))
        self.assertFalse((self.graph / "pages" / "Schulworkshops.md").exists())

    def test_commit_never_overwrites_an_existing_page(self):
        prepared = self.prepare(page="Taken", template="project")
        (self.graph / "pages" / "Taken.md").write_text(
            "existing\n", encoding="utf-8")
        with self.assertRaises(GraphError) as taken:
            self.commit(page="Taken", template="project",
                        expected_sha256=prepared["content_sha256"])
        self.assertIn("already exists", str(taken.exception))
        self.assertEqual((self.graph / "pages" / "Taken.md").read_text(
            encoding="utf-8"), "existing\n")
        # A symlink at the target is never followed, only refused.
        (self.graph / "pages" / "Taken.md").unlink()
        outside = Path(self.temp.name) / "outside.md"
        outside.write_text("secret\n", encoding="utf-8")
        (self.graph / "pages" / "Linked.md").symlink_to(outside)
        linked = self.prepare(page="Other", template="project")
        with self.assertRaises(GraphError):
            self.commit(page="Linked", template="project",
                        expected_sha256=linked["content_sha256"])
        self.assertEqual(outside.read_text(encoding="utf-8"), "secret\n")

    def test_commit_detects_template_change_after_prepare(self):
        prepared = self.prepare(page="Late", template="project")
        path = self.graph / "pages" / "Templates.md"
        text = path.read_text(encoding="utf-8")
        self.assertIn("\t\t- Open questions\n", text)
        path.write_text(text.replace("\t\t- Open questions\n",
                                     "\t\t- Open questions changed\n"),
                        encoding="utf-8")
        with self.assertRaises(GraphError) as changed:
            self.commit(page="Late", template="project",
                        expected_sha256=prepared["content_sha256"])
        self.assertIn("content changed, re-run prepare",
                      str(changed.exception))
        self.assertFalse((self.graph / "pages" / "Late.md").exists())

    def test_commit_empty_page_is_legal(self):
        prepared = self.prepare(page="Blank")
        result = self.commit(page="Blank",
                             expected_sha256=prepared["content_sha256"])
        self.assertTrue(result["created"])
        self.assertEqual(
            (self.graph / "pages" / "Blank.md").read_bytes(), b"")

    def test_today_format_matches_journal_titles(self):
        self.assertRegex(project_planner._today_logseq(),
                         r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
                         r"\d{1,2}(st|nd|rd|th), \d{4}$")
        self.assertEqual(project_planner._today_logseq(), expected_today())

    # -- CLI -------------------------------------------------------------

    def test_cli_prepare_and_commit_roundtrip(self):
        prepared = run_cli(self.graph, ["create-page"], json.dumps({
            "stage": "prepare", "page": "Schulworkshops",
            "template": "project", "template_page": "Templates",
            "properties": {"project-type": "Arbeit"}}))
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        payload = json.loads(prepared.stdout)
        self.assertEqual(payload["target"], "pages/Schulworkshops.md")
        committed = run_cli(self.graph, ["create-page"], json.dumps({
            "stage": "commit", "page": "Schulworkshops",
            "template": "project", "template_page": "Templates",
            "properties": {"project-type": "Arbeit"},
            "expected_sha256": payload["content_sha256"]}))
        self.assertEqual(committed.returncode, 0, committed.stderr)
        self.assertEqual(json.loads(committed.stdout)["created"], True)
        self.assertEqual(
            (self.graph / "pages" / "Schulworkshops.md").read_text(
                encoding="utf-8"), payload["content"])
        # Second commit never overwrites: nonzero exit, content untouched.
        again = run_cli(self.graph, ["create-page"], json.dumps({
            "stage": "commit", "page": "Schulworkshops",
            "template": "project", "template_page": "Templates",
            "properties": {"project-type": "Arbeit"},
            "expected_sha256": payload["content_sha256"]}))
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("already exists", again.stderr)
        self.assertEqual(
            (self.graph / "pages" / "Schulworkshops.md").read_text(
                encoding="utf-8"), payload["content"])
        # Errors stay single-line on stderr with empty stdout.
        bad = run_cli(self.graph, ["create-page"], json.dumps({
            "stage": "commit", "page": "Schulworkshops",
            "template": "project",
            "expected_sha256": "0" * 64}))
        self.assertNotEqual(bad.returncode, 0)
        self.assertEqual(bad.stdout, "")


if __name__ == "__main__":
    unittest.main()
